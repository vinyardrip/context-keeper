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
        if 1 <= t.line_number <= max_src_line:
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
        if 1 <= t.line_number <= max_src_line:
            original = out[t.line_number - 1]
            if not _is_task_line(original):
                raise ValueError(
                    f"Task {t.id!r} claims line {t.line_number} but the "
                    f"source line is not a task ({original!r}); refusing "
                    "to overwrite non-task content"
                )
            out[t.line_number - 1] = t.to_line()

    # Second pass: tasks beyond EOF. Insert them just before the
    # "## Completed" header (or at EOF), never replacing content.
    if pending_append:
        insert_at = _find_completed_line(out)
        if insert_at is None:
            insert_at = len(out)
        for t in pending_append:
            out.insert(insert_at, t.to_line())
            insert_at += 1

    rendered = nl.join(out)
    if not rendered.endswith(nl):
        rendered += nl
    return rendered


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
    "normalize_plan",
    "render_plan",
    "write_plan",
    "_find_completed_line",
]