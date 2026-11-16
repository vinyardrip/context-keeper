"""PLAN.md AST parser and renderer.

Three strictly separated layers:

1. ``parse_plan`` — PURE, NON-MUTATING. Reads disk text into the AST.
   Both CommonMark (``- [ ]``) and the legacy no-space form
   (``- []``) are accepted as input. No file I/O, no normalization.

2. ``normalize_plan`` — MUTATES the AST in-memory only. Fixes
   duplicate ``[>]`` markers and any other structural anomalies.
   Must be called explicitly before ``render_plan`` to commit
   changes to disk.

3. ``render_plan`` — Pure function from AST → string. Full-file
   deterministic render. Sections preserved.

Mutations (``add_task``, ``focus_task``, ``toggle_done``,
``edit_note``) work entirely on the ``TaskList`` AST. The string
mutator helpers have been removed.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from .models import (
    HistoryEntry,
    Section,
    Task,
    TaskList,
    TaskStatus,
    history_timestamp,
    note_stamp,
)


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Canonical CommonMark: "- [ ]", "- [>]", "- [~]", "- [x]"
_CANONICAL_TASK_RE = re.compile(
    r"""^-\s+\[(?P<marker>[ >x~])\]\s+(?P<title>.+?)\s*$
    """,
    re.VERBOSE,
)

# Legacy no-space form for OPEN: "- []"
_LEGACY_OPEN_RE = re.compile(
    r"""^-\s+\[\]\s+(?P<title>.+?)\s*$
    """,
    re.VERBOSE,
)

# Fallback: "- [>]" / "- [x]" / "- [~]" (FOCUSED / PAUSED / DONE are
# unambiguous; only OPEN has a non-space form).
_LEGACY_FOCUSED_DONE_RE = re.compile(
    r"""^-\s+\[(?P<marker>>|x|X|~)\]\s+(?P<title>.+?)\s*$
    """,
    re.VERBOSE,
)

# Stable task reference: the hidden HTML comment at the END of a task
# line ("<!-- id: a1b2c3d4 -->"). Captured and stripped from the title
# so the AST keeps the clean text plus a separate ``uuid`` field.
_TASK_UUID_RE = re.compile(
    r"<!--\s*id:\s*(?P<uuid>[0-9A-Za-z]+)\s*-->\s*$"
)

# Inline process note directly beneath a task line:
#   "* Note (2026-11-04 14:32 UTC): waiting on CI"
#   "* Note: waiting on CI"          (legacy, no stamp)
_INLINE_NOTE_RE = re.compile(
    r"^\s*\*\s*Note(?:\s*\((?P<stamp>[^)]*)\))?:\s*(?P<text>.+?)\s*$"
)

# Multiline task description line (v0.13.4), directly beneath a task
# bullet (or beneath another owned description / note line):
#   "  > First description line"
#   "  >"                            (empty paragraph-break line)
# The indent requirement keeps column-0 blockquotes (user prose in
# sections) unowned — only indented quotes attach to tasks.
_DESC_LINE_RE = re.compile(r"^\s+>\s?(?P<text>.*)$")

# Inline due-date marker at the END of a task title (v0.13.5):
#   "- [ ] Ship the release @due(2026-12-01) <!-- id: a1b2c3d4 -->"
# Only a strict, calendar-valid TRAILING marker is extracted into the
# AST ``due`` field; anything else (mid-title occurrences, junk dates)
# stays plain title text. The renderer re-attaches the marker in the
# same position (before the UUID comment), so the round-trip stays
# byte-exact.
_TASK_DUE_RE = re.compile(r"@due\((?P<date>\d{4}-\d{2}-\d{2})\)\s*$")

_HEADER_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*$")

# HISTORY.md completed-task line:
#   "- [x] 2026-11-04 16:45: write migration <!-- id: m3n4o5p6 -->"
_HISTORY_ENTRY_RE = re.compile(
    r"""^-\s+\[x\]\s+(?P<ts>\d{4}-\d{2}-\d{2}\ \d{2}:\d{2}):\s+
        (?P<title>.+?)\s*$
    """,
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Input sanitization
# ---------------------------------------------------------------------------

# ANSI escape sequences, ordered most-specific first:
# - CSI:  ESC [ <params/intermediates> <final byte 0x40-0x7E>
# - OSC:  ESC ] ... <BEL | ST(ESC \)>      (window title, hyperlinks)
# - DCS/SOS/PM/APC: ESC P/^/_ ... ESC \    (string sequences)
# - Charset designation: ESC ( B, ESC ) 0, ESC # 3, ESC % G, ...
# - Any other 2-byte ESC escape (ESC 7, ESC M, ESC =, ...)
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-9:;<=>?]*[ -/]*[@-~]"          # CSI sequences
    r"|\].*?(?:\x07|\x1b\\)"              # OSC sequences
    r"|[PX^_].*?\x1b\\"                    # DCS / SOS / PM / APC
    r"|[()#%*+./][0-9A-Za-z]?"            # charset & line-size escapes
    r"|."                                  # any remaining 2-byte escape
    r")"
)

# Non-printable control characters: C0 (except TAB/LF/CR, which are
# normalized as whitespace below), DEL, and the C1 range.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")


def sanitize_task_text(text: str) -> str:
    """Sanitize text before it is persisted to PLAN.md.

    1. Strips ANSI escape sequences (colors, cursor movement, OSC
       hyperlinks/titles, ...) so terminal output pasted via
       ``ck add`` can never be written into PLAN.md.
    2. Removes non-printable control characters (C0/C1/DEL).
    3. Normalizes whitespace: every run (tabs, newlines, repeated
       spaces) collapses to a single ASCII space; ends are trimmed.

    A trailing UUID comment (``<!-- id: ... -->``) is stripped so
    user-typed text can never forge or duplicate the structural
    reference ck itself writes. Idempotent: sanitizing already-clean
    text is a no-op. Returns ``""`` for input that is empty or
    consists only of noise.
    """
    if not text:
        return ""
    no_ansi = _ANSI_ESCAPE_RE.sub("", text)
    no_control = _CONTROL_CHARS_RE.sub("", no_ansi)
    collapsed = " ".join(no_control.split())
    # User text must never carry a structural UUID comment: ck owns
    # that reference and re-attaches it at render time.
    return _TASK_UUID_RE.sub("", collapsed).rstrip()


# ---------------------------------------------------------------------------
# Auto-repair of corrupted PLAN.md documents
# ---------------------------------------------------------------------------


def repair_plan_text(text: str) -> str:
    """Filter corrupted ANSI/artifact lines out of a PLAN.md document.

    Pure function: never performs I/O, never raises.

    Per-line policy:

    - Clean lines are preserved VERBATIM (including blank lines and
      ordinary prose).
    - Task lines whose titles carry escape/control noise are repaired
      in place (the sanitized title is kept, the original marker
      syntax is preserved). Tasks whose titles are pure noise are
      dropped.
    - Headers are repaired the same way.
    - Any OTHER line containing ANSI escapes, orphaned control
      characters, or mid-line carriage returns is treated as pasted
      terminal output / structural noise and REMOVED.

    Idempotent: repairing an already-clean document returns it
    unchanged (fast path).
    """
    if not text:
        return text
    if (
        not _ANSI_ESCAPE_RE.search(text)
        and not _CONTROL_CHARS_RE.search(text)
        # A CR that is not part of a CRLF pair is terminal noise
        # (progress bars, ^M artifacts, mixed line endings).
        and text.count("\r") <= text.count("\r\n")
    ):
        return text

    # Preserve the document's dominant line ending (same rule the
    # parser uses) so a CRLF file stays a CRLF file after repair.
    crlf = text.count("\r\n")
    lf_only = text.count("\n") - crlf
    nl = "\r\n" if crlf > lf_only else "\n"

    out: list[str] = []
    for line in text.split(nl):
        repaired = _repair_line(line)
        if repaired is not None:
            out.append(repaired)
    return nl.join(out)


def _repair_line(line: str):
    """Repair a single source line.

    Returns the repaired line, or ``None`` when the line is an
    artifact (pure noise, or a task/header whose text did not
    survive the cleanup) and must be dropped.
    """
    # Trailing CRs are line-ending leftovers, not content noise.
    core = line.rstrip("\r")
    has_noise = (
        _ANSI_ESCAPE_RE.search(core) is not None
        or _CONTROL_CHARS_RE.search(core) is not None
        or "\r" in core  # mid-line CR = terminal artifact
    )
    if not has_noise:
        return line

    stripped = _CONTROL_CHARS_RE.sub("", _ANSI_ESCAPE_RE.sub("", core))

    # A noise-bearing line may still be a real task (e.g. wrapped in
    # color codes) — recover it instead of dropping it. The stable
    # UUID comment is structural data: captured before sanitization
    # and re-attached to the repaired line.
    task_uuid = ""
    uuid_match = _TASK_UUID_RE.search(core)
    if uuid_match is not None:
        task_uuid = uuid_match.group("uuid")
        core = core[:uuid_match.start()].rstrip()

    stripped = _CONTROL_CHARS_RE.sub("", _ANSI_ESCAPE_RE.sub("", core))

    def _emit(marker: str, title: str, legacy_form: bool = False):
        text = sanitize_task_text(title)
        if not text:
            return None
        line = (f"- [{marker}] {text}" if not legacy_form
                else f"- [] {text}")
        if task_uuid:
            line += f" <!-- id: {task_uuid} -->"
        return line

    task = (
        _CANONICAL_TASK_RE.match(stripped)
        or _LEGACY_FOCUSED_DONE_RE.match(stripped)
    )
    if task:
        return _emit(task.group("marker"), task.group("title"))

    legacy = _LEGACY_OPEN_RE.match(stripped)
    if legacy:
        return _emit("", legacy.group("title"), legacy_form=True)

    header = _HEADER_RE.match(stripped)
    if header:
        title = sanitize_task_text(header.group("title"))
        if not title:
            return None
        return f"{header.group('level')} {title}"

    # Non-task, non-header line carrying escape/control noise: this
    # is pasted command output or a corrupted fragment — remove it.
    return None


# ---------------------------------------------------------------------------
# Pure parser
# ---------------------------------------------------------------------------


def parse_plan(text: str) -> TaskList:
    """Parse PLAN.md text into a TaskList AST.

    PURE: does not mutate state, does not normalize duplicate focus
    markers, does not perform any I/O. The returned ``TaskList`` is
    a faithful representation of what's on disk, including any
    duplicate ``[>]`` markers and any legacy ``[]`` open tasks.

    v0.13.0 extensions:

    - The stable reference comment (``<!-- id: a1b2c3d4 -->``) at the
      end of a task line is captured into ``task.uuid`` and stripped
      from the title, so re-rendering round-trips byte-identically.
    - ``[~]`` tasks parse as :attr:`TaskStatus.PAUSED` (v0.13.0
      paused state).
    - ``* Note (...)`` lines directly beneath a task bullet attach to
      that task as inline note entries — they never become sections
      or standalone lines.
    - Indented ``> text`` lines directly beneath a task bullet (or
      beneath its note/description lines) attach to that task as the
      multiline ``description`` (v0.13.4); a column-0 ``>`` blockquote
      is prose and stays unowned.
    - A trailing ``@due(YYYY-MM-DD)`` marker on a task title is
      extracted into the ``due`` field (v0.13.5); mid-title or
      calendar-invalid markers stay plain title text.

    The ORIGINAL line endings (CRLF vs LF) are recorded on the
    TaskList (``newline``) so ``render_plan`` can emit a byte-ident
    document instead of silently rewriting a CRLF file to LF.
    """
    tasks: list[Task] = []
    sections: list[Section] = []
    current_section = ""
    next_id = 1
    # True while the previous non-empty line belonged to the current
    # task block (the bullet itself, an owned note or description
    # line) — the window in which ``> `` lines attach as description.
    in_task_block = False

    # Detect the dominant line ending so the renderer can preserve
    # the file's existing format (M-4 fidelity).
    crlf = text.count("\r\n")
    lf_only = text.count("\n") - crlf
    newline = "\r\n" if crlf > lf_only else "\n"

    for idx, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line:
            continue

        header = _HEADER_RE.match(line)
        if header:
            current_section = header.group("title").strip()
            sections.append(Section(
                title=current_section,
                level=len(header.group("level")),
                line_number=idx,
            ))
            in_task_block = False
            continue

        # Multiline task description (v0.13.4): indented ``> text``
        # lines directly beneath a task bullet (or beneath another
        # owned note/description line of the same task). Column-0
        # blockquotes are user prose and stay unowned.
        desc_match = _DESC_LINE_RE.match(line)
        if desc_match and tasks and in_task_block:
            chunk = desc_match.group("text").rstrip()
            prev = tasks[-1].description
            tasks[-1].description = f"{prev}\n{chunk}" if prev else chunk
            continue

        # Inline note under the most recent task bullet: attached to
        # it (never a section, never a standalone node). Lines that
        # appear before ANY task stay verbatim prose (unowned).
        note_match = _INLINE_NOTE_RE.match(line)
        if note_match and tasks:
            stamp = (note_match.group("stamp") or "").strip()
            note_text = note_match.group("text").strip()
            if note_text:
                tasks[-1].notes.append({
                    "text": note_text,
                    "created": _stamp_to_iso(stamp),
                })
                # in_task_block stays as it was: a note INSIDE a
                # contiguous task block keeps it alive for a following
                # description; an orphan note after prose does not.
                continue

        canonical = _CANONICAL_TASK_RE.match(line)
        if canonical:
            marker = canonical.group("marker")
            title, uuid = _split_uuid_comment(canonical.group("title"))
            title, due = _split_due_marker(title)
            status = TaskStatus.from_marker(marker)
            tasks.append(_make_task(
                next_id, status, title, idx, marker, current_section, False,
                uuid=uuid, due=due,
            ))
            next_id += 1
            in_task_block = True
            continue

        legacy = _LEGACY_OPEN_RE.match(line)
        if legacy:
            title, uuid = _split_uuid_comment(legacy.group("title"))
            title, due = _split_due_marker(title)
            tasks.append(_make_task(
                next_id, TaskStatus.OPEN, title, idx, "", current_section,
                True, uuid=uuid, due=due,
            ))
            next_id += 1
            in_task_block = True
            continue

        legacy_fd = _LEGACY_FOCUSED_DONE_RE.match(line)
        if legacy_fd:
            marker = legacy_fd.group("marker")
            title, uuid = _split_uuid_comment(legacy_fd.group("title"))
            title, due = _split_due_marker(title)
            status = TaskStatus.from_marker(marker)
            tasks.append(_make_task(
                next_id, status, title, idx, marker, current_section, False,
                uuid=uuid, due=due,
            ))
            next_id += 1
            in_task_block = True
            continue

        # Any other non-empty line (prose) ends the task block: a
        # following ``>`` line is a blockquote, not a description.
        in_task_block = False

    return TaskList(
        tasks=tasks, sections=sections, source_text=text, newline=newline
    )


def _split_uuid_comment(title: str) -> tuple[str, str]:
    """Split a trailing ``<!-- id: ... -->`` comment off a task title.

    Returns ``(clean_title, uuid)``; ``uuid`` is "" when the line
    carries no reference comment (legacy content keeps parsing).
    """
    match = _TASK_UUID_RE.search(title)
    if match is None:
        return title.strip(), ""
    clean = title[:match.start()].rstrip()
    return clean, match.group("uuid")


def parse_due(due: str) -> Optional[date]:
    """Validate an ISO ``YYYY-MM-DD`` due date; ``None`` when invalid.

    Strict on BOTH the shape (``\\d{4}-\\d{2}-\\d{2}``) and the
    calendar (:func:`datetime.date.fromisoformat`), so ``2026-02-30``
    is rejected together with looser forms like ``2026-2-1``.
    """
    text = (due or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _split_due_marker(title: str) -> tuple[str, str]:
    """Split a trailing ``@due(YYYY-MM-DD)`` marker off a task title.

    Returns ``(clean_title, due)``; ``due`` is "" when no valid marker
    trails the title. Only a CALENDAR-VALID date is extracted — a junk
    marker (``@due(2026-02-30)``) stays plain title text so user
    content is never silently eaten.
    """
    match = _TASK_DUE_RE.search(title)
    if match is None:
        return title.strip(), ""
    due = match.group("date")
    if parse_due(due) is None:
        return title.strip(), ""
    return title[:match.start()].rstrip(), due


def _stamp_to_iso(stamp: str) -> str:
    """Convert an inline note stamp to an ISO ``created`` value.

    Accepts ``YYYY-MM-DD HH:MM`` (optionally followed by `` UTC``).
    Anything else round-trips verbatim so hand-written values are
    never mangled.
    """
    stamp = (stamp or "").strip()
    if not stamp:
        return ""
    probe = stamp[:-4].strip() if stamp.endswith(" UTC") else stamp
    try:
        parsed = datetime.strptime(probe, "%Y-%m-%d %H:%M")
    except ValueError:
        return stamp
    return parsed.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


def _make_task(task_id, status, title, line_number, marker_raw,
               section, legacy, *, uuid: str = "", due: str = "") -> Task:
    return Task(
        id=task_id,
        status=status,
        title=title,
        line_number=line_number,
        marker_raw=marker_raw,
        section=section,
        legacy_syntax=legacy,
        uuid=uuid,
        due=due,
    )


def parse_plan_file(path: Path | str) -> TaskList:
    """Read ``path`` and parse it. Returns an empty TaskList if missing.

    The file is opened with ``newline=""`` so the ORIGINAL line
    endings (CRLF vs LF) reach the parser intact — the default
    text-mode read would translate CRLF to LF and the renderer
    would then silently rewrite the whole file to LF.
    """
    p = Path(path)
    if not p.exists():
        return TaskList()
    with open(p, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    return parse_plan(text)


def load_repaired_plan(path: Path | str) -> Tuple[TaskList, bool]:
    """Read + auto-repair a PLAN.md file.

    Returns ``(task_list, repaired)`` where ``repaired`` is True when
    the on-disk text contained corruption (ANSI escapes, orphaned
    control sequences, artifact lines) and the cleaned text was
    ATOMICALLY WRITTEN BACK to disk (self-healing parse). Clean
    files are left untouched (no write, no lock churn).

    The repair-persist cycle is the read side of ``ck add`` and other
    mutations: every command that mutates PLAN.md heals any existing
    corruption as part of its read-modify-write pass, under the same
    file lock the caller's write will use.
    """
    p = Path(path)
    if not p.exists():
        return TaskList(), False
    with open(p, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    repaired_text = repair_plan_text(text)
    if repaired_text == text:
        return parse_plan(text), False
    _atomic_write_text(p, repaired_text)
    return parse_plan(repaired_text), True


# ---------------------------------------------------------------------------
# Normalization (explicit, opt-in)
# ---------------------------------------------------------------------------


def normalize_plan(task_list: TaskList) -> TaskList:
    """Mutate the AST in-memory to fix structural anomalies.

    - Demote all but the first ``[>]`` marker to ``OPEN``.
    - Lift ``legacy_syntax=False`` for all tasks.

    Returns the same ``task_list`` for chaining.
    """
    seen_focus = False
    for t in task_list.tasks:
        if t.status == TaskStatus.FOCUSED:
            if seen_focus:
                t.status = TaskStatus.OPEN
            else:
                seen_focus = True
        t.legacy_syntax = False
    return task_list


# ---------------------------------------------------------------------------
# Renderer (full-file deterministic)
# ---------------------------------------------------------------------------


def render_plan(task_list: TaskList) -> str:
    """Render the AST to a complete PLAN.md document.

    Strategy:

    1. If the AST has no source and no tasks → ``""``.
    2. If the AST has tasks but no source → flat list.
    3. Otherwise: walk the original source line by line, substituting
       freshly-rendered task lines **only where the original source
       line is itself a task line** (headers, prose and blanks are
       never overwritten). New tasks whose ``line_number`` falls past
       the original EOF are appended just before the ``## Completed``
       header (or at EOF). Duplicate ``line_number`` claims raise
       ``ValueError`` instead of silently dropping a task.
    """
    if not task_list.source_text and not task_list.tasks:
        return ""

    if not task_list.source_text:
        return _emit_flat(task_list)

    return _emit_with_source(task_list)


def _emit_flat(tl: TaskList) -> str:
    if not tl.tasks:
        return ""
    nl = getattr(tl, "newline", "\n") or "\n"
    return nl.join(t.to_line() for t in tl.tasks) + nl


def _emit_with_source(tl: TaskList) -> str:
    src_lines = tl.source_text.splitlines()
    max_src_line = len(src_lines)
    nl = getattr(tl, "newline", "\n") or "\n"

    # DELETED source lines (user-facing `ck delete`): drop them from
    # the document. Inline NOTE lines are owned by the task they
    # follow: the AST carries the authoritative note entries, so the
    # stale on-disk note lines are dropped too and re-emitted fresh
    # from the AST (mutation of notes must never duplicate blocks).
    # ``pos_map`` translates an ORIGINAL 1-based line number to its
    # position in the surviving list, so substitution and insertion
    # hints keep working on the reduced document.
    removed = {ln for ln in getattr(tl, "removed_lines", ()) or ()
               if isinstance(ln, int)} | _owned_note_lines(src_lines) \
        | _owned_desc_lines(src_lines)
    out: list[str] = []
    pos_map: dict[int, int] = {}
    for old_pos, raw in enumerate(src_lines, start=1):
        if old_pos in removed:
            continue
        out.append(raw)
        pos_map[old_pos] = len(out)

    # Collision guard: two AST nodes sharing a source line_number
    # would silently overwrite each other during substitution.
    # This is a structural invariant violation — refuse to render a
    # lossy document.
    seen_lines: set[int] = set()
    for t in tl.tasks:
        if t.line_number <= max_src_line and t.insert_line is None:
            if t.line_number in removed:
                raise ValueError(
                    f"Task {t.id!r} claims deleted line {t.line_number}; "
                    "refusing to render (would resurrect removed content)"
                )
            mapped = pos_map.get(t.line_number)
            if mapped is None:
                raise ValueError(
                    f"Task {t.id!r} claims line {t.line_number}, which "
                    "is not in the source document"
                )
            if mapped in seen_lines:
                raise ValueError(
                    f"Two tasks share line_number {t.line_number}; "
                    "refusing to render (would silently drop one)"
                )
            seen_lines.add(mapped)

    pending_append: list[Task] = []
    for t in tl.tasks:
        if t.line_number > max_src_line:
            pending_append.append(t)

    # First pass: substitute IN-PLACE only where the ORIGINAL source
    # line is itself a task line. This guarantees a task node can
    # never overwrite a header, prose, or blank line — non-task
    # source lines are structurally out of bounds for substitution.
    # Non-task lines are preserved VERBATIM (including any trailing
    # whitespace) so the render never reflows the user's document.
    for t in tl.tasks:
        if 1 <= t.line_number <= max_src_line and t.insert_line is None:
            target = pos_map[t.line_number] - 1
            original = out[target]
            if not _is_task_line(original):
                raise ValueError(
                    f"Task {t.id!r} claims line {t.line_number} but the "
                    f"source line is not a task ({original!r}); refusing "
                    "to overwrite non-task content"
                )
            out[target] = t.to_line()

    # Note pass: inline notes belong DIRECTLY under their task line.
    # Tasks are processed in REVERSE document position so an insertion
    # can never shift a later substitution target. Every re-emitted
    # block is recorded (owner original 1-based line, line count) so
    # the NEW-task insertion hint below can account for the growth.
    reemitted_notes: list[tuple[int, int]] = []
    tasks_with_position: list[tuple[int, Task]] = []
    for t in tl.tasks:
        if t.insert_line is None and 1 <= t.line_number <= max_src_line:
            tasks_with_position.append((pos_map[t.line_number] - 1, t))
        elif t.insert_line is not None or t.line_number > max_src_line:
            # Past-EOF/insert-hinted tasks are appended below together
            # with their notes.
            tasks_with_position.append((None, t))
    for target, t in sorted(
            (pair for pair in tasks_with_position if pair[0] is not None),
            key=lambda pair: pair[0], reverse=True):
        # The inline block lives DIRECTLY under the task line:
        # description first, then the note entries.
        block = [*render_task_description(t), *render_task_notes(t)]
        if block:
            out[target + 1:target + 1] = block
            reemitted_notes.append((t.line_number, len(block)))

    # Second pass: NEW tasks. Tasks carrying an explicit ``insert_line``
    # hint are inserted at that 1-based source position (the end of
    # ``## Current Sprint`` — computed by ``_sprint_insert_line`` and
    # validated in core). Other past-EOF tasks fall back to just
    # before the ``## Completed`` header (or at EOF), never
    # replacing content.
    if pending_append:
        insert_at = _insert_position_for(
            pending_append[0], out, max_src_line, pos_map=pos_map,
            reemitted_notes=reemitted_notes)
        for t in pending_append:
            block = [t.to_line(), *render_task_description(t),
                     *render_task_notes(t)]
            out[insert_at:insert_at] = block
            insert_at += len(block)

    rendered = nl.join(out)
    if not rendered.endswith(nl):
        rendered += nl
    return rendered


def render_task_description(task: Task) -> list[str]:
    """The indented ``> text`` description lines for one task bullet.

    Each description line renders as ``  > <text>`` (two-space
    indent); an empty description line renders as the bare ``  >``
    (a paragraph break inside the block), so a multiline description
    round-trips byte-identically. An empty description emits nothing.
    """
    if not task.description:
        return []
    lines: list[str] = []
    for chunk in task.description.split("\n"):
        lines.append(f"  > {chunk}" if chunk else "  >")
    return lines


def render_task_notes(task: Task) -> list[str]:
    """The inline ``* Note (...)`` lines for one task bullet.

    Format (spec-exact, two-space indent):
    ``  * Note (2026-11-04 14:32 UTC): waiting on CI``. Entries with
    no timestamp render the legacy bare form ``  * Note: <text>``.
    """
    lines: list[str] = []
    for entry in task.notes or ():
        if isinstance(entry, dict):
            text = entry.get("text")
            created = entry.get("created", "")
        else:
            text, created = entry, ""
        if not isinstance(text, str) or not text.strip():
            continue
        stamp = note_stamp(created) if created else ""
        suffix = f" ({stamp})" if stamp else ""
        lines.append(f"  * Note{suffix}: {text.strip()}")
    return lines


def _owned_note_lines(src_lines: list[str]) -> set[int]:
    """1-based source positions of INLINE NOTE lines owned by a task.

    A ``* Note ...`` line directly beneath a task bullet (or beneath
    another owned note line) belongs to that task's render block: the
    renderer drops these positions and re-emits the notes from the
    AST, so editing notes never duplicates stale blocks. Note lines
    with NO preceding task (orphan prose) are NOT owned — they stay
    verbatim.
    """
    owned: set[int] = set()
    after_task = False
    for pos, raw in enumerate(src_lines, start=1):
        line = raw.rstrip()
        if not line:
            continue
        if _INLINE_NOTE_RE.match(line) and after_task:
            owned.add(pos)
            continue
        if _DESC_LINE_RE.match(line) and after_task:
            # An owned description line keeps the task block alive
            # for a note that follows it.
            continue
        after_task = _is_task_line(line)
    return owned


def _owned_desc_lines(src_lines: list[str]) -> set[int]:
    """1-based source positions of DESCRIPTION lines owned by a task.

    Same contiguous-block walk as :func:`_owned_note_lines`, extended
    to the v0.13.4 indented ``> text`` description lines: a ``>``
    line belongs to the task block when it follows the bullet, another
    owned description line, or an owned note line. Column-0
    blockquotes and ``>`` lines after unrelated prose are NOT owned
    (they stay verbatim). The renderer drops these positions and
    re-emits the description from the AST, so editing a description
    never duplicates stale blocks.
    """
    owned: set[int] = set()
    after_task = False
    for pos, raw in enumerate(src_lines, start=1):
        line = raw.rstrip()
        if not line:
            continue
        if _DESC_LINE_RE.match(line) and after_task:
            owned.add(pos)
            continue
        if _INLINE_NOTE_RE.match(line) and after_task:
            # An owned note line keeps the task block alive for a
            # description that follows it.
            continue
        after_task = _is_task_line(line)
    return owned


def _insert_position_for(task: Task, out: list[str],
                         max_src_line: int,
                         *, pos_map: dict | None = None,
                         reemitted_notes=None) -> int:
    """0-based insertion index for the first new (past-EOF) task.

    A new task with an ``insert_line`` hint (end of ``## Current
    Sprint``) is inserted at its hinted position. When the hint is
    missing, stale, or points outside the document, fall back to just
    before ``## Completed`` (or EOF) so insertion is always safe.

    ``pos_map`` (original line -> surviving line) keeps the hint
    correct on a document shortened by ``ck delete``.
    ``reemitted_notes`` (owner original 1-based line -> note-line
    count) keeps the hint correct on a document that grew back: the
    note pass re-emits INLINE note blocks that ``pos_map`` knows
    nothing about, and every block whose owner precedes the hint
    shifts the insertion point down.
    """
    hint = task.insert_line
    if hint is not None and 1 <= hint <= max_src_line + 1:
        if pos_map is None:
            insert_at = hint - 1
        else:
            # Insert before the hint: count the surviving source lines
            # that precede it (removed lines never occupy a slot).
            insert_at = sum(1 for old in pos_map if old < hint)
        # Re-emitted inline note blocks are real output lines the
        # source-line count misses; owners before the hint push it.
        insert_at += sum(
            count for owner, count in (reemitted_notes or ())
            if owner < hint)
        return insert_at
    completed = _find_completed_header(out)
    if completed is not None:
        return completed
    return len(out)


def _is_task_line(line: str) -> bool:
    """True if ``line`` parses as a task bullet in any accepted syntax."""
    return bool(
        _CANONICAL_TASK_RE.match(line)
        or _LEGACY_OPEN_RE.match(line)
        or _LEGACY_FOCUSED_DONE_RE.match(line)
    )


def _find_completed_header(lines: list[str]) -> int | None:
    """0-based index of a ``## Completed`` header, if the doc has one.

    v0.13.0 plans hold no ``## Completed`` section (completed tasks
    live in HISTORY.md), but the fallback still matters for LEGACY
    documents and global space files, which keep completed work in
    place: a past-EOF task must be inserted before that header, not
    at EOF where it would visually join the completed block.
    """
    for i, raw in enumerate(lines):
        if _HEADER_RE.match(raw):
            title = _HEADER_RE.match(raw).group("title").strip().lower()
            if title == "completed":
                return i
    return None


def _find_section_end(lines: list[str], title: str) -> int | None:
    """0-based index one PAST the last content line of a section.

    ``title`` is matched case-insensitively against ``##`` headers
    (any heading level is accepted). The end of a section is the
    first line of the NEXT header at any level, or EOF. Trailing
    blank lines inside the section are kept OUT of the insertion
    point so new tasks land directly beneath the last task, not
    after a blank gap.
    """
    target = title.strip().lower()
    if not target:
        return None
    in_target = False
    end = None
    for i, raw in enumerate(lines):
        header = _HEADER_RE.match(raw)
        if header:
            if in_target:
                # Next header ends the section.
                end = i
                break
            if header.group("title").strip().lower() == target:
                in_target = True
        elif in_target:
            end = i + 1
    if not in_target:
        return None
    if end is None:
        return len(lines)
    # Trim trailing blank lines from the section body.
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    return end


# ---------------------------------------------------------------------------
# HISTORY.md: completed-task ledger (v0.13.0)
# ---------------------------------------------------------------------------


HISTORY_SECTION_HEADER = "## Completed"


def parse_history(text: str) -> list[HistoryEntry]:
    """Parse the completed-task entries out of a HISTORY.md document.

    Recognizes BOTH entry shapes so legacy and v0.13 files read the
    same way:

    - ``- [x] 2026-11-04 16:45: write migration <!-- id: m3n4o5p6 -->``
      (v0.13 completed-task ledger line);
    - ``### 2026-11-04 16:45 | [3] write migration`` (pre-0.13
      journal/note block headers), with their ``* Note`` lines.

    Entries appear in document order — for the ``ck st`` tail view
    the caller takes the LAST N.
    """
    entries: list[HistoryEntry] = []
    current: HistoryEntry | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        header = _HEADER_RE.match(line)
        if header and header.group("title").strip().lower() == "completed":
            current = None  # section header itself is not an entry
            continue
        legacy = re.match(r"^###\s+(.+?)\s*\|\s*(.+?)\s*$", line)
        if legacy:
            ts = legacy.group(1).strip()
            ref = legacy.group(2).strip()
            ref_match = re.match(r"^\[(\d+)\]\s+(.+?)\s*$", ref)
            if ref_match:
                title = ref_match.group(2).strip()
            else:
                title = ref
            current = HistoryEntry(timestamp=ts, title=title)
            entries.append(current)
            continue
        completed = _HISTORY_ENTRY_RE.match(line)
        if completed:
            title, uuid = _split_uuid_comment(completed.group("title"))
            entries.append(HistoryEntry(
                timestamp=completed.group("ts"),
                title=title,
                uuid=uuid,
            ))
            current = None  # * Note lines under a [x] row are ignored
            continue
        if current is not None:
            note_match = _INLINE_NOTE_RE.match(line)
            if note_match:
                stamp = (note_match.group("stamp") or "").strip()
                note_text = note_match.group("text").strip()
                if note_text:
                    current.notes.append({
                        "text": note_text,
                        "created": _stamp_to_iso(stamp),
                    })
    return entries


def parse_history_file(path: Path | str) -> list[HistoryEntry]:
    """Read ``path`` and parse its completed-task entries ([] if missing)."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    return parse_history(text)


