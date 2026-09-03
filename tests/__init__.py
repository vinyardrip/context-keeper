"""unittest test suite for Context Keeper.

Each module follows the standard ``unittest`` discovery pattern
(``test_*.py`` containing ``TestCase`` classes). Run with:

    python -m unittest discover tests

The suite covers (per the security & architecture review):

- AST-based task mutation roundtrips (parse → mutate → render).
- Standard CommonMark ``- [ ]`` rendering and legacy ``- []``
  parsing.
- ``.gitignore`` negation appending when blanket ignores are present.
- Pure ``parse_plan`` semantics (no side effects, no normalization).
- Safe decline of Git initialization in ``save``.
- Permanent deletion of legacy ``.ckrc`` after migration.
"""

import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


__all__ = []