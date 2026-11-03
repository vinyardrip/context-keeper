"""Out-of-project task spaces: ``ck local`` / ``ck remote`` and
any custom space file.

Unlike a project plan, a *space* is a global, non-project task
list for activity that belongs to no single Git repository:

- ``local``  — the workstation itself (OS config, tooling, sandbox
  reorganisation, …);
- ``remote`` — virtual / remote infrastructure (hosting, VPS,
  Proxmox/KVM, Docker hosts, …);
- ``today``  — the current day's scratch list (no reset logic yet);
- custom   — ANY Markdown file placed in ``~/.config/ck/spaces/``
  is automatically a routable space (dynamic discovery — no
  registration step, no config change).

Storage is deliberately boring: one plain Markdown plan per space
at ``~/.config/ck/spaces/<name>.md``, using the exact same
``- [ ]`` / ``- [>]`` / ``- [x]`` task syntax as ``PLAN.md`` so
the same parser and renderer apply. The BUILT-IN defaults
(``local`` / ``remote`` / ``today``) are materialized from the
canonical scaffold at CLI startup (:func:`ensure_default_spaces`) —
an existing file is strictly preserved, never rewritten. Custom
spaces and their parent directories still appear lazily — no
``~/.config/ck/spaces`` tree for them until the first write.

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
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from . import config, sequence
from . import notes as note_store
from .config import file_lock
from .models import Notes, TaskList, TaskStatus
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

# Process start time (monotonic wall clock). A space ``*.lock`` file
# whose mtime is OLDER than this process began belongs to a previous
# invocation and is stale — startup cleanup may remove it.
_PROCESS_START_TIME = time.time()


# ---------------------------------------------------------------------- #
# Paths
# ---------------------------------------------------------------------- #

def spaces_dir() -> Path:
    """Directory holding the space files (``~/.config/ck/spaces``).

    Computed on every call from the *current* ``GLOBAL_CONFIG_DIR`` so
    tests that pin a temporary config root (and any future runtime
    relocation) observe the change.

    STRICT DEV-MODE ISOLATION: while a sandbox context is active
    (``CK_SANDBOX`` / ``CK_DEV`` / ``ck-dev`` / cwd inside
    ``.sandbox/``), the whole spaces tree is redirected to
    ``.sandbox/config/spaces/``. Production spaces are neither read
    nor written in that mode — the sandbox owns its own space files,
    note sidecars, histories and ordering state. The mapping is pure
    (``create=False``), so merely asking for the directory never
    materializes ``.sandbox/``.
    """
    production = config.GLOBAL_CONFIG_DIR / config.SPACES_DIR_NAME
    try:
        from .sandbox import dev_context_active, resolve_write_path
        if dev_context_active():
            return resolve_write_path(
                production, create=False, force=True)
    except Exception:
        pass
    return production


def space_path(space: str) -> Path:
    """Path of ``<space>.md`` (``local.md`` / ``remote.md``)."""
    return spaces_dir() / f"{space}.md"


def state_path(space: str) -> Path:
    """Path of the space's note-state sidecar (``<space>.json``).

    The analogue of a project's ``.ck/state.json``: process notes
    attached to space tasks live here, keyed by task ID.
    """
    return spaces_dir() / f"{space}.json"


# Space history lives NEXT TO the space file, not inside the project's
# ``.ck/`` tree: a space is global and must keep its audit trail even
# when no project is bound. The ``.HISTORY.md`` suffix is deliberately
# distinct from a discoverable space file (``work.md`` -> ``work``);
# every discovery/validation probe excludes this suffix so a history
# file can never masquerade as a routable space.
SPACE_HISTORY_SUFFIX = ".HISTORY.md"


def is_space_history_name(name: str) -> bool:
    """True for names reserved by the ``<space>.HISTORY.md`` convention.

    Accepts either a bare stem (``work.HISTORY``) or a file name
    (``work.HISTORY.md``), case-insensitively. The check is structural:
    it does not touch the filesystem.
    """
    key = (name or "").strip().lower()
    return (key.endswith(SPACE_HISTORY_SUFFIX.lower())
            or key.endswith(".history"))


def history_path(space: str) -> Path:
    """Path of a space's durable note history (``<space>.HISTORY.md``).

    Notes archived by ``ck delete <ID> -s <space>`` land here, in the
    same flat directory as ``<space>.md`` and ``<space>.json``. The
    file is created lazily on the first archive; a space without
    archived notes has no history file.
    """
    return spaces_dir() / f"{space}{SPACE_HISTORY_SUFFIX}"


def is_builtin_space(space: str) -> bool:
    """True for the built-in default spaces (``local``/``remote``/``today``)."""
    return space in config.SPACE_NAMES


def _is_safe_space_name(name: str) -> bool:
    """True when ``name`` is safe to use as a space-file name.

    The ``.HISTORY`` suffix is structurally reserved for a space's
    archived note history (``<space>.HISTORY.md``), so it can never
    become a routable space name.
    """
    if not name or name in (".", "..") or is_space_history_name(name):
        return False
    return bool(_SAFE_SPACE_NAME_RE.match(name))


def spaces_dirs() -> list[Path]:
    """Every directory that can hold space files RIGHT NOW.

    In production this is exactly ``~/.config/ck/spaces/``. In a
    dev-mode session :func:`spaces_dir` already resolves to
    ``.sandbox/config/spaces/``, so the list contains ONLY that
    sandbox directory: production space files are deliberately
    invisible to the session and can never leak into discovery,
    validation, listings or the dashboard.

    Kept as a list-returning API because every probe historically
    iterated over it; there is exactly one authoritative directory
    per context.
    """
    return [spaces_dir()]


def existing_space_names() -> list[str]:
    """Discovered space names — every ``*.md`` file in the spaces dir.

    DYNAMIC DISCOVERY: any space file placed in
    ``~/.config/ck/spaces/`` is automatically a routable space with
    the full CLI interface (``add`` / ``list`` / ``st`` / ``done`` /
    ``focus`` / ``start`` / ``note`` / ``edit``) — no registration,
    no allow-list update. A brand-new, still-empty space file counts:
    discovery keys off the FILE, never off its task count.

    In dev mode the scan is strictly limited to the sandbox copy
    (``.sandbox/config/spaces/``); production spaces are never
    probed. Only structurally safe names are reported.
    """
    names: set = set()
    for directory in spaces_dirs():
        try:
            if not directory.is_dir():
                continue
            names.update(
                p.stem for p in directory.glob("*.md")
                if _is_safe_space_name(p.stem)
                and not is_space_history_name(p.stem)
            )
        except OSError:
            continue
    return sorted(names)


def is_valid_space(space: str) -> bool:
    """True for the built-in defaults and every discovered space file.

    Probes the same directory as :func:`existing_space_names`, so in
    dev mode only sandbox spaces are valid — a production-only space
    is intentionally unknown to the sandbox session.
    """
    if is_builtin_space(space):
        return True
    if is_space_history_name(space):
        return False
    return any(
        (directory / f"{space}.md").is_file()
        for directory in spaces_dirs()
    )


def routable_space_name(name: str) -> Optional[str]:
    """Structurally safe space name for ``ck <name> <command>``.

    DYNAMIC ROUTING: this helper only rejects unsafe tokens — path
    separators, traversal attempts, empty names, history archives.
    Whether the name actually resolves to a space is decided by the
    dispatcher (v0.9.3 strict routing): an existing space file or a
    built-in default proceeds, while a registered project or an
    unknown name is refused before any lock or write.

    Returns None for unsafe names so they can never become filenames.
    """
    if not _is_safe_space_name(name) or is_space_history_name(name):
        return None
    return name


def registered_project_for(name: str):
    """Registered project matching ``name``, or None.

    STRICT SPACE ROUTING: ``ck <name> <command>`` never resolves to a
    project. This helper lets the dispatcher detect the collision and
    refuse with an actionable message instead of lazily creating a
    same-named space file. Matching is by registry ``name`` or the
    project path's basename (so both spellings of the folder work).
    """
    if not name:
        return None
    try:
        from . import registry
        for entry in registry.list_projects():
            if entry.name == name:
                return entry
            try:
                if Path(entry.path).name == name:
                    return entry
            except (TypeError, ValueError):
                continue
    except Exception:
        # A broken registry must never make routing fail with a
        # traceback; fall through to the unknown-space branch.
        return None
    return None


def cleanup_stale_space_locks() -> list[Path]:
    """Best-effort removal of stale ``*.lock`` files in the spaces dir.

    A lock file older than the CURRENT process is a leftover from an
    interrupted invocation. Cleanup never creates the spaces directory
    or any file, and it skips a lock still held by a live process: the
    unlink happens only after a non-blocking ``flock`` succeeds (POSIX;
    on platforms without ``fcntl`` the age check is the only guard).
    Returns the paths actually removed.
    """
    try:
        directory = spaces_dir()
        if not directory.is_dir():
            return []
        locks = list(directory.glob("*.lock"))
    except OSError:
        return []

    removed: list[Path] = []
    for lock in locks:
        try:
            if lock.stat().st_mtime >= _PROCESS_START_TIME:
                continue
            fd = os.open(str(lock), os.O_RDWR)
        except OSError:
            continue
        try:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:
                pass
            except OSError:
                # Held by a live writer: leave it alone.
                continue
            try:
                lock.unlink()
                removed.append(lock)
            except OSError:
                pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    return removed


# The DEFAULT, directory-based project space. It is not a file: it
# stands for the active project row that ``ck space list`` renders, so
# the ordering sequence can position it like any other space. It is
# never creatable, renamable or deletable — the ``project`` sentinel
# always resolves to the current project.
PROJECT_SPACE = "project"

# Names that must NEVER become a space, even though they are
# structurally safe filenames:
#
# - ``local`` / ``remote`` / ``today`` — the built-in defaults are
#   permanent contexts (always listed by the dashboard); a same-named
#   file would shadow the default;
# - ``project`` — the sentinel the management list uses for the
#   active project row;
# - ``list`` / ``create`` / ``rename`` / ``delete`` — the
#   ``ck space …`` management verbs, and ``list`` additionally a
#   top-level command. A space named after one of them makes
#   ``ck <name> <verb>`` ambiguous for the reader.
RESERVED_SPACE_NAMES = frozenset({
    "local", "remote", "today", PROJECT_SPACE, "list", "create", "rename",
    "delete", "move", "swap", "reorder",
})


def validate_new_space_name(name: str) -> str:
    """Validate ``name`` as a space that does not exist yet.

    Returns the validated name; raises :class:`ValueError` with a
    user-facing message otherwise. Guards, in order:

    1. non-empty and structurally safe (see :func:`_is_safe_space_name`:
       ASCII letters/digits/``.``/``_``/``-`` only, so spaces, path
       separators and every other special character are rejected);
    2. not a reserved word (:data:`RESERVED_SPACE_NAMES`);
    3. not spelled with a redundant ``.md`` suffix (the extension is
       added automatically);
    4. not already taken by an existing space file.
    """
    if not name:
        raise ValueError("Space name is empty.")
    if is_space_history_name(name):
        raise ValueError(
            f"Invalid space name: {name!r}. The '.HISTORY' suffix is "
            "reserved for a space's archived note history."
        )
    if not _is_safe_space_name(name):
        raise ValueError(
            f"Invalid space name: {name!r}. Use letters, digits, "
            "'.', '_' and '-' only (no spaces, no path separators)."
        )
    if name in RESERVED_SPACE_NAMES:
        raise ValueError(
            f"Invalid space name: {name!r}. Reserved names: "
            f"{', '.join(sorted(RESERVED_SPACE_NAMES))}."
        )
    if name.lower().endswith(".md"):
        raise ValueError(
            f"Invalid space name: {name!r}. The '.md' extension is "
            f"added automatically — use {name[:-3]!r} instead."
        )
    if _space_exists_anywhere(name):
        raise ValueError(
            f"Space '{name}' already exists: {space_path(name)}")
    return name


def _read_path(space: str) -> Path:
    """Read-side path for a space (already dev-mode aware).

    :func:`space_path` resolves through the strict :func:`spaces_dir`
    mapping, so a dev session reads ONLY ``.sandbox/config/spaces/``.
    There is deliberately no production fallback: exposing host space
    content inside the sandbox would defeat isolation.
    """
    return space_path(space)


def _write_path(space: str) -> Path:
    """Write-side path for a space (already dev-mode aware).

    Parent directories are created lazily here — this is the only
    place that ever creates the active spaces directory, and dev mode
    redirects it into ``.sandbox/config/spaces/`` so the real config
    tree stays untouched.
    """
    path = space_path(space)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _state_read_path(space: str) -> Path:
    """Read-side path of the note-state sidecar (dev-mode aware)."""
    return state_path(space)


def _state_write_path(space: str) -> Path:
    """Write-side path of the note-state sidecar (dev-mode aware)."""
    path = state_path(space)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _history_write_path(space: str) -> Path:
    """Write-side path of the space's history file (dev-mode aware)."""
    path = history_path(space)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_space_history(space: str, entry: Notes) -> None:
    """Append a rendered ``Notes`` entry to the space's HISTORY.md.

    The write is serialised on the history file's ``*.lock``. The
    path follows the standard dev-mode redirect, so a sandbox append
    lands in ``.sandbox/config/spaces/`` and never seeds itself from
    (or touches) the production history file.
    """
    path = _history_write_path(space)
    with file_lock(path):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry.render())


