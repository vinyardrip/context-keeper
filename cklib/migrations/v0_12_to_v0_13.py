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

The plan is also standardized: a symlinked ``.ck/PLAN.md`` (the
dev-mode ``.ck/PLAN.md -> ROADMAP.md`` link) is replaced by a REAL
file first — the shared atomic-write helper resolves symlinks, so a
write through the link would mutate the target instead. Legacy
active sections (``## Backlog`` / ``## Active`` / custom) are
merged into one ``## Current Sprint``, and every header left with
an empty section (``### ...`` and the emptied ``## Completed``) is
stripped afterwards.

Safety contract: a full ``.ck_backup_<version>/`` copy of ``.ck/`` is
written BEFORE any mutation, failures are logged to
``.ck/migrate.log``, and the dry-run preview is the default.
"""

import json
import re
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
    schema_compatible,
)
from ..models import TaskStatus, history_timestamp, new_task_uuid
from ..notes import file_mtime_iso
from ..parser import _atomic_write_text, parse_plan_file, render_plan
from .base import Migration, MigrationResult

LEGACY_STATE_FILENAME = "state.json"
COMPLETED_SECTION_TITLE = "completed"
SPRINT_SECTION_TITLE = "current sprint"


def stored_schema_version(ck: Path):
    """The ``.ck/`` directory's stored schema version, or None.

    Resolution order: the ``version`` file (single line), then
    ``meta.json``'s ``schema_version`` key. None means the project
    predates the split layout. Shared by the batch migration so the
    "already migrated" test can never drift from the migration's own
    check.
    """
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

# ``##`` and deeper headings (the h1 document title is never touched).
_HEADER_RE = re.compile(r"^(#{2,6})\s")


def _header_level(line: str) -> int:
    """Markdown heading level (2..6), or 0 for non-heading lines."""
    match = _HEADER_RE.match(line)
    return len(match.group(1)) if match else 0


def _header_title(line: str) -> str:
    """The heading text, lowercased (``## Backlog`` -> ``backlog``)."""
    return line.lstrip("#").strip().lower()


def dereference_plan_symlink(plan_file: Path) -> bool:
    """Replace a symlinked ``.ck/PLAN.md`` with a REAL file.

    The dev-mode entry links ``.ck/PLAN.md -> ROADMAP.md``, and the
    shared atomic-write helper deliberately RESOLVES symlinks (so
    dotfile-managed plans keep their links). A migration writing
    through that helper would therefore silently mutate the link
    target — the root ``ROADMAP.md``. Migration owns ``.ck/PLAN.md``
    exclusively: the linked content is materialized as a regular
    file and the link is removed. The target file itself is never
    opened for writing.

    Returns True when a symlink was replaced.
    """
    if not plan_file.is_symlink():
        return False
    text = plan_file.read_text(encoding="utf-8")
    plan_file.unlink()
    plan_file.write_text(text, encoding="utf-8")
    return True


def _trim_blanks(lines: list[str]) -> list[str]:
    """Copy of ``lines`` without leading/trailing blank lines."""
    body = list(lines)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return body


def _tidy(lines: list[str]) -> str:
    """Collapse blank runs, trim the edges, single trailing newline."""
    out: list[str] = []
    for line in lines:
        if not line.strip() and (not out or not out[-1].strip()):
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def normalize_plan_layout(text: str) -> tuple[str, list[str]]:
    """Merge every active section into one ``## Current Sprint``.

    Roadmap-style legacy plans spread active work across several
    sections (``## Backlog``, ``## Active``, custom ones). The v0.13
    plan carries a single canonical sprint section, so the migration
    folds every non-completed section into ``## Current Sprint``:

    - all active task lines (and their inline notes / prose) keep
      their original document order;
    - a legacy ``## Current Sprint`` section (when present) comes
      first and keeps the document's h1 / front matter;
    - ``## Completed`` sections are preserved for the AST pass —
      their ``[x]`` rows move into HISTORY.md and
      :func:`strip_empty_headers` then removes the empty headers;
    - when the legacy document has NO sprint section at all, the
      front matter (``# title`` / prose before the first section) is
      dropped: the link target (e.g. ``ROADMAP.md``) remains the
      titled document and the plan becomes the derived working list
      that starts with ``## Current Sprint``.

    Returns ``(normalized_text, merged_titles)`` — the second value
    lists the section titles folded into the sprint (empty when the
    document needed no change).
    """
    lines = text.splitlines()
    front: list[str] = []
    blocks: list[list] = []  # [header_line, level, title, body]
    current: list | None = None
    for line in lines:
        level = _header_level(line)
        if level:
            current = [line, level, _header_title(line), []]
            blocks.append(current)
        elif current is None:
            front.append(line)
        else:
            current[3].append(line)

    sprint: list | None = None
    completed: list[list] = []
    merged: list[list] = []
    in_completed = False
    for block in blocks:
        _, level, title, _body = block
        if level == 2:
            in_completed = title == COMPLETED_SECTION_TITLE
        if in_completed:
            completed.append(block)
        elif title == SPRINT_SECTION_TITLE:
            sprint = block
        else:
            merged.append(block)

    if sprint is None and not merged:
        return text, []  # nothing to standardize

    out: list[str] = []
    if sprint is not None:
        # Regular legacy plan: keep the document title / front prose.
        out.extend(_trim_blanks(front))
    out.append("## Current Sprint")
    for block in ([sprint] if sprint is not None else []) + merged:
        # Bodies concatenate without separators: a blank between two
        # task rows would split the sprint list, and the folded
        # sections form one flat working list.
        out.extend(_trim_blanks(block[3]))
    for block in completed:
        if out and out[-1].strip():
            out.append("")
        out.append(block[0])
        out.extend(block[3])
    return _tidy(out), [block[2] for block in merged]


def strip_empty_headers(text: str) -> str:
    """Drop headings whose section carries no content lines.

    After the completed tasks graduate into HISTORY.md, their headers
    would remain as empty scaffolding. Every heading of level >= 3
    (``###`` and deeper) is removed when its section holds nothing
    but blank lines; a level-2 heading is removed only when it is the
    ``## Completed`` section. The canonical ``## Current Sprint``
    scaffold is kept even when empty, so ``ck add`` always has a
    home. Blank-line runs are collapsed.
    """
    lines = text.splitlines()
    idxs = [i for i, ln in enumerate(lines) if _header_level(ln)]
    drop: set[int] = set()
    for n, i in enumerate(idxs):
        end = idxs[n + 1] if n + 1 < len(idxs) else len(lines)
        if any(ln.strip() for ln in lines[i + 1:end]):
            continue  # section still has content
        level = _header_level(lines[i])
        if level >= 3 or _header_title(lines[i]) == COMPLETED_SECTION_TITLE:
            drop.add(i)
    return _tidy([ln for i, ln in enumerate(lines) if i not in drop])


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
        # Schema-generation comparison: patch releases share a schema,
        # and 0.14.0 keeps the 0.13 layout, so an older-patch project
        # is still "migrated".
        if schema_compatible(found) and not plan_file.is_symlink():
            result.say(
                f"Project is already at v{VERSION} "
                f"(schema v{found}); nothing to migrate.")
            result.applied = True
            return result
        if schema_compatible(found) and plan_file.is_symlink():
            result.say(
                f"Project schema v{found} is current, but {plan_file} is "
                "still a symlink; healing it into a real file.")

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
        if plan_file.is_symlink():
            result.say(
                "  - replace the .ck/PLAN.md symlink with a REAL file "
                f"({plan_file.resolve().name} stays untouched)")
        try:
            raw = plan_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raw = ""
        _normalized, merged_titles = normalize_plan_layout(raw)
        if merged_titles:
            listing = ", ".join(f"## {t.title()}" for t in merged_titles)
            result.say(
                f"  - merge {listing} into one '## Current Sprint' "
                "section")
        result.say("  - strip empty headers left behind in PLAN.md")
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

        plan_file = ck / "PLAN.md"

        # 1b) Materialize a symlinked PLAN.md BEFORE any mutation:
        #     the shared atomic-write helper resolves symlinks (so
        #     dotfile-managed plans keep their links), which would
        #     silently redirect every migration write into the link
        #     target (the root ROADMAP.md). Migration owns the plan
        #     path: link -> real file, target never written to.
        try:
            if dereference_plan_symlink(plan_file):
                result.say(
                    "PLAN.md: symlink replaced with a real file "
                    "(the link target was not modified).")
        except OSError as exc:
            message = f"PLAN.md dereference failed: {exc}"
            result.fail(message)
            log_lines.append(message)
            self._write_log(ck, log_lines)
            return

        # 1c) Standardize the layout: fold the legacy active sections
        #     (Backlog / Active / custom) into one Current Sprint.
        try:
            raw = plan_file.read_text(encoding="utf-8")
            normalized, merged_titles = normalize_plan_layout(raw)
            if normalized != raw:
                _atomic_write_text(plan_file, normalized)
                if merged_titles:
                    listing = ", ".join(
                        f"## {t.title()}" for t in merged_titles)
                    result.say(
                        f"PLAN.md: merged {listing} into "
                        "'## Current Sprint'.")
                else:
                    result.say(
                        "PLAN.md: layout normalized to "
                        "'## Current Sprint'.")
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            message = f"PLAN.md normalization failed: {exc}"
            result.fail(message)
            log_lines.append(message)
            self._write_log(ck, log_lines)
            return

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
        return stored_schema_version(ck)

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
        """Write PLAN.md: no ``[x]`` rows, no empty headers.

        Completed tasks are dropped (their content lives in
        HISTORY.md now); :func:`strip_empty_headers` then removes
        any heading left with an empty section (``### ...`` and the
        emptied ``## Completed``) while the canonical ``## Current
        Sprint`` scaffold survives for ``ck add``.
        """
        if tl is None:
            raise ValueError("Plan AST missing; cannot rewrite PLAN.md")

        done = [t for t in tl.tasks if t.status == TaskStatus.DONE]
        for task in done:
            tl.delete(task.id)
        text = strip_empty_headers(render_plan(tl))
        _atomic_write_text(plan_file, text)

    @staticmethod
    def _write_metadata(root: Path, ck: Path, state: dict) -> None:
        """Create the v0.13 metadata set (missing files only)."""
        def put(path: Path, text: str) -> None:
            if not path.exists():
                path.write_text(text, encoding="utf-8")

        put(ck / JOURNAL_FILENAME, "")
        # The version marker is NOT a "missing file only" write: a
        # project migrating from an earlier v0.13.x keeps its old
        # .ck/version, and put() would silently leave it stale.
        (ck / VERSION_FILENAME).write_text(f"{VERSION}\n", encoding="utf-8")
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