def render_history(entries: list[HistoryEntry]) -> str:
    """Render a complete HISTORY.md document (``## Completed`` ledger)."""
    lines = [HISTORY_SECTION_HEADER]
    for entry in entries:
        lines.append(entry.to_line())
        lines.extend(render_history_notes(entry))
    return "\n".join(lines) + "\n"


def render_history_notes(entry: HistoryEntry) -> list[str]:
    """Inline note lines under one completed entry (two-space indent)."""
    lines: list[str] = []
    for note in entry.notes or ():
        text = note.get("text") if isinstance(note, dict) else note
        created = note.get("created", "") if isinstance(note, dict) else ""
        if not isinstance(text, str) or not text.strip():
            continue
        stamp = note_stamp(created) if created else ""
        suffix = f" ({stamp})" if stamp else ""
        lines.append(f"  * Note{suffix}: {text.strip()}")
    return lines


def append_to_history(path: Path | str, timestamp: str, title: str,
                      uuid: str = "", notes: list | None = None) -> None:
    """Append one completed task to HISTORY.md (atomic, under lock).

    The ``## Completed`` ledger section is created at the top of the
    file when absent (legacy journal blocks below it stay intact);
    the new entry is appended at the END of that section so the
    ledger stays chronological. ``notes`` are inlined beneath the
    entry so a completed task's process context graduates into the
    durable record instead of vanishing with the plan.
    """
    from .config import file_lock

    entry = HistoryEntry(timestamp=timestamp, title=title, uuid=uuid)
    if notes:
        entry.notes = list(notes)
    p = Path(path)
    if p.exists():
        lines = p.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    block = [entry.to_line(), *render_history_notes(entry)]
    existing_header = HISTORY_SECTION_HEADER in (
        ln.strip() for ln in lines)
    if existing_header:
        # Insert before the first header that FOLLOWS the Completed
        # section (or at EOF) — i.e. at the end of the ledger.
        insert_at = len(lines)
        in_completed = False
        for i, raw in enumerate(lines):
            header = _HEADER_RE.match(raw)
            if header:
                title_l = header.group("title").strip().lower()
                if not in_completed and title_l == "completed":
                    in_completed = True
                    continue
                if in_completed:
                    insert_at = i
                    break
        lines[insert_at:insert_at] = block
    else:
        lines = [HISTORY_SECTION_HEADER, *block, "", *lines]
        # No trailing gap when the legacy content is empty.
        while len(lines) > len(block) + 2 and not lines[-1].strip():
            lines.pop()
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    with file_lock(p):
        _atomic_write_text(p, text)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def write_plan(path: Path | str, task_list: TaskList) -> None:
    """Atomic write of PLAN.md: write to tmp + rename.

    Always writes the canonical CommonMark form. Caller is expected
    to have already mutated the AST and (if desired) called
    ``normalize_plan``.
    """
    p = Path(path)
    text = render_plan(task_list)
    _atomic_write_text(p, text)