# ---------------------------------------------------------------------- #
# Space management: create / rename / delete
# ---------------------------------------------------------------------- #

def _space_exists_anywhere(name: str) -> bool:
    """True when ``<name>.md`` exists in the active spaces directory.

    The active directory is resolved by :func:`spaces_dir` and is
    already dev-mode aware: in a sandbox session only
    ``.sandbox/config/spaces/`` is probed, so a production-only space
    can never block or shadow a sandbox create.
    """
    return any(
        (directory / f"{name}.md").exists()
        for directory in spaces_dirs()
    )


# ---------------------------------------------------------------------- #
# Space ordering (persisted in the global state.json)
# ---------------------------------------------------------------------- #

SPACE_ORDER_KEY = "spaces_order"


def global_state_path() -> Path:
    """``<global config>/state.json`` — the space-order store.

    Computed from the CURRENT ``GLOBAL_CONFIG_DIR`` on every call
    (like :func:`spaces_dir`), so a pinned test config root or a
    runtime relocation observes it. In production this is exactly
    ``cklib.config.GLOBAL_STATE_FILE``; space order is GLOBAL state
    (spaces outlive any one project), never a project's
    ``.ck/state.json``.
    """
    return config.GLOBAL_CONFIG_DIR / "state.json"


def _global_state_read_path() -> Path:
    """Read-side state path, strictly sandboxed in dev mode.

    The path follows the same redirect as :func:`_global_state_write_path`.
    There is no production fallback: host space ordering (including
    custom space names) must not leak into a sandbox session. The
    probe never materialises ``.sandbox/``.
    """
    path = global_state_path()
    try:
        from .sandbox import dev_context_active, resolve_write_path
        if dev_context_active():
            return resolve_write_path(path, create=False, force=True)
    except Exception:
        pass
    return path


