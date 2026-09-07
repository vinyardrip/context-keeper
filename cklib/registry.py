"""Global project registry stored at ``~/.config/ck/projects.json``.

Concurrent safety: every read/write is wrapped in
:func:`cklib.config.file_lock` so concurrent processes and threads
serialise on a sibling ``.lock`` file. The lock is FAIL-CLOSED: if it
cannot be acquired within the timeout the operation aborts with
:class:`cklib.config.LockTimeoutError` instead of proceeding without
mutual exclusion (which would turn a read-modify-write into a lost
update).

Notes from prior security reviews:

- Legacy ``~/.ckrc`` is migrated **once** and **unlinked** immediately
  to eliminate per-load I/O overhead.
- A ``projects.json`` that fails JSON parsing is **never** silently
  reset: the corrupt file is quarantined to a timestamped ``.bak``
  before a fresh registry is initialized, and if the quarantine fails
  the operation aborts (see :class:`RegistryCorruptError`).
- Timestamps use microsecond precision and include the local
  timezone offset for unambiguous ordering.
- ``list_projects()`` sorts by ``last_seen`` (descending), with a
  stable secondary sort by project name for deterministic order
  when timestamps collide.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from .config import (
    GLOBAL_CONFIG_DIR,
    GLOBAL_REGISTRY_FILE,
    LEGACY_GLOBAL_CONFIG_FILE,
    file_lock,
)


class RegistryCorruptError(RuntimeError):
    """Raised when the registry file is corrupt AND cannot be
    quarantined. Mutations must abort rather than risk overwriting
    user data with an empty registry."""


def _quarantine_corrupt(target: Path) -> None:
    """Move a corrupt registry file aside (timestamped backup).

    Preserves the user's data for manual recovery and lets the next
    write start from a clean slate. Raises
    :class:`RegistryCorruptError` if the file cannot be moved so the
    caller aborts instead of overwriting it.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = target.with_name(f"{target.name}.corrupt-{ts}.bak")
    try:
        os.replace(target, backup)
    except OSError as e:
        raise RegistryCorruptError(
            f"Registry file {target} is corrupt and could not be "
            f"backed up ({e}); aborting to avoid data loss"
        ) from e
    print(
        f"\u26a0\ufe0f  Corrupt registry detected; original moved to "
        f"{backup}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO-8601 timestamp with microsecond precision + tz offset."""
    return datetime.now().astimezone().isoformat()


def _ts_key(iso: str) -> int:
    """Convert an ISO timestamp to a comparable integer (epoch ns)."""
    if not iso:
        return 0
    try:
        dt = datetime.fromisoformat(iso)
        return int(dt.timestamp() * 1_000_000_000)
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass
class ProjectEntry:
    """A single registered project.

    Only the spec-required fields are used for sorting/display:
    ``name``, ``path``, ``last_seen``. The other fields are kept for
    backwards compatibility with existing registries on disk.
    """

    name: str
    path: str
    registered_at: str
    last_seen: str = ""
    active_task_id: int | None = None
    active_task_title: str = ""

    def touch(self) -> None:
        self.last_seen = _now_iso()


@dataclass
class Registry:
    """In-memory representation of the registry file."""

    projects: list[ProjectEntry] = field(default_factory=list)

    # ---- persistence ----

    def save(self, path: Path | None = None) -> None:
        """Atomic-replace write. No lock taken — caller is responsible."""
        target = path or GLOBAL_REGISTRY_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            target,
            {"projects": [asdict(p) for p in self.projects]},
        )

    @classmethod
    def load(cls, path: Path | None = None, *, locked: bool = True) -> "Registry":
        """Read registry from ``path`` (default: global registry file).

        When ``locked`` is True (default), the read is wrapped in
        ``file_lock`` so concurrent writers cannot tear the read.
        Pass ``locked=False`` when calling from a context that
        already holds the lock.
        """
        target = path or GLOBAL_REGISTRY_FILE

        def _do_load() -> "Registry":
            migrated = cls._migrate_legacy_if_needed(target)
            if migrated is not None:
                return migrated

            if not target.exists():
                return cls()
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # Never silently wipe a corrupt registry: quarantine
                # it (timestamped backup) and start from empty. If
                # quarantining fails, abort loudly.
                _quarantine_corrupt(target)
                return cls()
            except OSError:
                return cls()
            if not isinstance(data, dict):
                # Structurally invalid (e.g. a JSON list): same
                # quarantine path as unparsable content.
                _quarantine_corrupt(target)
                return cls()
            projects_raw = data.get("projects", [])
            if not isinstance(projects_raw, list):
                _quarantine_corrupt(target)
                return cls()
            return cls(projects=[
                _entry_from_dict(item) for item in projects_raw
                if isinstance(item, dict)
            ])

        if locked:
            with file_lock(target):
                return _do_load()
        return _do_load()

    @classmethod
    def _migrate_legacy_if_needed(cls, target: Path) -> "Registry | None":
        """If the new registry file is absent but legacy ``~/.ckrc``
        exists with valid JSON, migrate its content into ``target``
        and **delete** the legacy file.
        """
        if target.exists():
            return None
        if not LEGACY_GLOBAL_CONFIG_FILE.exists():
            return None
        try:
            raw = LEGACY_GLOBAL_CONFIG_FILE.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict) or "projects" not in data:
            return None

        reg = cls()
        for item in data.get("projects", []):
            if isinstance(item, dict):
                reg.projects.append(_entry_from_dict(item))
        reg.save(target)

        try:
            LEGACY_GLOBAL_CONFIG_FILE.unlink()
        except OSError:
            pass
        return reg


