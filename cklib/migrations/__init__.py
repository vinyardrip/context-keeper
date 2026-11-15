"""Schema migrations for legacy ``.ck/`` project layouts.

Each migration upgrades ONE schema generation to the next. The
package currently carries:

- :class:`MigrateV012ToV013` — pre-0.13 ``state.json`` monoliths to
  the v0.13.0 split layout (PLAN.md active-only, HISTORY.md ledger,
  inline notes, UUIDs, meta/version/links files).

Run through ``ck migrate`` (dry-run preview by default, ``--apply``
to write — always after a ``.ck_backup_<version>/`` backup).
"""

from .base import Migration, MigrationResult
from .v0_12_to_v0_13 import MigrateV012ToV013

__all__ = ["Migration", "MigrationResult", "MigrateV012ToV013"]
