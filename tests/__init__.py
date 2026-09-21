"""unittest test suite for Context Keeper.

Each module follows the standard ``unittest`` discovery pattern
(``test_*.py`` containing ``TestCase`` classes). Run with:

    python -m unittest discover tests

The suite covers (per the security & architecture review):

- AST-based task mutation roundtrips (parse -> mutate -> render).
- Standard CommonMark ``- [ ]`` rendering and legacy ``- []``
  parsing.
- ``.gitignore`` negation appending when blanket ignores are present.
- Pure ``parse_plan`` semantics (no side effects, no normalization).
- Safe decline of Git initialization in ``save``.
- Permanent deletion of legacy ``.ckrc`` after migration.

FILESYSTEM ISOLATION (every runner): importing this package pins the
sandbox anchor (``CK_SANDBOX_ROOT``) to a throwaway directory inside
the OS temp directory, removed at interpreter exit. Every in-process
sandbox operation of ANY test — including runs through
``python -m unittest discover`` which do NOT see the pytest conftest
fixture — therefore lands in the OS temp directory, never in this
repository: the repo's own ``.sandbox/`` (the manual
``ck-dev sandbox setup`` environment) can never be created, read-
modified, or wiped by an automated run. The pytest conftest refines
this to a per-test anchor; tests may override the variable freely
(saved/restored values chain correctly).
"""

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SANDBOX_ROOT_ENV = "CK_SANDBOX_ROOT"

if _SANDBOX_ROOT_ENV not in os.environ:
    # Process-wide safety net for runners without pytest fixtures.
    # An outer harness that already pinned the anchor keeps control.
    _ISO_BASE = tempfile.mkdtemp(prefix="ck-test-sandbox-")
    os.environ[_SANDBOX_ROOT_ENV] = str(Path(_ISO_BASE) / ".sandbox")
    atexit.register(shutil.rmtree, _ISO_BASE, ignore_errors=True)


__all__ = []