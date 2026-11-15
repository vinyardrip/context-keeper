"""Global-space migration: legacy v0.12 layout → the v0.13 split.

A global space (``~/.config/ck/spaces/<name>.md``) historically kept
its completed ``[x]`` rows in the space file itself and its process
notes in a sibling JSON sidecar (``<name>.json``) — the exact
analogue of a project's pre-0.13 ``.ck/state.json`` monolith. This
migration brings EVERY space to the v0.13 project layout:

- regular spaces — active tasks (``[ ]`` / ``[>]`` / ``[~]``) stay in
  ``<name>.md``, each stamped with a stable UUID comment and its
  sidecar notes inlined as ``* Note (...)`` lines; completed ``[x]``
  tasks move to ``<name>.HISTORY.md`` as ``- [x] <ts>: <title>
  <!-- id: uuid -->`` ledger rows;
- DAILY spaces (``today``) — journals have NO history ledger: their
  ``[x]`` rows are DELETED permanently (the same rule the daily
  rollover applies). Active tasks keep UUIDs and inline notes;
- the sidecar ``<name>.json`` is deleted once its notes are inline;
- ``<space>.HISTORY.md`` files that already exist (archived-note
  records from ``ck delete -s``) are never treated as spaces.

Safety contract: the whole spaces directory is copied to
``~/.config/ck/spaces_backup_<tag>/`` BEFORE any mutation, and the
dry-run preview is the default. The tag (``0.13.1``) is the
migration release that introduced the spaces split — deliberately
independent of the project schema version, which stays ``0.13.0``.
"""

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .. import notes as note_store
from ..config import GLOBAL_CONFIG_DIR, SPACES_DIR_NAME, file_lock
from ..models import TaskStatus, history_timestamp, new_task_uuid
from ..parser import _atomic_write_text, parse_plan, render_plan
from .v0_12_to_v0_13 import strip_empty_headers

#: Backup-directory tag for the spaces migration (this release).
SPACES_BACKUP_TAG = "0.13.3"


@dataclass
class SpacePlan:
    """Dry-run analysis of one legacy space file.

    ``changed`` is True when the migration would touch the space at
    all (completed rows to move, notes to inline, missing UUIDs or a
    sidecar to retire); ``error`` carries a per-space failure so one
    broken space never aborts the batch.
    """

    name: str
    file: Path
    daily: bool
    done_count: int = 0
    active_count: int = 0
    note_count: int = 0
    uuid_missing: int = 0
    sidecar: Optional[Path] = None
    changed: bool = False
    error: Optional[str] = None


@dataclass
class SpacesMigrationResult:
    """Outcome of one spaces-migration run (see :class:`MigrationResult`
    of the project migration for the field semantics)."""

    dry_run: bool
    applied: bool = False
    backup_dir: Optional[str] = None
    messages: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    spaces: List[SpacePlan] = field(default_factory=list)

    def say(self, text: str) -> None:
        self.messages.append(text)

    def fail(self, text: str) -> None:
        self.errors.append(text)


def discover_space_files(directory: Path) -> List[Path]:
    """Every routable space ``*.md`` in ``directory`` (sorted).

    ``<space>.HISTORY.md`` archives are structurally excluded (see
    :func:`cklib.spaces.is_space_history_name`), so a history file can
    never be migrated as if it were a plan.
    """
    from ..spaces import is_space_history_name

    try:
        return sorted(
            p for p in directory.glob("*.md")
            if p.is_file() and not is_space_history_name(p.stem)
        )
    except OSError:
        return []


def _daily_space_names() -> frozenset:
    """The reserved daily-journal space names (``today`` for now)."""
    from ..spaces import DAILY_SPACES
    return DAILY_SPACES


def read_sidecar_notes(sidecar: Optional[Path]) -> Dict[int, List[dict]]:
    """Normalized note entries keyed by task ID ({} when none).

    The legacy sidecar may store a single string per task; the shared
    ``note_store.migrate_notes`` normalizes both shapes in memory.
    The file itself is never rewritten here.
    """
    if sidecar is None or not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    fallback = note_store.file_mtime_iso(sidecar)
    state = note_store.migrate_notes(dict(data), fallback)
    raw = state.get("notes")
    if not isinstance(raw, dict):
        return {}
    out: Dict[int, List[dict]] = {}
    for key, value in raw.items():
        try:
            tid = int(key)
        except (TypeError, ValueError):
            continue
        entries = [
            {"text": entry.get("text"), "created": entry.get("created", "")}
            for entry in (value if isinstance(value, list) else [])
            if isinstance(entry, dict) and entry.get("text")
        ]
        if entries:
            out[tid] = entries
    return out