def _resolve_symlink_target(path: Path) -> Path:
    """If ``path`` is a symlink, return its (absolute) target.

    Atomic replace on a symlink path would swap the *link* for a
    regular file, silently breaking dotfile-managed setups. Writing
    to the resolved target keeps the user's link intact.
    """
    try:
        if path.is_symlink():
            return path.resolve()
    except OSError:
        pass
    return path


def _atomic_write_text(path: Path, text: str) -> None:
    """Durable atomic write: tmp file + fsync + rename.

    - DEV MODE: ``path`` is first passed through the sandbox
      interceptor (``resolve_write_path``) so PLAN.md / .ck/ writes
      land under ``.sandbox/`` while production stays immutable.
      Reads are NOT affected — only this write path is redirected.
    - Target symlinks are resolved first (the link survives).
    - The original file's permissions are preserved (mkstemp's 0600
      would otherwise demote e.g. 0644 files on every write).
    - Data is flushed and fsync'ed before the rename so a crash
      cannot leave a renamed-but-empty file.
    """
    target = path
    try:
        from .sandbox import is_dev_mode, resolve_write_path
        if is_dev_mode():
            target = resolve_write_path(path)
    except Exception:
        target = path  # interception must never break a real write
    real = _resolve_symlink_target(target)
    real.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = os.stat(real).st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp_name = tempfile.mkstemp(
        dir=str(real.parent), prefix=".ck-plan-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, real)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


__all__ = [
    "parse_plan",
    "parse_plan_file",
    "load_repaired_plan",
    "normalize_plan",
    "render_plan",
    "write_plan",
    "parse_history",
    "parse_history_file",
    "render_history",
    "render_history_notes",
    "render_task_description",
    "render_task_notes",
    "append_to_history",
    "sanitize_task_text",
    "parse_due",
    "repair_plan_text",
    "_find_completed_header",
    "_find_section_end",
]