def _global_state_write_path() -> Path:
    """Write-side state path (dev mode redirects into the sandbox)."""
    path = global_state_path()
    try:
        from .sandbox import dev_context_active, resolve_write_path
        if dev_context_active():
            path = resolve_write_path(path, force=True)
    except Exception:
        pass
    return path


def _read_global_state_dict() -> dict:
    """Best-effort read of the global state mapping (never raises)."""
    try:
        data = json.loads(
            _global_state_read_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_space_order() -> list[str]:
    """Persisted space order (valid, unique names only).

    Purely a read: a missing, unreadable or corrupt state file yields
    an empty list (callers fall back to the canonical default order).
    The ``project`` sentinel is a valid entry.
    """
    raw = _read_global_state_dict().get(SPACE_ORDER_KEY)
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, str) or item in seen:
            continue
        if item != PROJECT_SPACE and not _is_safe_space_name(item):
            continue
        seen.add(item)
        out.append(item)
    return out


def write_space_order(order: list[str]) -> None:
    """Persist ``order`` under ``spaces_order``, preserving other keys.

    Read-modify-write under the state file's fail-closed ``*.lock`` +
    atomic replace, so ordering can never tear the update-notifier
    timestamps living in the same file. DEV MODE redirects the write
    into ``.sandbox/config/state.json``.
    """
    path = _global_state_write_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        state = _read_global_state_dict()
        state[SPACE_ORDER_KEY] = list(order)
        text = json.dumps(state, indent=2, ensure_ascii=False)
        _atomic_write_text(path, text)


