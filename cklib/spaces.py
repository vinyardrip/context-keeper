"""Out-of-project task spaces: ``ck local`` / ``ck remote``.

Unlike a project plan, a *space* is a global, non-project task list
for activity that belongs to no single Git repository:

- ``local``  — the workstation itself (OS config, tooling, sandbox
  reorganisation, …);
- ``remote`` — virtual / remote infrastructure (hosting, VPS,
  Proxmox/KVM, Docker hosts, …).

Storage is deliberately boring: one plain Markdown plan per space at
``~/.config/ck/spaces/<name>.md``, using the exact same
``- [ ]`` / ``- [>]`` / ``- [x]`` task syntax as ``PLAN.md`` so the
same parser and renderer apply. Directories are created lazily — no
``~/.config/ck/spaces`` tree appears until the first ``ck local add``
or ``ck remote add``.

The module is import-light and project-independent: the CLI dispatches
``ck local`` / ``ck remote`` straight here, and the global dashboard
reads :func:`render_system_ops_block` for its ``[SYSTEM / OPS]`` top
section. Every write is atomic and serialised on a sibling ``.lock``
file (fail-closed), mirroring the project-plan write path.
"""

from __future__ import annotations

from pathlib import Path

from . import config
from .config import file_lock
from .models import TaskList, TaskStatus
from .parser import (
    _atomic_write_text,
    parse_plan,
    render_plan,
    sanitize_task_text,
)

# Default scaffold for a space file that does not exist yet. Kept in
# sync with DEFAULT_PLAN's section naming so the renderer's
# ``## Completed`` insertion anchor behaves identically.
_EMPTY_SPACE = "# {space}\n\n## Current Sprint\n\n## Completed\n"


# ---------------------------------------------------------------------- #
# Paths
# ---------------------------------------------------------------------- #

def spaces_dir() -> Path:
    """Directory holding the space files (``~/.config/ck/spaces``).

    Computed on every call from the *current* ``GLOBAL_CONFIG_DIR`` so
    tests that pin a temporary config root (and any future runtime
    relocation) observe the change.
    """
    return config.GLOBAL_CONFIG_DIR / config.SPACES_DIR_NAME


def space_path(space: str) -> Path:
    """Path of ``<space>.md`` (``local.md`` / ``remote.md``)."""
    return spaces_dir() / f"{space}.md"


def is_valid_space(space: str) -> bool:
    """True when ``space`` is one of the known space names."""
    return space in config.SPACE_NAMES


def _read_path(space: str) -> Path:
    """Read-side path, preferring a sandbox copy in dev mode.

    Mirrors the registry / PLAN.md read-your-writes contract: once a
    dev-mode session has written a sandboxed space file, reads come
    from that copy; with no sandbox copy the real file is read. The
    probe uses ``create=False`` so a read never materialises the
    ``.sandbox/`` skeleton.
    """
    path = space_path(space)
    try:
        from .sandbox import dev_context_active, resolve_write_path
        if dev_context_active():
            sandboxed = resolve_write_path(path, create=False, force=True)
            if sandboxed.exists():
                return sandboxed
    except Exception:
        pass
    return path


def _write_path(space: str) -> Path:
    """Write-side path, redirected into the sandbox in dev mode.

    Parent directories are created lazily here — this is the only
    place that ever creates ``~/.config/ck/spaces``, and dev mode
    redirects the creation so the real config tree stays untouched.
    """
    path = space_path(space)
    try:
        from .sandbox import dev_context_active, resolve_write_path
        if dev_context_active():
            path = resolve_write_path(path, force=True)
    except Exception:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------- #
# Read / write
# ---------------------------------------------------------------------- #

def load_space(space: str) -> TaskList:
    """Parse a space into a :class:`TaskList` (empty space when absent).

    A missing or blank file parses to the canonical empty scaffold so
    IDs and section context are stable across the first ``add``.
    """
    path = _read_path(space)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if not text.strip():
        text = _EMPTY_SPACE.format(space=space)
    return parse_plan(text)


def add_space_task(space: str, text: str) -> int:
    """Append a new open task to ``space``; return its numeric ID.

    The title is sanitized the same way project tasks are (ANSI
    escapes / control characters / whitespace runs stripped), the
    file is created lazily, and the write is atomic under a
    fail-closed lock.
    """
    if not is_valid_space(space):
        raise ValueError(f"Unknown space: {space!r}")
    title = sanitize_task_text(text)
    if not title:
        raise ValueError("Task text is empty")

    path = _write_path(space)
    with file_lock(path):
        tl = load_space(space)
        section = _section_for_new_task(tl)
        new_task = tl.add(title, status=TaskStatus.OPEN, section=section)
        _atomic_write_text(path, render_plan(tl))
    return new_task.id


def list_space(space: str) -> str:
    """Render ``space`` as a pipe-friendly task listing.

    Format::

        [local] 1/3 done
        [ ] 1. first open task
        [>] 2. focused task
        [x] 3. done task
    """
    if not is_valid_space(space):
        raise ValueError(f"Unknown space: {space!r}")
    tl = load_space(space)
    header = f"[{space}] {len(tl.done)}/{tl.total} done"
    lines = [header]
    for t in tl.tasks:
        lines.append(f"[{t.status.canonical_marker}] {t.id}. {t.title}")
    return "\n".join(lines)


def _section_for_new_task(tl: TaskList) -> str:
    """Section a new space task belongs to (mirrors core's helper)."""
    for sec in reversed(tl.sections):
        if sec.title.lower() == "completed":
            continue
        return sec.title
    return "Current Sprint"


# ---------------------------------------------------------------------- #
# Dashboard overlay
# ---------------------------------------------------------------------- #

def _space_summary(space: str) -> list[str]:
    """Indented per-space lines: focus task, then open pending tasks."""
    tl = load_space(space)
    lines: list[str] = []
    focused = tl.focused[0] if tl.focused else None
    if focused is not None:
        lines.append(f"    [>] [{focused.id}] {focused.title}")
    pending = [t for t in tl.open if focused is None or t.id != focused.id]
    for t in pending:
        lines.append(f"    [ ] [{t.id}] {t.title}")
    if not lines:
        lines.append("    (empty)")
    return lines


def render_system_ops_block() -> str:
    """Render the fixed ``[SYSTEM / OPS]`` dashboard top section.

    Always emitted (even when both spaces are empty) so the dashboard
    layout is stable::

        [SYSTEM / OPS]
          LOCAL:
            [>] [2] install drivers
            [ ] [3] cleanup _tests
          REMOTE:
            [ ] [1] provision VPS
    """
    out: list[str] = ["[SYSTEM / OPS]"]
    for space in config.SPACE_NAMES:
        out.append(f"  {space.upper()}:")
        out.extend(_space_summary(space))
    return "\n".join(out)


__all__ = [
    "spaces_dir",
    "space_path",
    "is_valid_space",
    "load_space",
    "add_space_task",
    "list_space",
    "render_system_ops_block",
]
