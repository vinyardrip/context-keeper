#!/usr/bin/env python3
"""Context Keeper CLI entry point.

Thin wrapper that delegates to the modular package ``cklib/``.
Run as:

    ./ck <command>          # from the checkout (development)
    python ck <command>
    ck <command>            # after `ck install` — a PHYSICAL copy in
                            # ~/.local/bin/ck backed by the static package
                            # snapshot in ~/.local/share/ck (never a symlink)

WORKSPACE SOURCE RESOLUTION: the ``cklib`` package that ships NEXT TO
THIS LAUNCHER is the one that executes — never a stale copy from
site-packages or an unrelated workspace. The anchor is ``resolve()``d
so a launcher reached through a symlink still pins the real checkout,
and the directory is forced to POSITION 0 of ``sys.path`` (a later
entry would let an earlier ``cklib`` — via ``PYTHONPATH`` or
site-packages — win the import race) BEFORE any ``cklib`` import
happens.

INSTALLED SNAPSHOT FALLBACK: ``ck install`` copies THIS launcher to
``~/.local/bin/ck`` — a directory with no adjacent ``cklib`` — and
snapshots the package to ``~/.local/share/ck/cklib``. When no adjacent
package exists, that snapshot is pinned to position 0 so the installed
binary always executes its own static copy: edits to the checkout (and
even a ``PYTHONPATH`` pointing at it) cannot alter production until
``ck install`` or ``ck update`` explicitly re-runs. Keep the snapshot
computation in sync with ``cklib.config.SNAPSHOT_INSTALL_DIR``.

Active-workspace overrides:

- ``CK_PROJECT_ROOT`` — when set to a directory containing a ``cklib/``
  package, THAT workspace's sources take priority (development against
  a different checkout); invalid or missing values are ignored.
- ``CK_SANDBOX`` (dev session) — the active development workspace IS
  this checkout; the unconditional position-0 insert above already
  guarantees launcher-adjacent sources win.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_repo_root_str = str(_REPO_ROOT)
if _repo_root_str in sys.path:
    # Already importable? Still force it to POSITION 0: a later entry
    # lets an earlier cklib (PYTHONPATH, site-packages) shadow it.
    sys.path.remove(_repo_root_str)
sys.path.insert(0, _repo_root_str)

# Installed-snapshot fallback (see module docstring): only when this
# launcher has no adjacent cklib (i.e. it is the physical copy in
# ~/.local/bin) does the static package snapshot outrank PYTHONPATH /
# site-packages.
if not (_REPO_ROOT / "cklib" / "__init__.py").is_file():
    try:
        _snapshot_dir = Path.home() / ".local" / "share" / "ck"
    except (OSError, RuntimeError):
        _snapshot_dir = None
    if (_snapshot_dir is not None
            and (_snapshot_dir / "cklib" / "__init__.py").is_file()):
        _snapshot_str = str(_snapshot_dir)
        if _snapshot_str in sys.path:
            sys.path.remove(_snapshot_str)
        sys.path.insert(0, _snapshot_str)

# Active-workspace source override (see module docstring).
_workspace = os.environ.get("CK_PROJECT_ROOT", "").strip()
if _workspace:
    try:
        _ws_root = Path(_workspace).resolve()
    except (OSError, RuntimeError):
        _ws_root = None
    if _ws_root is not None and (_ws_root / "cklib").is_dir():
        _ws_str = str(_ws_root)
        if _ws_str in sys.path:
            sys.path.remove(_ws_str)
        sys.path.insert(0, _ws_str)

from cklib.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