def available_space_names() -> list[str]:
    """Every real space right now: built-ins first, then discoveries."""
    return list(config.SPACE_NAMES) + [
        name for name in existing_space_names()
        if name not in config.SPACE_NAMES
    ]


def space_order(*, include_project: bool = False) -> list[str]:
    """The current display/mutation sequence for spaces.

    ``include_project=True`` prepends the default, directory-based
    ``project`` space: the sentinel for the active project row in
    ``ck space list`` (which therefore participates in move/swap/
    reorder like any other space). The dashboard omits it.

    The result reconciles the PERSISTED ``spaces_order`` with what
    actually exists: stale entries are dropped, safe unknown names
    are ignored, new spaces are appended in canonical order and
    duplicates collapse. With no persisted order the canonical
    default applies (``project``, then ``local``/``remote``/``today``,
    then custom spaces sorted). Existing persisted orders are never
    rewritten by this read-only reconciliation.
    """
    available = available_space_names()
    if include_project:
        available = [PROJECT_SPACE, *available]
    stored = read_space_order()
    if not stored:
        return available
    order = sequence.normalize_order(stored, available)
    if include_project and PROJECT_SPACE not in stored:
        # Hand-edited/pre-v0.8.15 state: the default project space
        # belongs FIRST, not appended at the end.
        order = sequence.without(order, PROJECT_SPACE)
        order.insert(0, PROJECT_SPACE)
    return order


def _require_ordered_space(name: str) -> None:
    """Validate a move/swap target (``project`` or an existing space)."""
    if name == PROJECT_SPACE:
        return
    if not _is_safe_space_name(name) or not is_valid_space(name):
        raise SpaceNotFoundError(
            f"Space {name!r} not found: {space_path(name)} "
            f"does not exist."
        )


def move_space(name: str, position: int) -> list[str]:
    """Move ``name`` to 1-based ``position`` in the space sequence.

    Persists the new order (``spaces_order``) and returns it. Raises
    :class:`SpaceNotFoundError` for an unknown space and
    :class:`ValueError` for an out-of-range position — validated
    BEFORE anything is written, so a bad request never persists a
    partial order.
    """
    _require_ordered_space(name)
    order = space_order(include_project=True)
    try:
        new_order = sequence.move_item(order, name, position)
    except KeyError:
        raise SpaceNotFoundError(
            f"Space {name!r} not found in the space sequence.") from None
    write_space_order(new_order)
    return new_order


def swap_spaces(name_a: str, name_b: str) -> list[str]:
    """Exchange the positions of two spaces (persists the order)."""
    _require_ordered_space(name_a)
    _require_ordered_space(name_b)
    order = space_order(include_project=True)
    try:
        new_order = sequence.swap_items(order, name_a, name_b)
    except KeyError:
        raise SpaceNotFoundError(
            "Space not found in the space sequence: "
            f"{name_a!r} / {name_b!r}.") from None
    if new_order != order:
        write_space_order(new_order)
    return new_order


def reorder_spaces() -> list[str]:
    """Normalize the persisted sequence and write it back.

    Re-indexes against the spaces that EXIST right now: stale names
    disappear, new spaces are appended, duplicates collapse and the
    ``project`` sentinel keeps its (possibly moved) slot. Returns the
    resulting sequence, 1-based positions implied by list order.
    """
    order = space_order(include_project=True)
    write_space_order(order)
    return order


def _rename_space_in_order(old_name: str, new_name: str) -> None:
    """Carry a renamed space's position in the persisted order."""
    stored = read_space_order()
    if old_name not in stored:
        return
    write_space_order([
        new_name if name == old_name else name for name in stored
    ])


def _remove_space_from_order(name: str) -> None:
    """Drop a deleted space from the persisted order (if present).

    Writes only when the name was actually stored, so deleting a
    space on a machine with a pristine state file never creates one.
    """
    stored = read_space_order()
    if name not in stored:
        return
    write_space_order(sequence.without(stored, name))


def _write_space_file(name: str) -> Path:
    """Write the default scaffold to ``<name>.md`` (dev-mode aware).

    Shared by :func:`create_space` (explicit user action, after
    full validation) and :func:`ensure_default_spaces` (startup
    auto-init, whose names come from the built-in list only — the
    built-ins are RESERVED and would fail validation). The write is
    atomic under a fail-closed lock, and parent directories are
    created lazily by :func:`_write_path`.
    """
    path = _write_path(name)
    text = _EMPTY_SPACE.format(space=name)
    with file_lock(path):
        _atomic_write_text(path, text)
    return path


def create_space(name: str) -> Path:
    """Create a new space file from the default template.

    Writes ``<name>.md`` with the standard space scaffold (``#
    <name>``, ``## Current Sprint``, ``## Completed``) — identical to
    what the lazy ``add`` path would produce, so a freshly created
    space and a space created by its first task are the same file.

    Raises :class:`ValueError` for an unsafe, reserved, suffixed or
    already-taken name (see :func:`validate_new_space_name`).
    Returns the created path.
    """
    validate_new_space_name(name)
    return _write_space_file(name)


