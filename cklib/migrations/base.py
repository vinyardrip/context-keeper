"""Migration framework: result record and the abstract runner.

A migration is a one-way, versioned upgrade of a project's ``.ck/``
layout. Every run produces a :class:`MigrationResult` so the CLI can
render the same shape of output for any migration (dry-run previews,
applied changes, backup location, errors).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class MigrationResult:
    """Outcome of one migration run.

    - ``dry_run``    — True when nothing was written (preview only);
    - ``applied``    — True when the migration completed without
      errors (also True for the "already migrated" no-op);
    - ``backup_dir`` — the ``.ck_backup_<version>/`` path written
      before applying (None on dry-run);
    - ``messages``   — human-readable action/report lines, in order;
    - ``errors``     — failures that aborted (or degraded) the run;
      the CLI exits non-zero when this list is non-empty.
    """

    dry_run: bool
    applied: bool = False
    backup_dir: Optional[str] = None
    messages: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def say(self, text: str) -> None:
        """Record one human-readable report line."""
        self.messages.append(text)

    def fail(self, text: str) -> None:
        """Record one error line (rendered as ``ERROR:`` by the CLI)."""
        self.errors.append(text)


class Migration:
    """Abstract version-to-version project layout upgrade.

    Subclasses implement :meth:`upgrade`. ``run`` is the only entry
    point callers use: it resolves the project root, dispatches to
    the dry-run preview or the real upgrade, and always returns a
    fully populated :class:`MigrationResult`.
    """

    #: Schema version this migration UPGRADES TO (e.g. ``"0.13.0"``).
    target_version: str = ""

    def run(self, project_root: Path, *,
            dry_run: bool = True) -> MigrationResult:
        """Migrate ``project_root`` (preview when ``dry_run``)."""
        raise NotImplementedError

    def upgrade(self, project_root: Path,
                result: MigrationResult) -> None:
        """Perform the real upgrade. Implement per target version."""
        raise NotImplementedError
