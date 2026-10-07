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
calls :func:`space_snapshot` for its unified ``SPACES (GLOBAL
CONTEXTS)`` table (rendered by ``cklib.core`` through the same grid
pipeline as the Git projects table). Every write is atomic and
serialised on a sibling ``.lock`` file (fail-closed), mirroring the
project-plan write path.
"""

from __future__ import annotations

from datetime import datetime, timezone
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

def space_snapshot(space: str) -> dict:
    """Aggregate the state of ``space`` for the dashboard table.

    Data only — no rendering. ``cklib.core`` feeds these snapshots
    through the SAME ``_render_grid`` pipeline the Git projects table
    uses, so ``LOCAL`` / ``REMOTE`` render as uniform table rows
    (``Space | Focus Task | Progress | Last Active``) instead of an
    unformatted task-list dump.

    Keys:

    - ``name``        — upper-cased space name (``LOCAL`` / ``REMOTE``);
    - ``path``        — the space's Markdown file path (shown on the
                        row's second line, home-contracted by the
                        renderer);
    - ``focus_id`` / ``focus_title`` — the focused task (or None);
    - ``done`` / ``total`` / ``pct`` — progress metrics;
    - ``last``        — ISO-8601 last-modified time of the space file,
                        or None when it does not exist yet.
    """
    if not is_valid_space(space):
        raise ValueError(f"Unknown space: {space!r}")
    tl = load_space(space)
    focused = tl.focused[0] if tl.focused else None
    last_iso: str | None = None
    try:
        mtime = _read_path(space).stat().st_mtime
        last_iso = datetime.fromtimestamp(
            mtime, tz=timezone.utc).isoformat()
    except OSError:
        pass  # space file not created yet -> no last-active stamp
    return {
        "name": space.upper(),
        "path": str(space_path(space)),
        "focus_id": focused.id if focused is not None else None,
        "focus_title": focused.title if focused is not None else None,
        "done": len(tl.done),
        "total": tl.total,
        "pct": tl.completion_pct,
        "last": last_iso,
    }


__all__ = [
    "spaces_dir",
    "space_path",
    "is_valid_space",
    "load_space",
    "add_space_task",
    "list_space",
    "space_snapshot",
]