def ensure_default_spaces() -> list[str]:
    """Create missing defaults (``local`` / ``remote`` / ``today``).

    Idempotent, strictly non-destructive initialization (v0.8.12):

    - probes the SAME dev-aware directory every space read uses
      (:func:`_space_exists_anywhere`); a space whose file exists in
      the active directory is PRESERVED byte-for-byte — never
      rewritten, reset or deleted, so user tasks and process notes
      survive;
    - a missing space is materialized from the canonical scaffold
      (byte-identical to the lazy first-write file), with the
      existence check REPEATED inside the fail-closed lock so a
      concurrent first ``ck <space> add`` can never be overwritten;
    - only the built-in names (:data:`cklib.config.SPACE_NAMES`)
      are ever touched — custom spaces are never created or modified;
    - follows the strict dev-mode redirect (:func:`_write_path`): a
      sandboxed session seeds ONLY ``.sandbox/config/spaces/`` with
      the built-in defaults and never mutates or reads the real
      config tree;
    - never raises: a read-only or broken config tree must not break
      the command being started.

    Returns the list of names actually created (empty when every
    default already existed).
    """
    # Best-effort housekeeping BEFORE seeding: locks left by an
    # interrupted earlier invocation are safe to drop, while locks
    # this process is about to create are newer than the cutoff.
    try:
        cleanup_stale_space_locks()
    except Exception:
        pass
    created: list[str] = []
    for name in config.SPACE_NAMES:
        try:
            if _space_exists_anywhere(name):
                continue
            path = _write_path(name)
            with file_lock(path):
                # Re-probe UNDER the lock: another process (or a
                # concurrent first ``ck <space> add``) may have
                # materialized the file in the meantime — the
                # loser must not overwrite the winner's file.
                if path.is_file() or _space_exists_anywhere(name):
                    continue
                _atomic_write_text(
                    path, _EMPTY_SPACE.format(space=name))
            created.append(name)
        except Exception:
            # One unwritable/broken space must never block the
            # others — and never break the starting command.
            continue
    return created


def rename_space(old_name: str, new_name: str) -> dict:
    """Rename a space, carrying its note sidecar along.

    Moves ``<old>.md`` -> ``<new>.md`` and, when it exists, the
    sibling ``<old>.json`` -> ``<new>.json`` so no process notes are
    lost. Both moves are dev-mode aware (they operate on the same
    sandboxed paths every other space write uses).

    Raises :class:`ValueError` when the source is not an existing
    space file, or the target name is unsafe, reserved or already
    taken. Returns ``{"from", "to", "moved", "notes_moved"}``.
    """
    if not _is_safe_space_name(old_name):
        raise ValueError(
            f"Invalid space name: {old_name!r}. Use letters, digits, "
            "'.', '_' and '-' only.")
    src = _write_path(old_name)
    if not src.is_file():
        raise ValueError(
            f"Space {old_name!r} not found: {space_path(old_name)} "
            f"does not exist. Create it with `ck space create "
            f"{old_name}`."
        )
    if old_name == new_name:
        raise ValueError(
            f"Space is already named {old_name!r}.")
    validate_new_space_name(new_name)

    dst = _write_path(new_name)
    # Both moves are serialised on the SOURCE lock (fail-closed), the
    # same contract every space write follows.
    with file_lock(src):
        src.replace(dst)
        # Carry the note sidecar along so no process notes are orphaned.
        notes_moved = False
        src_state = _state_write_path(old_name)
        if src_state.is_file():
            src_state.replace(_state_write_path(new_name))
            notes_moved = True
        # The archived note history belongs to the space, not to its
        # old name: carry it along so a rename never orphans it.
        history_moved = False
        src_history = _history_write_path(old_name)
        if src_history.is_file():
            src_history.replace(_history_write_path(new_name))
            history_moved = True
    # A renamed space keeps its position in the persisted sequence
    # instead of dropping to the end as a "new" space.
    _rename_space_in_order(old_name, new_name)
    return {
        "from": old_name,
        "to": new_name,
        "moved": str(dst),
        "notes_moved": notes_moved,
        "history_moved": history_moved,
    }


class SpaceNotFoundError(ValueError):
    """A space (or its file) does not exist.

    A DISTINCT domain error so callers can map "not found" to its own
    exit code instead of lumping it in with usage errors — ``ck space
    delete`` reports a missing target with exit 1, while malformed
    input keeps the generic exit 2. Subclasses :class:`ValueError`, so
    any existing ``except ValueError`` handler still catches it.
    """


def validate_deletable_space(name: str) -> Path:
    """Validate that ``name`` may be deleted; return its file path.

    The PRE-FLIGHT check every delete flow must run BEFORE asking the
    user to confirm anything:

    1. a built-in default (``local`` / ``remote``) is PROTECTED and can
       never be deleted;
    2. the name must be structurally safe;
    3. the space file must actually exist in the active spaces
       directory — in a dev session that is the sandbox copy
       (``.sandbox/config/spaces/``), never production.

    Raises :class:`ValueError` with a user-facing message on any
    failure; the CLI turns that into a clean ``ERROR:`` line WITHOUT
    ever showing the confirmation prompt for a space that cannot be
    deleted anyway.
    """
    if name == PROJECT_SPACE:
        raise ValueError(
            "Cannot delete the default project space 'project': it "
            "represents the active project directory, not a space "
            "file. Delete project tasks with `ck delete <ID>`.")
    if is_builtin_space(name):
        raise ValueError(
            f"Cannot delete the built-in space {name!r}: it is a "
            "default context. Create a custom space instead.")
    if is_space_history_name(name):
        raise ValueError(
            f"Invalid space name: {name!r}. The '.HISTORY' suffix is "
            "reserved for a space's archived note history.")
    if not _is_safe_space_name(name):
        raise ValueError(
            f"Invalid space name: {name!r}. Use letters, digits, "
            "'.', '_' and '-' only.")
    path = _write_path(name)
    if not path.is_file():
        raise SpaceNotFoundError(
            f"Space {name!r} not found: {space_path(name)} "
            f"does not exist."
        )
    return path


