"""Throwaway checkout copies for subprocess (``ck-dev``) tests.

The ``ck-dev`` wrapper resolves its sandbox anchor from the GIT work
tree that contains the script (``git rev-parse --show-toplevel``) and
forces ``CK_SANDBOX_ROOT=$REPO_ROOT/.sandbox`` in the child — by
design, so a manual dev session always anchors at the real checkout.
Running the real wrapper from a test would therefore create and wipe
the repository's own ``.sandbox/`` (the developer's MANUAL
environment, see ``ck-dev sandbox setup``).

:func:`make_checkout_copy` solves this by materializing a minimal,
verbatim copy of the checkout (``cklib/`` + the entry scripts) inside
a caller-provided OS-temp directory and git-initializing it, so every
subsequent ``ck-dev`` invocation against the copy anchors its
sandbox INSIDE that temp directory. The automated suite never runs
the real repo's wrapper against the repo's ``.sandbox/``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Minimal set that makes the copy a functional context-keeper
# checkout: the package plus every entry script (ck-dev needs cklib;
# ck/ck-clean are included so install/clean flows are testable too).
_COPY_ITEMS = ("cklib", "ck", "ck-dev", "ck-clean")


def make_checkout_copy(dest: Path) -> Path:
    """Materialize an isolated context-keeper checkout at ``dest``.

    - copies ``cklib/`` (without bytecode caches) and the entry
      scripts verbatim;
    - ``git init`` the directory so the ``ck-dev`` wrapper's
      ``git rev-parse --show-toplevel`` anchor resolves to ``dest``
      (its sandbox then lives at ``dest/.sandbox``, entirely inside
      the OS temp directory the caller chose).

    Raises RuntimeError when git is unavailable or init fails (the
    wrapper cannot function without a git root); callers should skip
    the test in that case.
    """
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        REPO_ROOT / "cklib", dest / "cklib",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        dirs_exist_ok=True,
    )
    for name in _COPY_ITEMS[1:]:
        shutil.copy2(REPO_ROOT / name, dest / name)
    try:
        result = subprocess.run(
            ["git", "init", "-q", str(dest)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"git init failed in {dest}: {e}") from e
    if result.returncode != 0:
        raise RuntimeError(f"git init failed in {dest}: {result.stderr}")
    return dest