class MigrateSpacesV012ToV013:
    """Legacy global spaces → v0.13 split (active-only md + HISTORY)."""

    def run(self, *, dry_run: bool = True) -> SpacesMigrationResult:
        """Preview (default) or apply the spaces migration."""
        directory = GLOBAL_CONFIG_DIR / SPACES_DIR_NAME
        # Dev-mode aware: a sandbox session migrates ITS copy only.
        try:
            from ..spaces import spaces_dir
            directory = spaces_dir()
        except Exception:
            pass

        result = SpacesMigrationResult(dry_run=dry_run)
        if not directory.is_dir():
            result.say(
                f"No spaces directory at {directory}; nothing to migrate.")
            result.applied = True
            return result

        for path in discover_space_files(directory):
            result.spaces.append(self._analyze(path))

        changed = [s for s in result.spaces if s.error is None and s.changed]
        broken = [s for s in result.spaces if s.error is not None]
        clean = [
            s for s in result.spaces
            if s.error is None and not s.changed
        ]

        result.say(
            f"Spaces migration plan for {directory} (schema -> v0.13):")
        for plan in changed:
            result.say(f"  - {self._describe(plan)}")
        for plan in clean:
            result.say(f"  - {plan.name}: nothing to migrate")
        for plan in broken:
            result.say(f"  - {plan.name}: skipped ({plan.error})")
            result.fail(f"{plan.name}: {plan.error}")
        if not changed and not broken:
            result.say("  - every space is already at v0.13; nothing to do.")
        if broken:
            return result
        if dry_run:
            result.say(
                "Dry run: nothing written. Re-run with --apply.")
            return result
        if not changed:
            result.applied = True
            return result

        self.upgrade(directory, changed, result)
        return result

    def upgrade(self, directory: Path, plans: List[SpacePlan],
                result: SpacesMigrationResult) -> None:
        """Apply the migration to every planned space."""
        backup = self._backup_dir(directory)
        try:
            shutil.copytree(directory, backup)
            result.backup_dir = str(backup)
            result.say(f"Backup written: {backup}")
        except OSError as exc:
            result.fail(f"Backup failed, aborting migration: {exc}")
            return

        ts = history_timestamp()
        for plan in plans:
            try:
                self._migrate_space(plan, ts)
            except (OSError, ValueError) as exc:
                result.fail(f"{plan.name}: migration failed: {exc}")
                continue
            result.say(f"  - {self._describe(plan)}: done.")
        if not result.errors:
            result.applied = True

    # ------------------------------------------------------------------ #
    # Analysis
    # ------------------------------------------------------------------ #

    def _analyze(self, path: Path) -> SpacePlan:
        """Dry-run analysis of one space file (never writes)."""
        name = path.name[:-len(".md")]
        plan = SpacePlan(
            name=name,
            file=path,
            daily=name in _daily_space_names(),
        )
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            plan.error = f"cannot read the file: {exc}"
            return plan
        sidecar = path.with_suffix(".json")
        if sidecar.is_file():
            plan.sidecar = sidecar
        try:
            tl = parse_plan(text)
        except ValueError as exc:
            plan.error = f"cannot parse the file: {exc}"
            return plan

        tasks = tl.tasks
        plan.done_count = sum(
            1 for t in tasks if t.status == TaskStatus.DONE)
        plan.active_count = sum(1 for t in tasks if t.status.is_active)
        if plan.daily:
            # Journals drop their completed rows permanently: only the
            # surviving tasks count for notes and UUID stamping.
            survivors = [t for t in tasks if t.status != TaskStatus.DONE]
        else:
            # Completed rows take their notes + UUIDs into HISTORY.md.
            survivors = tasks
        plan.uuid_missing = sum(1 for t in survivors if not t.uuid)
        notes_map = read_sidecar_notes(plan.sidecar)
        plan.note_count = sum(
            len(notes_map.get(t.id, ())) for t in survivors)
        plan.changed = bool(
            plan.done_count or plan.note_count or plan.uuid_missing
            or plan.sidecar is not None)
        return plan

    def _describe(self, plan: SpacePlan) -> str:
        """One compact report line for the plan/apply output."""
        pieces: List[str] = []
        if plan.daily:
            pieces.append(
                f"{plan.name} (daily): delete {plan.done_count} completed "
                "task(s) permanently (journals keep no history)")
        else:
            pieces.append(
                f"{plan.name}: move {plan.done_count} completed task(s) "
                f"to {plan.name}.HISTORY.md")
        if plan.note_count:
            pieces.append(f"inline {plan.note_count} note(s)")
        if plan.uuid_missing:
            pieces.append(f"stamp {plan.uuid_missing} UUID(s)")
        if plan.sidecar is not None:
            pieces.append(f"delete {plan.sidecar.name}")
        return "; ".join(pieces)

    # ------------------------------------------------------------------ #
    # Apply
    # ------------------------------------------------------------------ #

    def _migrate_space(self, plan: SpacePlan, ts: str) -> None:
        """Rewrite one space file per the analyzed plan."""
        try:
            text = plan.file.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"cannot read the file: {exc}") from None
        tl = parse_plan(text)
        notes_map = read_sidecar_notes(plan.sidecar)

        # 1) Attach legacy sidecar notes + UUIDs BEFORE anything is
        #    deleted, so a completed task's notes travel into
        #    HISTORY.md with it (mirrors the project migration).
        #    DAILY spaces stamp UUIDs on surviving tasks only: their
        #    completed rows are deleted, never archived.
        done_tasks = []
        for task in tl.tasks:
            entries = notes_map.get(task.id)
            if entries:
                existing = {e.get("text") for e in (task.notes or ())}
                merged = list(task.notes or ())
                for entry in entries:
                    if entry["text"] not in existing:
                        merged.append(dict(entry))
                        existing.add(entry["text"])
                task.notes = merged
            needs_uuid = not task.uuid and (
                not plan.daily or task.status != TaskStatus.DONE)
            if needs_uuid:
                task.uuid = new_task_uuid()
            if task.status == TaskStatus.DONE:
                done_tasks.append(task)

        # 2) Completed tasks -> the space's HISTORY.md ledger. DAILY
        #    spaces are journals: their done rows are deleted, never
        #    archived.
        if not plan.daily:
            history_file = plan.file.with_suffix(".HISTORY.md")
            from ..parser import append_to_history
            for task in done_tasks:
                append_to_history(
                    history_file, ts, task.title, uuid=task.uuid,
                    notes=[dict(e) for e in (task.notes or ())])

        # 3) Drop the completed rows from the plan, strip the empty
        #    headers they leave behind, and write atomically under
        #    the space's fail-closed lock.
        # Delete highest id first: tl.delete() shifts positional ids,
        # so removing from the top keeps the remaining ids stable.
        for task in sorted(done_tasks, key=lambda t: t.id, reverse=True):
            tl.delete(task.id)
        if plan.done_count or plan.note_count or plan.uuid_missing:
            with file_lock(plan.file):
                _atomic_write_text(
                    plan.file, render_space(tl, plan.name))

        # 4) Retire the sidecar: every note it held is inline now.
        if plan.sidecar is not None:
            plan.sidecar.unlink(missing_ok=True)

    @staticmethod
    def _backup_dir(directory: Path) -> Path:
        """``spaces_backup_<tag>/`` next to the spaces dir (deduped)."""
        base = directory.parent / f"spaces_backup_{SPACES_BACKUP_TAG}"
        candidate = base
        seq = 2
        while candidate.exists():
            candidate = directory.parent / (
                f"spaces_backup_{SPACES_BACKUP_TAG}_{seq}")
            seq += 1
        return candidate


def render_space(tl, name: str) -> str:
    """Render a migrated space document.

    The completed rows are gone and their emptied ``## Completed``
    header is stripped (``strip_empty_headers``). When the migration
    emptied the document completely, the canonical empty scaffold is
    written so ``ck <space> add`` keeps a home — mirroring
    :func:`cklib.spaces.SpaceManager.load`.
    """
    from ..spaces import _EMPTY_SPACE
    text = strip_empty_headers(render_plan(tl))
    if not text.strip():
        text = _EMPTY_SPACE.format(space=name)
    return text