def _entry_from_dict(d: dict) -> ProjectEntry:
    return ProjectEntry(
        name=d.get("name", ""),
        path=d.get("path", ""),
        registered_at=d.get("registered_at", ""),
        last_seen=d.get("last_seen", ""),
        active_task_id=d.get("active_task_id"),
        active_task_title=d.get("active_task_title", ""),
    )


# ---------------------------------------------------------------------------
# Locked module-level helpers
# ---------------------------------------------------------------------------


def ensure_global_dir() -> Path:
    """Ensure ``~/.config/ck/`` exists. Returns the directory."""
    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return GLOBAL_CONFIG_DIR


def _locked_modify(path: Path, mutator) -> Registry:
    """Read-modify-write helper under ``file_lock``.

    Loads the registry inside the lock (without recursive locking),
    runs ``mutator(reg)`` to mutate the in-memory state, then saves.
    The atomic-replace save means readers either see the old
    content or the new content, never a partial write.
    """
    ensure_global_dir()
    with file_lock(path):
        reg = Registry.load(path, locked=False)
        mutator(reg)
        reg.save(path)
        return reg


def register_project(path: Path, name: Optional[str] = None) -> ProjectEntry:
    """Register (or refresh) ``path`` in the global registry."""
    abs_path = str(Path(path).resolve())
    project_name = name or Path(abs_path).name
    now = _now_iso()

    def mutate(reg: Registry) -> None:
        existing = next(
            (p for p in reg.projects if p.path == abs_path), None
        )
        if existing is not None:
            existing.last_seen = now
            existing.name = project_name or existing.name
            return
        reg.projects.append(
            ProjectEntry(
                name=project_name,
                path=abs_path,
                registered_at=now,
                last_seen=now,
            )
        )

    reg = _locked_modify(GLOBAL_REGISTRY_FILE, mutate)
    # Return the entry we just touched/created.
    return next(p for p in reg.projects if p.path == abs_path)


def update_active_task(path: Path, task_id: int | None, task_title: str) -> None:
    """Update the active-task fields for a registered project."""
    abs_path = str(Path(path).resolve())

    def mutate(reg: Registry) -> None:
        for entry in reg.projects:
            if entry.path == abs_path:
                entry.active_task_id = task_id
                entry.active_task_title = task_title
                entry.touch()

    _locked_modify(GLOBAL_REGISTRY_FILE, mutate)


