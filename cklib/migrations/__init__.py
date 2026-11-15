"""Schema migrations for legacy layouts.

Each migration upgrades ONE storage generation to the next. The
package currently carries:

- :class:`MigrateV012ToV013` — pre-0.13 project ``.ck/``
  ``state.json`` monoliths to the v0.13.0 split layout (PLAN.md
  active-only, HISTORY.md ledger, inline notes, UUIDs,
  meta/version/links files);
- :class:`MigrateSpacesV012ToV013` — global spaces
  (``~/.config/ck/spaces/``) to the same v0.13 shape: completed rows
  into ``<space>.HISTORY.md``, sidecar notes inline, UUIDs, sidecar
  JSON retired (backed up to ``spaces_backup_<tag>/``).

Single projects run through ``ck migrate`` (dry-run preview by
default, ``--apply`` to write — always after a
``.ck_backup_<version>/`` backup). The batch helpers
:func:`migrate_spaces` and :func:`migrate_all_projects` power
``ck migrate --spaces`` / ``--all``: every registered project is
migrated in one pass, one failure never aborts the batch.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..config import CK_DIR_NAME, VERSION
from .base import Migration, MigrationResult
from .spaces_v0_12_to_v0_13 import (
    SPACES_BACKUP_TAG,
    MigrateSpacesV012ToV013,
    SpacesMigrationResult,
    SpacePlan,
)
from .v0_12_to_v0_13 import (
    MigrateV012ToV013,
    stored_schema_version,
)

__all__ = [
    "Migration",
    "MigrationResult",
    "MigrateV012ToV013",
    "MigrateSpacesV012ToV013",
    "SpacesMigrationResult",
    "SpacePlan",
    "SPACES_BACKUP_TAG",
    "ProjectBatchEntry",
    "ProjectMigrationBatch",
    "migrate_spaces",
    "migrate_all_projects",
]


def migrate_spaces(*, dry_run: bool = True) -> SpacesMigrationResult:
    """Migrate every global space (``ck migrate --spaces``)."""
    return MigrateSpacesV012ToV013().run(dry_run=dry_run)


@dataclass
class ProjectBatchEntry:
    """One registered project's batch-migration outcome."""

    name: str
    path: str
    #: ``migrated`` | ``already`` | ``skipped`` | ``error`` | ``planned``
    status: str
    result: Optional[MigrationResult] = None


@dataclass
class ProjectMigrationBatch:
    """Outcome of one ``ck migrate --all`` project pass.

    ``messages`` carries the per-project summary lines (``[ok]`` /
    ``[!]`` / ``[i]``) plus each migration's own report lines;
    ``errors`` accumulates every failure so the CLI can exit non-zero
    without aborting the remaining projects.
    """

    dry_run: bool
    applied: bool = False
    messages: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    entries: List[ProjectBatchEntry] = field(default_factory=list)

    def say(self, text: str) -> None:
        self.messages.append(text)

    def fail(self, text: str) -> None:
        self.errors.append(text)


def migrate_all_projects(*, dry_run: bool = True) -> ProjectMigrationBatch:
    """Migrate EVERY registered project (``ck migrate --all``).

    The global registry (``~/.config/ck/projects.json``) is walked in
    its stored order. Projects already at the current schema are
    reported and skipped, missing paths are warned about, and a
    failure on one project is recorded and the walk CONTINUES —
    a broken project must never stop the batch.
    """
    batch = ProjectMigrationBatch(dry_run=dry_run)

    from .. import registry as ckregistry

    try:
        projects = ckregistry.list_projects()
    except Exception as exc:  # a broken registry must not crash the CLI
        batch.fail(f"Cannot read the project registry: {exc}")
        return batch

    if not projects:
        batch.say("No registered projects; nothing to migrate.")
        batch.applied = True
        return batch

    batch.say(f"Registered projects: {len(projects)}")
    for entry in projects:
        root = Path(entry.path)
        ck = root / CK_DIR_NAME
        if not root.is_dir():
            batch.entries.append(ProjectBatchEntry(
                name=entry.name, path=entry.path, status="skipped"))
            batch.say(
                f"[!] Project {entry.name}: path missing, skipped "
                f"({entry.path})")
            continue
        if not ck.is_dir():
            batch.entries.append(ProjectBatchEntry(
                name=entry.name, path=entry.path, status="skipped"))
            batch.say(
                f"[!] Project {entry.name}: no .ck/ project, skipped "
                f"({root})")
            continue

        # Already migrated (and not a dev symlink that still needs
        # healing)? The migration run would report the same no-op, so
        # the batch answers directly.
        plan_file = ck / "PLAN.md"
        stored = stored_schema_version(ck)
        if stored == VERSION and not plan_file.is_symlink():
            batch.entries.append(ProjectBatchEntry(
                name=entry.name, path=entry.path, status="already"))
            batch.say(f"[!] Project {entry.name}: already at v{VERSION}")
            continue

        migration = MigrateV012ToV013()
        try:
            result = migration.run(root, dry_run=dry_run)
        except Exception as exc:  # one broken project, batch goes on
            batch.entries.append(ProjectBatchEntry(
                name=entry.name, path=entry.path, status="error"))
            batch.fail(f"[!] Project {entry.name}: migration failed: {exc}")
            continue

        if result.errors:
            batch.entries.append(ProjectBatchEntry(
                name=entry.name, path=entry.path, status="error",
                result=result))
            for line in result.errors:
                batch.fail(f"[!] Project {entry.name}: {line}")
            for line in result.messages:
                batch.say(f"    {line}")
            continue

        batch.entries.append(ProjectBatchEntry(
            name=entry.name, path=entry.path,
            status="planned" if dry_run else "migrated",
            result=result))
        if dry_run:
            batch.say(f"[i] Project {entry.name}: legacy layout, "
                      "migration planned")
        else:
            batch.say(f"[ok] Project {entry.name}: migrated")
        for line in result.messages:
            batch.say(f"    {line}")

    if not batch.errors:
        batch.applied = True
    return batch
