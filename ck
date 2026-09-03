#!/usr/bin/env python3
"""Context Keeper CLI entry point.

Thin wrapper that delegates to the modular package ``cklib/``.
Run as:

    ./ck <command>
    python ck <command>
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from cklib.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())