def delete_space(name: str, *, confirmed: bool = False) -> dict:
    """Permanently delete a space file and its note sidecar.

    The TARGET is validated first (:func:`validate_deletable_space`):
    asking to confirm the deletion of a space that does not exist — or
    of a protected built-in — is a pointless prompt, so the honest
    error wins.

    STRICTLY CONFIRMED: the caller MUST then pass ``confirmed=True``
    (the ``ck space delete`` prompt / ``-f`` / ``-y`` flag). Without
    it nothing is removed and the function refuses — an accidental
    call can never destroy a space. The name is also dropped from the
    persisted ``spaces_order`` so the sequence never keeps a hole.

    Returns ``{"name", "removed", "notes"}``.
    """
    path = validate_deletable_space(name)
    if not confirmed:
        raise ValueError(
            f"Refusing to delete space {name!r} without confirmation. "
            "Re-run with confirmation (the CLI prompts, or pass -y).")
    # Serialised on the space's own lock (fail-closed), so a delete can
    # never interleave with a concurrent write.
    with file_lock(path):
        removed = path.name
        path.unlink()
        notes_removed = False
        sidecar = _state_write_path(name)
        if sidecar.is_file():
            sidecar.unlink()
            notes_removed = True
        # The space's audit trail is part of the space, so deleting
        # the space removes it as well (the file is lazy: most spaces
        # have no history until a task is deleted).
        history_removed = False
        history = _history_write_path(name)
        if history.is_file():
            history.unlink()
            history_removed = True
    # Keep the persisted sequence in sync: a deleted space must not
    # leave a hole (normalization would drop it anyway, but the state
    # file should reflect reality immediately).
    _remove_space_from_order(name)
    return {
        "name": name,
        "removed": removed,
        "notes": notes_removed,
        "history": history_removed,
    }


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

    @property
    def history_file(self) -> Path:
        """This space's durable note history (``<space>.HISTORY.md``)."""
        return history_path(self.space)

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
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return note_store.migrate_notes(
            data, note_store.file_mtime_iso(path))

    def _write_state(self, state: dict) -> None:
        path = _state_write_path(self.space)
        state = note_store.migrate_notes(state, note_store.now_iso())
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

    def task_summary(self, task_id: int) -> dict:
        """Read-only snapshot of a task for a deletion confirmation.

        Mirrors :meth:`cklib.core.ContextKeeper.task_summary`, but reads
        THIS space's Markdown file instead of a project ``PLAN.md``.
        Returns ``{"id", "title", "status", "space"}`` and raises
        ``KeyError`` for an unknown ID. Nothing is written.
        """
        tl = self.load()
        target = tl.by_id(task_id)
        if target is None:
            raise KeyError(f"No task with id {task_id}")
        return {
            "id": target.id,
            "title": target.title,
            "status": target.status.canonical_marker,
            "space": self.space,
        }

    def delete_task(self, task_id: int) -> dict:
        """Permanently remove task ``task_id`` from this space.

        The space analogue of :meth:`ContextKeeper.delete_task`:

        - process notes bound to the task are archived into the
          space's sibling ``<space>.HISTORY.md`` BEFORE the task
          disappears;
        - the task line is removed and the remaining IDs are
          re-indexed 1..N;
        - the note sidecar's IDs are shifted to follow the same
          positional reindexing, and the deleted task's note entry is
          dropped after archiving;
        - the active project's ``.ck/state.json`` and ``PLAN.md`` are
          never touched — this method only reads/writes the target
          space file, its sidecar and its history.

        Returns ``{"id", "title", "space", "remaining",
        "notes_archived"}``.
        """
        tl = self.load()
        target = tl.by_id(task_id)
        if target is None:
            raise KeyError(f"No task with id {task_id}")

        notes = self._notes_bound_to_task(task_id)
        if notes:
            self._archive_note_to_history(target, notes)

        tl.delete(task_id)
        self._commit(tl)
        self._remap_notes_after_delete(task_id)
        return {
            "id": task_id,
            "title": target.title,
            "space": self.space,
            "remaining": tl.total,
            "notes_archived": len(notes),
        }

    def _notes_bound_to_task(self, task_id: int) -> list[dict]:
        """Every timestamped process-note entry attached to ``task_id``."""
        return self.get_notes(task_id)

    def _archive_note_to_history(self, task, notes) -> None:
        """Append the task's notes to this space's ``<space>.HISTORY.md``.

        Reuses the project history entry format (``Notes.render()``)
        so space and project audit trails look identical. A single
        note keeps the historical ``* Note: <text>`` line; multiple
        notes share one dated entry with one timestamped ``* Note``
        line per entry, in append order. The task ID is the ID the
        user asked to delete (before re-indexing).
        """
        if isinstance(notes, str):
            entries = [{"text": notes, "created": ""}]
        else:
            entries = list(notes)
        if not entries:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if len(entries) == 1:
            entry = Notes(
                task_id=task.id,
                task_title=task.title,
                timestamp=ts,
                comment="",
                note_lines=[f"  * Note: {entries[0]['text']}"],
            )
        else:
            from .core import format_note_time
            lines: list[str] = []
            for item in entries:
                stamp = format_note_time(item.get("created") or "")
                suffix = f" ({stamp})" if stamp else ""
                lines.append(
                    f"  * Note{suffix}: {item['text']}")
            entry = Notes(
                task_id=task.id,
                task_title=task.title,
                timestamp=ts,
                comment="",
                note_lines=lines,
            )
        _append_space_history(self.space, entry)

    def _remap_notes_after_delete(self, deleted_id: int) -> None:
        """Shift sidecar note IDs after a positional task deletion.

        Notes for tasks AFTER the deleted one now belong to the task
        one slot earlier. The deleted task's entry was archived by
        :meth:`delete_task`, so it is dropped. Writes only when
        something changed, preserving unrelated sidecar keys.
        """
        state = self._read_state()
        notes = state.get("notes")
        if not isinstance(notes, dict):
            return

        updated: dict = {}
        changed = False
        for key, value in notes.items():
            try:
                tid = int(key)
            except (TypeError, ValueError):
                updated[key] = value
                continue
            if tid == deleted_id:
                changed = True
                continue
            if tid > deleted_id:
                updated[str(tid - 1)] = value
                changed = True
            else:
                updated[key] = value

        if not changed:
            return
        if updated:
            state["notes"] = updated
        else:
            state.pop("notes", None)
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._write_state(state)

    def _all_note_entries(self) -> dict[int, list[dict]]:
        """All task note entries in the space's sidecar (valid entries).

        Keys are task IDs (ints); values are ordered ``{"text",
        "created"}`` arrays. Orphaned notes (task since removed) are
        harmless: the list renderer only looks up IDs the plan still
        has.
        """
        raw = self._read_state().get("notes")
        if not isinstance(raw, dict):
            return {}
        out: dict[int, list[dict]] = {}
        for key, value in raw.items():
            try:
                tid = int(key)
            except (TypeError, ValueError):
                continue
            entries = [
                {"text": item["text"], "created": item["created"]}
                for item in value
                if isinstance(item, dict)
                and isinstance(item.get("text"), str)
                and item.get("text")
                and isinstance(item.get("created"), str)
                and item.get("created")
            ] if isinstance(value, list) else []
            if entries:
                out[tid] = entries
        return out

    def _all_notes(self) -> dict[int, str]:
        """Newest note text per task (backward-compatible view)."""
        return {
            tid: entries[-1]["text"]
            for tid, entries in self._all_note_entries().items()
            if entries
        }

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
            f"{_render_tasks_listing(tl, self._all_note_entries())}"
        )

    def status(self) -> str:
        """Render the detailed status view for the space.

        Uses the SAME renderer as the project ``ck st``
        (``cklib.core._render_local_status``), so a space
        status is laid out exactly like a project status:
        the header bar, the `` > NAME [v<version>]`` line,
        the ``[%] Progress:`` line, the ``-> CURRENT FOCUS:``
        block with its process note, and the full
        ``WORK CONTEXT`` (``<< Done`` / ``[!] Skipped`` /
        ``[>] Focus`` / ``[>] Next`` / ``>> Upcoming`` /
        ``Unfocused / Paused Context`` / ``>> Backlog``).

        The only space-specific adaptation is the data
        source: :class:`_SpaceKeeperView` supplies the
        space's display name, the note bound to the focused
        task (from the JSON sidecar), and the space's
        paused ledger (open, noted, non-focused tasks —
        the analogue of the project's paused registry).
        """
        # Imported lazily: core imports this module at load time.
        from .core import _render_local_status
        return _render_local_status(
            _SpaceKeeperView(self), self.load())

    def notes(self) -> str:
        """Render all active process notes for the space.

        Uses the SAME renderer as the project ``ck notes``
        (``cklib.core._render_notes_listing``), so the listing is
        formatted identically: the ``[>] Active Focus:`` section
        for the focused task's bound note, the ``[!] Unfocused /
        Paused Context:`` section for every note on an open,
        non-focused task, and the single ``[i] No active process
        notes found.`` line when nothing is noted.

        The space analogue of a project's paused registry is the
        set of OPEN, NOTED, non-focused tasks (see
        :class:`_SpaceKeeperView`): those are exactly the tasks
        whose notes are still live process context.
        """
        # Imported lazily: core imports this module at load time.
        from .core import _render_notes_listing
        return _render_notes_listing(
            _SpaceKeeperView(self), self.load())

    def context_data(self) -> dict:
        """Plan + note ledger for a cross-space dashboard block.

        The read-side twin of :meth:`snapshot`: where the compact
        table needs only aggregates (focus id/title, counts, mtime),
        the verbose ``ck dashboard -v`` block needs the parsed plan
        and the note ledger to resolve the PREV / FOCUS / NEXT
        triad. Returning them together keeps the dashboard a pure
        RENDERER and keeps this module the single owner of space
        file I/O.
        """
        view = _SpaceKeeperView(self)
        return {
            "name": self.space.upper(),
            "path": str(space_path(self.space)),
            "tl": self.load(),
            "note": view.get_note(),
            "paused": view._paused_tasks(),
        }

    def edit(self) -> None:
        """Open the space's Markdown file in the user's editor.

        Editor resolution follows the project's standard
        logic (``cklib.config.get_editor``): the ``"editor"``
        key in the nearest project's ``.ck.json`` (walking up
        from the cwd), then ``$VISUAL``, ``$EDITOR``, then
        ``nano`` / ``vi``.

        DEV MODE: the sandboxed copy is opened (the real
        space file is never handed to the editor as a write
        target), mirroring ``ck edit`` for projects. The
        target comes from the same :func:`_write_path`
        helper every space mutation uses, so editor opens
        follow the identical dev-mode redirect and lazy
        directory creation contract.
        """
        target = _write_path(self.space)
        subprocess.run(
            [config.get_editor(), str(target)], check=False)

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
        """Append a timestamped process note to space task ``task_id``.

        The note ledger lives in the space's JSON sidecar
        (``<space>.json``) keyed by task ID, as an ordered array of
        ``{"text", "created"}`` entries — the analogue of a project's
        ``.ck/state.json`` ``notes`` ledger. Every call APPENDS; an
        existing note is never overwritten. Raises ``KeyError`` when
        the task does not exist and ``ValueError`` for empty text.
        Returns the stored note metadata dict.
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
        entries = notes.get(str(task_id))
        if not isinstance(entries, list):
            entries = []
        entry = note_store.append_entry(entries, note)
        notes[str(task_id)] = entries
        state["notes"] = notes
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._write_state(state)
        return {
            "id": task_id,
            "title": target.title,
            "note": note,
            "created": entry["created"],
            "notes": entries,
        }

    def get_notes(self, task_id: int) -> list[dict]:
        """All timestamped note entries for ``task_id``, in order."""
        return self._all_note_entries().get(task_id, [])

    def clear_notes(self, task_id: int) -> dict:
        """Remove every process note bound to space task ``task_id``."""
        tl = self.load()
        target = tl.by_id(task_id)
        if target is None:
            raise KeyError(f"No task with id {task_id}")
        state = self._read_state()
        notes = state.get("notes")
        removed = 0
        if isinstance(notes, dict):
            entries = notes.pop(str(task_id), None)
            if isinstance(entries, list):
                removed = len(entries)
            if removed:
                state["updated_at"] = datetime.now().isoformat(
                    timespec="seconds")
                self._write_state(state)
        return {"id": task_id, "title": target.title, "cleared": removed}

    def note_listing(self, task_id: int) -> str:
        """Render one space task's notes with full timestamps."""
        tl = self.load()
        target = tl.by_id(task_id)
        if target is None:
            raise KeyError(f"No task with id {task_id}")
        entries = self.get_notes(task_id)
        if not entries:
            from .ui import INFO, notice
            return notice(
                INFO, f"No process notes found for task [{task_id}].")
        lines = [f"[{target.id}] {target.title}"]
        from .core import format_note_time
        for entry in entries:
            stamp = format_note_time(entry["created"])
            lines.append(f"   * Note ({stamp}): {entry['text']}")
        return "\n".join(lines)

    def get_note(self, task_id: int) -> str:
        """The newest process-note text for ``task_id`` ("" when none)."""
        entries = self.get_notes(task_id)
        return entries[-1]["text"] if entries else ""

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


