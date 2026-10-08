"""Out-of-project task spaces: ``ck local`` / ``ck remote`` and
any custom space file.

Unlike a project plan, a *space* is a global, non-project task
list for activity that belongs to no single Git repository:

- ``local``  — the workstation itself (OS config, tooling, sandbox
  reorganisation, …);
- ``remote`` — virtual / remote infrastructure (hosting, VPS,
  Proxmox/KVM, Docker hosts, …);
- custom   — ANY Markdown file placed in ``~/.config/ck/spaces/``
  is automatically a routable space (dynamic discovery — no
  registration step, no config change).

Storage is deliberately boring: one plain Markdown plan per space
at ``~/.config/ck/spaces/<name>.md``, using the exact same
``- [ ]`` / ``- [>]`` / ``- [x]`` task syntax as ``PLAN.md`` so
the same parser and renderer apply. Directories are created lazily
— no ``~/.config/ck/spaces`` tree appears until the first write.

:class:`SpaceManager` is the CENTRALIZED space engine: it treats
a global space file identically to a project ``PLAN.md`` by
re-using the core ``Task`` / ``Plan`` parsing and editing engine
(parse → mutate the :class:`TaskList` AST → render → atomic write
under a fail-closed lock). Process notes live in a sibling JSON
sidecar (``<name>.json``) — the exact analogue of a project's
``.ck/state.json`` — so notes survive across commands.

The module is import-light and project-independent: the CLI
dispatches ``ck <space> <command>`` straight here, and the global
dashboard calls :func:`space_snapshot` for its unified ``SPACES
(GLOBAL CONTEXTS)`` table (rendered by ``cklib.core`` through the
same grid pipeline as the Git projects table).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config
from .config import file_lock
from .models import TaskList, TaskStatus
from .parser import (
    _atomic_write_text,
    normalize_plan,
    parse_plan,
    render_plan,
    sanitize_task_text,
)

# Default scaffold for a space file that does not exist yet. Kept in
# sync with DEFAULT_PLAN's section naming so the renderer's
# ``## Completed`` insertion anchor behaves identically.
_EMPTY_SPACE = "# {space}\n\n## Current Sprint\n\n## Completed\n"

# A safe space name doubles as a file name in the spaces
# directory: ASCII alphanumerics plus ``.``/``_``/``-``, must
# start with an alphanumeric, and never ``.``/``..`` — this
# structurally excludes path separators and traversal attempts.
_SAFE_SPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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


def state_path(space: str) -> Path:
    """Path of the space's note-state sidecar (``<space>.json``).

    The analogue of a project's ``.ck/state.json``: process notes
    attached to space tasks live here, keyed by task ID.
    """
    return spaces_dir() / f"{space}.json"


def is_builtin_space(space: str) -> bool:
    """True for the built-in default spaces (``local``/``remote``)."""
    return space in config.SPACE_NAMES


def _is_safe_space_name(name: str) -> bool:
    """True when ``name`` is safe to use as a space-file name."""
    if not name or name in (".", ".."):
        return False
    return bool(_SAFE_SPACE_NAME_RE.match(name))


def existing_space_names() -> list[str]:
    """Discovered space names — every ``*.md`` file in the spaces dir.

    DYNAMIC DISCOVERY: any space file placed in
    ``~/.config/ck/spaces/`` is automatically a routable space with
    the full CLI interface (``add`` / ``list`` / ``done`` /
    ``focus`` / ``note``) — no registration, no allow-list update.
    Only structurally safe names are reported.
    """
    try:
        if not spaces_dir().is_dir():
            return []
        return sorted(
            p.stem for p in spaces_dir().glob("*.md")
            if _is_safe_space_name(p.stem)
        )
    except OSError:
        return []


def is_valid_space(space: str) -> bool:
    """True for the built-in defaults and every discovered space file."""
    return is_builtin_space(space) or space_path(space).is_file()


def routable_space_name(name: str) -> Optional[str]:
    """Space name when ``name`` routes to a space command.

    DYNAMIC ROUTING: every structurally safe name routes —

    1. the built-in defaults (``local`` / ``remote``) always;
    2. any discovered space file in the spaces directory;
    3. any other safe name — the ``add`` write action
       materializes a brand-new dynamic space lazily on its
       first write (mirroring the built-ins' lazy directory
       creation), while read/mutate actions on a space file
       that does not exist fail with a clean
       ``Space '<name>' not found`` error.

    Returns None for unsafe names (path separators, traversal
    attempts, empty) so they can never become filenames.
    """
    if not _is_safe_space_name(name):
        return None
    return name


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


def _state_read_path(space: str) -> Path:
    """Read-side path of the note-state sidecar (dev-mode aware)."""
    path = state_path(space)
    try:
        from .sandbox import dev_context_active, resolve_write_path
        if dev_context_active():
            sandboxed = resolve_write_path(path, create=False, force=True)
            if sandboxed.exists():
                return sandboxed
    except Exception:
        pass
    return path


def _state_write_path(space: str) -> Path:
    """Write-side path of the note-state sidecar (dev-mode aware)."""
    path = state_path(space)
    try:
        from .sandbox import dev_context_active, resolve_write_path
        if dev_context_active():
            path = resolve_write_path(path, force=True)
    except Exception:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------- #
# SpaceManager — the centralized space engine
# ---------------------------------------------------------------------- #

class SpaceManager:
    """Unified read/modify engine for one global space.

    A :class:`SpaceManager` treats ``~/.config/ck/spaces/<name>.md``
    EXACTLY like a project ``PLAN.md``: every command parses the
    Markdown into the core :class:`TaskList` AST, mutates the AST,
    then renders and atomically writes the file under a fail-closed
    lock — the same engine :class:`~cklib.core.ContextKeeper` uses,
    so no space-specific parsing or editing logic exists.

    ``create=True`` relaxes the existence check for the ``add``
    action, which materializes a brand-new dynamic space lazily on
    its first write (the built-ins behave the same way).
    """

    def __init__(self, space: str, *, create: bool = False) -> None:
        if not _is_safe_space_name(space):
            raise ValueError(f"Invalid space name: {space!r}")
        if not create and not is_valid_space(space):
            raise ValueError(
                f"Space {space!r} not found: {space_path(space)} does "
                f"not exist. Create it with `ck {space} add <text>`."
            )
        self.space = space

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def load(self) -> TaskList:
        """Parse the space into a :class:`TaskList` (empty when absent).

        A missing or blank file parses to the canonical empty scaffold
        so IDs and section context are stable across the first ``add``.
        """
        path = _read_path(self.space)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if not text.strip():
            text = _EMPTY_SPACE.format(space=self.space)
        return parse_plan(text)

    def _commit(self, task_list: TaskList) -> None:
        """Normalize (opt-in), then atomically write the space file.

        The write is serialised on a sibling ``.lock`` file
        (fail-closed), mirroring the project-plan write path.
        """
        normalize_plan(task_list)
        text = render_plan(task_list)
        path = _write_path(self.space)
        with file_lock(path):
            _atomic_write_text(path, text)

    def _section_for_new_task(self, tl: TaskList) -> str:
        """Section a new space task belongs to (mirrors core's helper)."""
        for sec in reversed(tl.sections):
            if sec.title.lower() == "completed":
                continue
            return sec.title
        return "Current Sprint"

    # ------------------------------------------------------------------ #
    # State sidecar (process notes)
    # ------------------------------------------------------------------ #

    def _read_state(self) -> dict:
        path = _state_read_path(self.space)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _write_state(self, state: dict) -> None:
        path = _state_write_path(self.space)
        text = json.dumps(state, indent=2, ensure_ascii=False)
        with file_lock(path):
            _atomic_write_text(path, text)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    def add_task(self, text: str) -> int:
        """Append a new open task to the space; return its numeric ID.

        The title is sanitized the same way project tasks are (ANSI
        escapes / control characters / whitespace runs stripped), the
        file is created lazily, and the write is atomic under a
        fail-closed lock.
        """
        title = sanitize_task_text(text)
        if not title:
            raise ValueError("Task text is empty")

        tl = self.load()
        section = self._section_for_new_task(tl)
        new_task = tl.add(title, status=TaskStatus.OPEN, section=section)
        self._commit(tl)
        return new_task.id

    def _all_notes(self) -> dict[int, str]:
        """All task notes in the space's sidecar (valid entries).

        Keys are task IDs (ints); only non-empty string values
        count. Orphaned notes (task since removed) are harmless:
        the list renderer only looks up IDs the plan still has.
        """
        raw = self._read_state().get("notes")
        if not isinstance(raw, dict):
            return {}
        out: dict[int, str] = {}
        for key, value in raw.items():
            if not isinstance(value, str) or not value:
                continue
            try:
                tid = int(key)
            except (TypeError, ValueError):
                continue
            out[tid] = value
        return out

    def list_tasks(self) -> str:
        """Render the space task list in the PROJECT ``ck list``
        layout.

        Routes through the SAME rendering engine as the
        project list (``cklib.core._render_tasks_listing``):
        tasks grouped under their ``##`` section headers with
        PLAN.md status markers, and every attached process
        note rendered as an indented ``* Note: <text>`` line
        directly beneath its task — matching how ``ck st``
        surfaces notes. A leading badge line summarizes the
        space itself.

        Format::

            [local] 1/3 done
            ## Current Sprint
            [ ] 1. first open task
            [>] 2. focused task
                * Note: process note text
            ## Completed
            [x] 3. done task
        """
        tl = self.load()
        header = f"[{self.space}] {len(tl.done)}/{tl.total} done"
        if not tl.tasks:
            return (
                f"{header}\n"
                f"No tasks. Add one with "
                f"`ck {self.space} add <text>`."
            )
        # Imported lazily: core imports this module at load time.
        from .core import _render_tasks_listing
        return (
            f"{header}\n"
            f"{_render_tasks_listing(tl, self._all_notes())}"
        )

    def done(self, spec: str) -> list[int]:
        """Mark task(s) as DONE — IDs, ranges and lists (``3``, ``2-4``).

        Returns the IDs actually transitioned (already-done tasks are
        skipped, mirroring the project ``ck done``). Raises
        ``ValueError`` for an unparseable spec and ``KeyError`` when
        the spec references a task the space does not have.
        """
        # Imported lazily: core imports this module at load time.
        from .core import _parse_id_spec

        ids = _parse_id_spec(spec)
        if not ids:
            raise ValueError(f"No valid task IDs in spec: {spec!r}")

        tl = self.load()
        unknown = [i for i in ids if tl.by_id(i) is None]
        if unknown:
            raise KeyError(
                f"Unknown task ID(s) in '{self.space}' space: {unknown}"
            )
        transitioned = tl.toggle_done(ids)
        if transitioned:
            self._commit(tl)
        return transitioned

    def focus(self, task_id: int) -> dict:
        """Focus a task (demoting any other focused task to open).

        ``task_id <= 0`` RESETS focus: the currently focused task (if
        any) is demoted to open and no new focus is set — mirroring
        the project ``ck start 0`` semantics. Raises ``KeyError``
        when the target task does not exist.
        """
        tl = self.load()
        focused_before = tl.focused[0] if tl.focused else None
        if task_id <= 0:
            if focused_before is not None:
                focused_before.status = TaskStatus.OPEN
            self._commit(tl)
            return {
                "task_id": 0,
                "title": "",
                "demoted_id": focused_before.id if focused_before else None,
                "demoted_title": (
                    focused_before.title if focused_before else ""
                ),
            }
        target = tl.by_id(task_id)
        if target is None:
            raise KeyError(f"No task with id {task_id}")
        demoted = None
        if focused_before is not None and focused_before.id != task_id:
            demoted = focused_before
        # TaskList.focus() demotes every other focused task to OPEN.
        tl.focus(task_id)
        self._commit(tl)
        return {
            "task_id": task_id,
            "title": target.title,
            "demoted_id": demoted.id if demoted else None,
            "demoted_title": demoted.title if demoted else "",
        }

    def already_focused(self, task_id: int) -> bool:
        """True when ``task_id`` is the currently focused task."""
        tl = self.load()
        focused = tl.focused[0] if tl.focused else None
        return focused is not None and focused.id == task_id

    def current_focus_id(self) -> Optional[int]:
        """The explicitly focused task's ID, or None (no focus set)."""
        tl = self.load()
        focused = tl.focused[0] if tl.focused else None
        return focused.id if focused is not None else None

    def set_note(self, task_id: int, text: str) -> dict:
        """Attach/update a process note on space task ``task_id``.

        The note lives in the space's JSON sidecar (``<space>.json``)
        keyed by task ID — the analogue of a project's
        ``.ck/state.json`` ``active_task`` note. Raises ``KeyError``
        when the task does not exist and ``ValueError`` for empty
        text. Returns the stored note metadata dict.
        """
        note = sanitize_task_text(text)
        if not note:
            raise ValueError("Note text is empty")
        tl = self.load()
        target = tl.by_id(task_id)
        if target is None:
            raise KeyError(f"No task with id {task_id}")
        state = self._read_state()
        notes = state.get("notes")
        if not isinstance(notes, dict):
            notes = {}
        notes[str(task_id)] = note
        state["notes"] = notes
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._write_state(state)
        return {"id": task_id, "title": target.title, "note": note}

    def get_note(self, task_id: int) -> str:
        """The stored process-note text for ``task_id`` ("" when none)."""
        notes = self._read_state().get("notes")
        if not isinstance(notes, dict):
            return ""
        note = notes.get(str(task_id))
        return note if isinstance(note, str) else ""

    def snapshot(self) -> dict:
        """Aggregate the state of the space for the dashboard table.

        Data only — no rendering. ``cklib.core`` feeds these snapshots
        through the SAME ``_render_grid`` pipeline the Git projects
        table uses, so every space renders as a uniform table row
        (``Space | Focus Task | Progress | Last Active``).

        Keys:

        - ``name``        — space name (upper-cased);
        - ``path``        — the space's Markdown file path (shown on the
                            row's second line, home-contracted by the
                            renderer);
        - ``focus_id`` / ``focus_title`` — the focused task (or None);
        - ``done`` / ``total`` / ``pct`` — progress metrics;
        - ``last``        — ISO-8601 last-modified time of the space
                            file, or None when it does not exist yet.
        """
        tl = self.load()
        focused = tl.focused[0] if tl.focused else None
        last_iso: str | None = None
        try:
            mtime = _read_path(self.space).stat().st_mtime
            last_iso = datetime.fromtimestamp(
                mtime, tz=timezone.utc).isoformat()
        except OSError:
            pass  # space file not created yet -> no last-active stamp
        return {
            "name": self.space.upper(),
            "path": str(space_path(self.space)),
            "focus_id": focused.id if focused is not None else None,
            "focus_title": focused.title if focused is not None else "",
            "done": len(tl.done),
            "total": tl.total,
            "pct": tl.completion_pct,
            "last": last_iso,
        }


# ---------------------------------------------------------------------- #
# Module-level API (backwards-compatible thin wrappers)
# ---------------------------------------------------------------------- #

def load_space(space: str) -> TaskList:
    """Parse a space into a :class:`TaskList` (empty space when absent)."""
    return SpaceManager(space).load()


def add_space_task(space: str, text: str) -> int:
    """Append a new open task to ``space``; return its numeric ID."""
    return SpaceManager(space).add_task(text)


def list_space(space: str) -> str:
    """Render ``space`` as a pipe-friendly task listing."""
    return SpaceManager(space).list_tasks()


def space_snapshot(space: str) -> dict:
    """Aggregate the state of ``space`` for the dashboard table."""
    return SpaceManager(space).snapshot()


__all__ = [
    "spaces_dir",
    "space_path",
    "state_path",
    "is_builtin_space",
    "existing_space_names",
    "is_valid_space",
    "routable_space_name",
    "SpaceManager",
    "load_space",
    "add_space_task",
    "list_space",
    "space_snapshot",
]
