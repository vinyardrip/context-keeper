"""v0.12 → v0.13.0 project layout migration.

Upgrades a legacy ``.ck/`` project (the pre-0.13 ``state.json``
monolith) to the v0.13.0 split layout:

- ``PLAN.md``      — active tasks only (``[ ]`` / ``[>]`` / ``[~]``),
  every task carrying a stable ``<!-- id: uuid -->`` comment and its
  process notes as INLINE ``* Note (...)`` lines;
- ``HISTORY.md``   — completed tasks as ``- [x] <ts>: <title>`` ledger
  entries (moved out of the plan);
- ``JOURNAL.md``   — created empty for ``ck save`` checkpoints;
- ``version`` / ``meta.json`` / ``links.json`` / ``AGENTS.md`` —
  the v0.13 metadata set;
- ``state.json``   — DELETED (its notes move into PLAN.md, its
  registry fields become obsolete).

Safety contract: a full ``.ck_backup_<version>/`` copy of ``.ck/`` is
written BEFORE any mutation, failures are logged to
``.ck/migrate.log``, and the dry-run preview is the default.
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .. import VERSION
from ..config import (
    AGENTS_FILENAME,
    CK_DIR_NAME,
    DEFAULT_AGENTS_MD,
    DEFAULT_CK_GITIGNORE,
    GITIGNORE_FILENAME,
    JOURNAL_FILENAME,
    LINKS_FILENAME,
    META_FILENAME,
    VERSION_FILENAME,
)
from ..models import TaskStatus, history_timestamp, new_task_uuid
from ..notes import file_mtime_iso
from ..parser import parse_plan_file, write_plan
from .base import Migration, MigrationResult

LEGACY_STATE_FILENAME = "state.json"
COMPLETED_SECTION_TITLE = "completed"


class MigrateV012ToV013(Migration):
    """Legacy ``state.json`` project → v0.13.0 split layout."""

    target_version = VERSION

    def run(self, project_root: Path, *,
            dry_run: bool = True) -> MigrationResult:
        root = Path(project_root)
        ck = root / CK_DIR_NAME
        result = MigrationResult(dry_run=dry_run)
        plan_file = ck / "PLAN.md"

        if not ck.is_dir() or not plan_file.is_file():
            result.fail(
                f"No .ck/ project found at {root} "
                "(nothing to migrate; run `ck init` first).")
            return result

        found = self._stored_version(ck)
        if found == VERSION:
            result.say(
                f"Project is already at v{VERSION}; nothing to migrate.")
            result.applied = True
            return result

        try:
            tl = parse_plan_file(plan_file)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            result.fail(f"Cannot parse {plan_file}: {exc}")
            return result

        done_tasks = [t for t in tl.tasks if t.status == TaskStatus.DONE]
        active_count = sum(1 for t in tl.tasks if t.status.is_active)
        state = self._read_state(ck)
        notes_map, note_errors = self._collect_notes(state, ck)
        for error in note_errors:
            result.say(f"  ! {error}")
        note_count = sum(len(v) for v in notes_map.values())

        result.say(
            f"Migration plan for {root} (schema -> v{VERSION}):")
        result.say(
            f"  - move {len(done_tasks)} completed task(s) from "
            "PLAN.md into HISTORY.md (timestamped at migration time)")
        result.say(
            f"  - keep {active_count} active task(s) in PLAN.md, "
            "stamping each with a stable UUID")
        result.say(
            f"  - inline {note_count} legacy process note(s) from "
            "state.json into PLAN.md")
        result.say(
            f"  - create {JOURNAL_FILENAME} / {VERSION_FILENAME} / "
            f"{META_FILENAME} / {LINKS_FILENAME} / {AGENTS_FILENAME}")
        result.say("  - delete state.json")

        if dry_run:
            result.say("Dry run: nothing written. Re-run with --apply.")
            return result

        self.upgrade(root, result)
        return result

    # ------------------------------------------------------------------ #
    # Apply
    # ------------------------------------------------------------------ #

    def upgrade(self, root: Path, result: MigrationResult) -> None:
        """Perform the real upgrade (base-class contract signature).

        Re-derives the plan AST and legacy state (``run()`` validated
        them moments before; parsing twice is cheap and keeps this
        method callable standalone).
        """
        ck = root / CK_DIR_NAME
        log_lines: list[str] = []

        # 1) Backup FIRST: the whole .ck/ directory, byte-for-byte.
        backup = self._backup_dir(root)
        try:
            shutil.copytree(ck, backup)
            result.backup_dir = str(backup)
            result.say(f"Backup written: {backup}")
        except OSError as exc:
            message = f"Backup failed, aborting migration: {exc}"
            result.fail(message)
            log_lines.append(message)
            self._write_log(ck, log_lines)
            return

        ts = history_timestamp()

        try:
            tl = parse_plan_file(ck / "PLAN.md")
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            message = f"PLAN.md re-read failed: {exc}"
            result.fail(message)
            log_lines.append(message)
            self._write_log(ck, log_lines)
            return

        state = self._read_state(ck)
        notes_map, note_errors = self._collect_notes(state, ck)
        log_lines.extend(note_errors)
        done_tasks = [t for t in tl.tasks if t.status == TaskStatus.DONE]

        # 2) Attach legacy notes + UUIDs to the AST FIRST, so a
        #    completed task's notes travel into HISTORY.md with it.
        try:
            self._apply_plan_mutations(tl, notes_map)
        except (OSError, ValueError) as exc:
            message = f"PLAN.md note migration failed: {exc}"
            result.fail(message)
            log_lines.append(message)
            self._write_log(ck, log_lines)
            return

        # 3) Completed tasks -> HISTORY.md ledger.
        try:
            self._write_history(ck, done_tasks, ts)
            result.say(
                f"HISTORY.md: appended {len(done_tasks)} completed "
                "task(s).")
        except OSError as exc:
            message = f"HISTORY.md write failed: {exc}"
            result.fail(message)
            log_lines.append(message)

        # 4) PLAN.md: drop the completed rows, write inline notes.
        try:
            self._finalize_plan(ck / "PLAN.md", tl)
            result.say(
                f"PLAN.md: {len(done_tasks)} completed task(s) removed, "
                f"{sum(len(v) for v in notes_map.values())} note(s) "
                "inlined, UUIDs ensured.")
        except (OSError, ValueError) as exc:
            message = f"PLAN.md rewrite failed: {exc}"
            result.fail(message)
            log_lines.append(message)

        # 5) Metadata files.
        try:
            self._write_metadata(root, ck, state)
            result.say(
                f"Created {VERSION_FILENAME} / {META_FILENAME} / "
                f"{LINKS_FILENAME} / {AGENTS_FILENAME} / "
                f"{JOURNAL_FILENAME} (missing ones only).")
        except OSError as exc:
            message = f"Metadata write failed: {exc}"
            result.fail(message)
            log_lines.append(message)

        # 6) Retire the legacy registry.
        try:
            (ck / LEGACY_STATE_FILENAME).unlink(missing_ok=True)
            result.say("state.json deleted.")
        except OSError as exc:
            message = f"state.json delete failed: {exc}"
            result.fail(message)
            log_lines.append(message)

        if not result.errors:
            result.applied = True
            result.say(f"Migration complete (backup kept at {backup}).")
        self._write_log(ck, log_lines)

    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #

    @staticmethod
    def _backup_dir(root: Path) -> Path:
        """``.ck_backup_<version>/`` (deduped ``_2``/``_3``… when taken)."""
        base = root / f"{CK_DIR_NAME}_backup_{VERSION}"
        candidate = base
        seq = 2
        while candidate.exists():
            candidate = root / f"{CK_DIR_NAME}_backup_{VERSION}_{seq}"
            seq += 1
        return candidate

    @staticmethod
    def _stored_version(ck: Path):
        """The project's stored schema version, or None."""
        vfile = ck / VERSION_FILENAME
        try:
            if vfile.is_file():
                text = vfile.read_text(encoding="utf-8").strip()
                if text:
                    return text
            mfile = ck / META_FILENAME
            if mfile.is_file():
                data = json.loads(mfile.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    value = data.get("schema_version")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        except (OSError, ValueError):
            return None
        return None

    @staticmethod
    def _read_state(ck: Path) -> dict:
        """The legacy ``state.json`` payload ({} when absent/corrupt)."""
        state_file = ck / LEGACY_STATE_FILENAME
        if not state_file.is_file():
            return {}
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _collect_notes(state: dict, ck: Path):
        """Extract every legacy note entry, keyed by legacy task ID.

        Sources (in order): the canonical ``notes`` ledger, the
        ``active_task.note`` scalar, and ``paused_tasks[].note``.
        Returns ``(notes_map, errors)`` — ``notes_map`` maps int task
        IDs to ``{"text", "created"}`` entry lists.
        """
        errors: list[str] = []
        notes_map: dict[int, list] = {}
        if not state:
            return notes_map, errors

        def attach(tid, entries):
            if not isinstance(tid, int) or isinstance(tid, bool):
                return
            if not entries:
                return
            notes_map.setdefault(tid, [])
            seen_texts = {e.get("text") for e in notes_map[tid]}
            for entry in entries:
                if entry.get("text") not in seen_texts:
                    notes_map[tid].append(entry)
                    seen_texts.add(entry.get("text"))

        ledger = state.get("notes")
        if ledger is not None and not isinstance(ledger, dict):
            errors.append(
                "state.json: 'notes' is not an object; skipped.")
            ledger = None
        if isinstance(ledger, dict):
            for key, value in ledger.items():
                try:
                    tid = int(key)
                except (TypeError, ValueError):
                    continue
                if not isinstance(value, list):
                    continue
                entries = [
                    {"text": item.get("text"),
                     "created": item.get("created", "")}
                    for item in value
                    if isinstance(item, dict)
                    and isinstance(item.get("text"), str)
                    and item.get("text")
                ]
                attach(tid, entries)

        mtime = file_mtime_iso(ck / LEGACY_STATE_FILENAME)
        active = state.get("active_task")
        if isinstance(active, dict):
            note = active.get("note")
            if isinstance(note, str) and note:
                attach(active.get("id"),
                       [{"text": note, "created": mtime}])
        paused = state.get("paused_tasks")
        if isinstance(paused, list):
            for item in paused:
                if not isinstance(item, dict):
                    continue
                note = item.get("note")
                if isinstance(note, str) and note:
                    attach(item.get("id"),
                           [{"text": note, "created": mtime}])
        return notes_map, errors

    @staticmethod
    def _write_history(ck: Path, done_tasks, ts: str) -> None:
        """Append the legacy ``[x]`` rows to HISTORY.md (ledger order).

        Legacy rows carry no completion timestamps; the migration
        stamps the batch with the migration time so every entry keeps
        the ledger's ``- [x] <ts>: <title>`` shape.
        """
        from ..parser import append_to_history

        history_file = ck / "HISTORY.md"
        for task in done_tasks:
            append_to_history(
                history_file, ts, task.title, uuid=task.uuid or "",
                notes=[dict(entry) for entry in (task.notes or ())])

    @staticmethod
    def _apply_plan_mutations(tl, notes_map: dict) -> None:
        """Attach legacy notes + UUIDs to the AST (in-memory only).

        - every legacy note entry attaches to its (positional) task;
        - tasks without a UUID get one (legacy plans had none).

        Notes are attached BEFORE the HISTORY.md write so a completed
        task's process context travels into the durable record with
        it; the UUIDs make the moved entries resolvable forever.
        """
        if tl is None:
            raise ValueError("Plan AST missing; cannot migrate PLAN.md")

        for task in tl.tasks:
            entries = notes_map.get(task.id)
            if entries:
                task.notes = list(entries)
            if not task.uuid:
                task.uuid = new_task_uuid()

    @staticmethod
    def _finalize_plan(plan_file: Path, tl) -> None:
        """Write PLAN.md: no ``[x]`` rows, no empty ``## Completed``.

        Completed tasks are dropped (their content lives in
        HISTORY.md now) and a ``## Completed`` header whose section
        is left empty disappears with its section.
        """
        if tl is None:
            raise ValueError("Plan AST missing; cannot rewrite PLAN.md")

        completed_headers = [
            s.line_number for s in tl.sections
            if s.title.strip().lower() == COMPLETED_SECTION_TITLE
        ]
        done = [t for t in tl.tasks if t.status == TaskStatus.DONE]
        for task in done:
            tl.delete(task.id)
        # Drop a Completed header whose section is now empty.
        remaining_in_section = {
            t.line_number for t in tl.tasks
            if (t.section or "").strip().lower()
            == COMPLETED_SECTION_TITLE
        }
        if not remaining_in_section:
            for header_line in completed_headers:
                tl.removed_lines.add(header_line)
        write_plan(plan_file, tl)

    @staticmethod
    def _write_metadata(root: Path, ck: Path, state: dict) -> None:
        """Create the v0.13 metadata set (missing files only)."""
        def put(path: Path, text: str) -> None:
            if not path.exists():
                path.write_text(text, encoding="utf-8")

        put(ck / JOURNAL_FILENAME, "")
        put(ck / VERSION_FILENAME, f"{VERSION}\n")
        put(ck / LINKS_FILENAME, "{}\n")
        put(ck / AGENTS_FILENAME, DEFAULT_AGENTS_MD)
        meta = {}
        created_at = state.get("created_at") if isinstance(state, dict) \
            else None
        meta["created_at"] = (
            created_at if isinstance(created_at, str) and created_at
            else datetime.now(timezone.utc).isoformat(
                timespec="seconds"))
        meta["project_name"] = root.name
        meta["created_with_ck"] = VERSION
        tools = state.get("tools") if isinstance(state, dict) else None
        meta["tools"] = tools if isinstance(tools, dict) else {}
        meta_path = ck / META_FILENAME
        if not meta_path.exists():
            meta_path.write_text(
                json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        put(ck / GITIGNORE_FILENAME, DEFAULT_CK_GITIGNORE)

    @staticmethod
    def _write_log(ck: Path, log_lines: list) -> None:
        """Append migration errors to ``.ck/migrate.log``."""
        if not log_lines:
            return
        log = ck / "migrate.log"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        try:
            with log.open("a", encoding="utf-8") as fh:
                for line in log_lines:
                    fh.write(f"[{stamp}] {line}\n")
        except OSError:
            pass