class _SpaceKeeperView:
    """Minimal keeper surface shared by the space renderers.

    The core renderers — ``cklib.core._render_local_status``
    (behind ``ck st``) and ``cklib.core._render_notes_listing``
    (behind ``ck notes``) — read a small, fixed set of things off
    their keeper. This adapter supplies the space equivalents so
    both engines render a space exactly as they render a project:

    - ``root.name`` — the space's display name (upper-cased,
      matching the dashboard's Space column);
    - ``get_note()`` — the process note bound to the focused
      task (the sidecar analogue of a project's
      ``.ck/state.json`` ``active_task`` entry);
    - ``_paused_tasks()`` — open, noted, non-focused tasks:
      the space analogue of the project's Unfocused / Paused
      Context ledger (spaces have no focus-loss registry, so
      a note on an open task that does not hold focus is the
      closest equivalent of a paused entry);
    - ``color_overrides()`` — spaces carry no ``.ck.json``,
      so every palette slot inherits the terminal's native
      color;
    - ``start_command()`` — spaces are driven by
      ``ck <space> start <ID>``, so the "no active focus"
      guidance names the spelling that is valid here.
    """

    def __init__(self, mgr: "SpaceManager") -> None:
        self._mgr = mgr
        self.root = SimpleNamespace(name=mgr.space.upper())

    def start_command(self) -> str:
        return f"ck {self._mgr.space} start"

    def get_note(self) -> Optional[dict]:
        focus_id = self._mgr.current_focus_id()
        if focus_id is None:
            return None
        entries = self._mgr.get_notes(focus_id)
        if not entries:
            return None
        return {
            "id": focus_id,
            "note": entries[-1]["text"],
            "notes": entries,
        }

    def get_note_entries(self, task_id: int) -> list[dict]:
        return self._mgr.get_notes(task_id)

    def _paused_tasks(self) -> list[dict]:
        tl = self._mgr.load()
        focus_id = self._mgr.current_focus_id()
        out: list[dict] = []
        for t in tl.open:
            if focus_id is not None and t.id == focus_id:
                continue
            entries = self._mgr.get_notes(t.id)
            if entries:
                out.append({
                    "id": t.id,
                    "title": t.title,
                    "note": entries[-1]["text"],
                    "notes": entries,
                })
        return out

    def color_overrides(self) -> dict:
        return {}


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