def remove_project(path: Path) -> bool:
    """Remove a project from the registry by absolute path.

    Returns True if the path was present (i.e. actually removed).
    Missing-disk-path is fine — we only mutate the registry. The
    presence check and the removal happen in a SINGLE locked
    read-modify-write pass (no TOCTOU between check and mutation,
    no second lock acquisition).
    """
    abs_path = str(Path(path).resolve())
    result = {"was_present": False}

    def mutate(reg: Registry) -> None:
        before = [p.path for p in reg.projects]
        if abs_path in before:
            result["was_present"] = True
        reg.projects = [p for p in reg.projects if p.path != abs_path]

    _locked_modify(GLOBAL_REGISTRY_FILE, mutate)
    return result["was_present"]


def remove_project_by_name(name: str) -> bool:
    """Remove registered project(s) by display name in one locked pass.

    Returns True if anything was removed. The name match and the
    removal happen in a SINGLE locked read-modify-write pass —
    previously this listed (lock #1) then removed each match one
    by one (lock #2..N), a TOCTOU window in which a concurrent
    registration could be silently dropped.
    """
    result = {"removed_any": False}

    def mutate(reg: Registry) -> None:
        kept = [p for p in reg.projects if p.name != name]
        if len(kept) != len(reg.projects):
            result["removed_any"] = True
        reg.projects = kept

    _locked_modify(GLOBAL_REGISTRY_FILE, mutate)
    return result["removed_any"]


def list_projects() -> List[ProjectEntry]:
    """All registered projects, sorted by ``last_seen`` (newest first)
    with a stable secondary sort by ``name`` ascending.
    """
    reg = Registry.load()
    return sorted(
        reg.projects,
        key=lambda p: (
            -_ts_key(p.last_seen),  # descending
            p.name,                  # ascending (stable tiebreaker)
        ),
    )


def prune_missing() -> list[str]:
    """Remove entries whose paths no longer exist on disk.

    Returns the list of paths that were pruned.

    Disk ``stat`` calls happen OUTSIDE the lock (they can be slow on
    network filesystems and would widen the lock-contention window
    for every other ``ck`` process); only the in-memory reconcile
    against the freshly loaded registry runs under the lock. A path
    that vanishes between the stat and the locked pass is simply
    re-detected on the next prune.
    """
    ensure_global_dir()

    # Phase 1 (unlocked): gather candidates whose paths are missing.
    reg_snapshot = Registry.load()
    missing: set[str] = set()
    for entry in reg_snapshot.projects:
        try:
            if not Path(entry.path).exists():
                missing.add(entry.path)
        except OSError:
            # Treat any OS error on stat as "missing" — be safe.
            missing.add(entry.path)

    if not missing:
        return []

    # Phase 2 (locked): pure in-memory reconcile — remove entries that
    # are BOTH still present in the registry AND known-missing on
    # disk. No disk I/O inside the critical section.
    pruned: list[str] = []

    def mutate(reg: Registry) -> None:
        nonlocal pruned
        kept: list[ProjectEntry] = []
        for entry in reg.projects:
            if entry.path in missing:
                pruned.append(entry.path)
                continue
            kept.append(entry)
        reg.projects = kept

    _locked_modify(GLOBAL_REGISTRY_FILE, mutate)
    return pruned


def all_paths() -> Iterable[Path]:
    """Yield Path objects for every registered project."""
    return (Path(p.path) for p in list_projects())


# ---------------------------------------------------------------------------
# Atomic writer
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Durable atomic JSON write: tmp + fsync + rename.

    - Target symlinks are resolved first (the link survives).
    - The original file's permissions are preserved (mkstemp's 0600
      would otherwise demode the registry on every write).
    - Data is flushed and fsync'ed before the rename so a crash
      cannot leave a renamed-but-empty registry.
    """
    real = path
    try:
        if path.is_symlink():
            real = path.resolve()
    except OSError:
        real = path
    real.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = os.stat(real).st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp_name = tempfile.mkstemp(
        dir=str(real.parent), prefix=".ck-registry-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
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
    "ProjectEntry",
    "Registry",
    "RegistryCorruptError",
    "ensure_global_dir",
    "register_project",
    "update_active_task",
    "remove_project",
    "remove_project_by_name",
    "list_projects",
    "all_paths",
    "prune_missing",
]