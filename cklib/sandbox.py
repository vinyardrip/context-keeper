"""Sandbox manager for dev mode (``ck-dev`` / ``CK_SANDBOX=1``).

Dev mode provides a read-only view of the real Context Keeper state
(``~/.config/context-keeper/projects.json``, real project folders)
while redirecting EVERY write to an isolated tree under
``<repo_root>/.sandbox/``. Production state stays strictly immutable
while the sandbox is active.

Layout::

    <repo_root>/.sandbox/
    ├── projects/            # redirected per-project writes
    │   └── <project_hash>/  # one sandbox dir per real project
    ├── config/              # pseudo global config / registry copies
    └── dev.log              # sandbox debug log (never stdout)

The sandbox root is anchored to the REPOSITORY root (the parent
directory of the ``cklib`` package), not the cwd, so its location is
stable no matter where ``ck`` is invoked from.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path
from typing import Union

SANDBOX_DIR_NAME = ".sandbox"
PROJECTS_SUBDIR = "projects"
CONFIG_SUBDIR = "config"
DEV_LOG_FILENAME = "dev.log"


def sandbox_root() -> Path:
    """Return the sandbox root: ``<repo_root>/.sandbox`` (absolute).

    The repository root is the parent of the ``cklib`` package
    directory. The returned path is LEXICAL (not resolved): if
    ``.sandbox`` is a symlink, callers must be able to see the LINK
    itself — resolving here would make ``clean_sandbox`` follow the
    link and delete the pointed-to tree (guardrail violation).
    Containment checks resolve explicitly where needed
    (:func:`is_within_sandbox`).
    """
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / SANDBOX_DIR_NAME


def project_hash(project_root: Union[Path, str]) -> str:
    """Stable short identifier for a real project path.

    Combines the directory name with a SHA-256 prefix of the
    absolute path, so two projects with the same folder name in
    different locations never collide in the sandbox.
    """
    p = Path(project_root)
    digest = hashlib.sha256(str(p.resolve()).encode("utf-8")).hexdigest()
    return f"{p.name}-{digest[:10]}"


def ensure_sandbox_dir() -> Path:
    """Safely initialize ``.sandbox/`` and its subdirectories.

    Creates ``.sandbox/``, ``.sandbox/projects/`` and
    ``.sandbox/config/`` with ``parents=True, exist_ok=True`` —
    idempotent, race-safe with concurrent creators, and never
    raising on an already-existing tree.
    """
    root = sandbox_root()
    (root / PROJECTS_SUBDIR).mkdir(parents=True, exist_ok=True)
    (root / CONFIG_SUBDIR).mkdir(parents=True, exist_ok=True)
    return root


def sandbox_project_dir(project_root: Union[Path, str]) -> Path:
    """Sandbox target for all writes belonging to ``project_root``.

    ``.sandbox/projects/<name>-<hash>/``. Creates the directory on
    demand (a project that has never been written to has no sandbox
    dir yet).
    """
    root = ensure_sandbox_dir()
    d = root / PROJECTS_SUBDIR / project_hash(project_root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def sandbox_config_dir() -> Path:
    """Sandbox target for global-config writes: ``.sandbox/config/``."""
    return ensure_sandbox_dir() / CONFIG_SUBDIR


def clean_sandbox(*, quiet: bool = False) -> bool:
    """Safely and recursively remove the entire ``.sandbox/`` tree.

    - Missing sandbox: no-op success (idempotent cleanup).
    - ``.sandbox`` exists but is a FILE (or a symlink to a file):
      it is removed with ``unlink`` — never followed as a directory.
    - ``.sandbox`` is a symlink to a directory: only the LINK is
      removed; the pointed-to tree is never touched (guardrail: a
      user-created symlink could aim anywhere, e.g. ``~``).
    - Permission/OS errors: reported to stderr, return False. No
      unhandled exceptions escape.

    Returns True when the sandbox is gone (or never existed).
    """
    root = sandbox_root()
    try:
        if not root.exists() and not root.is_symlink():
            if not quiet:
                _emit("Sandbox already absent — nothing to clean.")
            return True
        if root.is_symlink():
            # Remove the LINK only; never recurse through it.
            root.unlink()
        elif root.is_dir():
            shutil.rmtree(root)
        else:
            root.unlink()
    except OSError as e:
        print(
            f"[ck-clean] Failed to remove sandbox at {root}: {e}",
            file=sys.stderr,
        )
        return False
    if not quiet:
        _emit("Sandbox environment cleared successfully.")
    return True


def _emit(message: str) -> None:
    """Print a single-line confirmation to stdout (ck-clean output)."""
    print(f"[ck-clean] {message}")


def is_within_sandbox(path: Union[Path, str]) -> bool:
    """True when ``path`` lives inside the sandbox (post-interception
    sanity check / test helper)."""
    try:
        p = Path(path).resolve()
        return sandbox_root() in p.parents or p == sandbox_root()
    except OSError:
        return False


__all__ = [
    "SANDBOX_DIR_NAME",
    "PROJECTS_SUBDIR",
    "CONFIG_SUBDIR",
    "DEV_LOG_FILENAME",
    "sandbox_root",
    "project_hash",
    "ensure_sandbox_dir",
    "sandbox_project_dir",
    "sandbox_config_dir",
    "clean_sandbox",
    "is_within_sandbox",
]