def list_space_notes(space: str) -> str:
    """Render ``space``'s active process notes (``ck <space> notes``)."""
    return SpaceManager(space).notes()


def space_snapshot(space: str) -> dict:
    """Aggregate the state of ``space`` for the dashboard table."""
    return SpaceManager(space).snapshot()


def space_context(space: str) -> dict:
    """Plan + note ledger of ``space`` for a dashboard block."""
    return SpaceManager(space).context_data()


__all__ = [
    "spaces_dir",
    "spaces_dirs",
    "space_path",
    "state_path",
    "history_path",
    "SPACE_HISTORY_SUFFIX",
    "is_space_history_name",
    "global_state_path",
    "PROJECT_SPACE",
    "SPACE_ORDER_KEY",
    "is_builtin_space",
    "existing_space_names",
    "available_space_names",
    "is_valid_space",
    "routable_space_name",
    "registered_project_for",
    "cleanup_stale_space_locks",
    "RESERVED_SPACE_NAMES",
    "read_space_order",
    "write_space_order",
    "space_order",
    "move_space",
    "swap_spaces",
    "reorder_spaces",
    "validate_new_space_name",
    "create_space",
    "ensure_default_spaces",
    "rename_space",
    "validate_deletable_space",
    "SpaceNotFoundError",
    "delete_space",
    "SpaceManager",
    "load_space",
    "add_space_task",
    "list_space",
    "list_space_notes",
    "space_snapshot",
    "space_context",
]
