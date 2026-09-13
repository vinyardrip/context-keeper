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
from pathlib import Path
from typing import Tuple

from .models import Section, Task, TaskList, TaskStatus


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Canonical CommonMark: "- [ ]", "- [>]", "- [x]"
_CANONICAL_TASK_RE = re.compile(
    r"""^-\s+\[(?P<marker>[ >x])\]\s+(?P<title>.+?)\s*$
    """,
    re.VERBOSE,
)

# Legacy no-space form for OPEN: "- []"
_LEGACY_OPEN_RE = re.compile(
    r"""^-\s+\[\]\s+(?P<title>.+?)\s*$
    """,
    re.VERBOSE,
)

# Fallback: "- [>]" / "- [x]" (FOCUSED / DONE are unambiguous; only
# OPEN has a non-space form).
_LEGACY_FOCUSED_DONE_RE = re.compile(
    r"""^-\s+\[(?P<marker>>|x|X)\]\s+(?P<title>.+?)\s*$
    """,
    re.VERBOSE,
)

_HEADER_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*$")


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

    Idempotent: sanitizing already-clean text is a no-op. Returns
    ``""`` for input that is empty or consists only of noise.
    """
    if not text:
        return ""
    no_ansi = _ANSI_ESCAPE_RE.sub("", text)
    no_control = _CONTROL_CHARS_RE.sub("", no_ansi)
    return " ".join(no_control.split())


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
    # color codes) — recover it instead of dropping it.
    task = (
        _CANONICAL_TASK_RE.match(stripped)
        or _LEGACY_FOCUSED_DONE_RE.match(stripped)
    )
    if task:
        title = sanitize_task_text(task.group("title"))
        if not title:
            return None
        return f"- [{task.group('marker')}] {title}"

    legacy = _LEGACY_OPEN_RE.match(stripped)
    if legacy:
        title = sanitize_task_text(legacy.group("title"))
        if not title:
            return None
        return f"- [] {title}"

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

    The ORIGINAL line endings (CRLF vs LF) are recorded on the
    TaskList (``newline``) so ``render_plan`` can emit a byte-ident
    document instead of silently rewriting a CRLF file to LF.
    """
    tasks: list[Task] = []
    sections: list[Section] = []
    current_section = ""
    next_id = 1

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
            continue

        canonical = _CANONICAL_TASK_RE.match(line)
        if canonical:
            marker = canonical.group("marker")
            title = canonical.group("title").strip()
            status = TaskStatus.from_marker(marker)
            tasks.append(_make_task(
                next_id, status, title, idx, marker, current_section, False
            ))
            next_id += 1
            continue

        legacy = _LEGACY_OPEN_RE.match(line)
        if legacy:
            title = legacy.group("title").strip()
            tasks.append(_make_task(
                next_id, TaskStatus.OPEN, title, idx, "", current_section, True
            ))
            next_id += 1
            continue

        legacy_fd = _LEGACY_FOCUSED_DONE_RE.match(line)
        if legacy_fd:
            marker = legacy_fd.group("marker")
            title = legacy_fd.group("title").strip()
            status = TaskStatus.from_marker(marker)
            tasks.append(_make_task(
                next_id, status, title, idx, marker, current_section, False
            ))
            next_id += 1
            continue

    return TaskList(
        tasks=tasks, sections=sections, source_text=text, newline=newline
    )


def _make_task(task_id, status, title, line_number, marker_raw,
               section, legacy) -> Task:
    return Task(
        id=task_id,
        status=status,
        title=title,
        line_number=line_number,
        marker_raw=marker_raw,
        section=section,
        legacy_syntax=legacy,
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

    # Collision guard: two AST nodes sharing a source line_number
    # would silently overwrite each other during substitution.
    # This is a structural invariant violation — refuse to render a
    # lossy document.
    seen_lines: set[int] = set()
    for t in tl.tasks:
        if t.line_number <= max_src_line and t.insert_line is None:
            if t.line_number in seen_lines:
                raise ValueError(
                    f"Two tasks share line_number {t.line_number}; "
                    "refusing to render (would silently drop one)"
                )
            seen_lines.add(t.line_number)

    out: list[str] = list(src_lines)
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
            original = out[t.line_number - 1]
            if not _is_task_line(original):
                raise ValueError(
                    f"Task {t.id!r} claims line {t.line_number} but the "
                    f"source line is not a task ({original!r}); refusing "
                    "to overwrite non-task content"
                )
            out[t.line_number - 1] = t.to_line()

    # Second pass: NEW tasks. Tasks carrying an explicit ``insert_line``
    # hint are inserted at that 1-based source position (the end of
    # ``## Current Sprint`` — computed by ``_sprint_insert_line`` and
    # validated in core). Other past-EOF tasks fall back to just
    # before the ``## Completed`` header (or at EOF), never
    # replacing content.
    if pending_append:
        insert_at = _insert_position_for(pending_append[0], out, max_src_line)
        for t in pending_append:
            out.insert(insert_at, t.to_line())
            insert_at += 1

    rendered = nl.join(out)
    if not rendered.endswith(nl):
        rendered += nl
    return rendered


def _insert_position_for(task: Task, out: list[str],
                         max_src_line: int) -> int:
    """0-based insertion index for the first new (past-EOF) task.

    A new task with an ``insert_line`` hint (end of ``## Current
    Sprint``) is inserted at its hinted position. When the hint is
    missing, stale, or points outside the document, fall back to just
    before ``## Completed`` (or EOF) so insertion is always safe.
    """
    hint = task.insert_line
    if hint is not None and 1 <= hint <= max_src_line + 1:
        return hint - 1
    completed = _find_completed_line(out)
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


def _find_completed_line(lines: list[str]) -> int | None:
    """Return the 0-based index of the ``## Completed`` header if present."""
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

    - Target symlinks are resolved first (the link survives).
    - The original file's permissions are preserved (mkstemp's 0600
      would otherwise demote e.g. 0644 files on every write).
    - Data is flushed and fsync'ed before the rename so a crash
      cannot leave a renamed-but-empty file.
    """
    real = _resolve_symlink_target(path)
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
    "sanitize_task_text",
    "repair_plan_text",
    "_find_completed_line",
    "_find_section_end",
]