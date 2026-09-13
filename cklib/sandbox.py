"""Sandbox manager for dev mode (``ck-dev`` / ``CK_SANDBOX=1``).

Dev mode provides a read-only view of the real Context Keeper state
(``~/.config/ck/projects.json``, real project folders) while
redirecting EVERY write to an isolated tree under
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

Dev-mode triggers (any one activates interception):

- the ``ck-dev`` entrypoint was invoked
- ``CK_SANDBOX=1`` (or another truthy value) in the environment
- ``CK_DEV=1`` in the environment
- the ``--sandbox`` flag on the command line
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Union

from .config import CK_DIR_NAME as CK_DIR, PROJECT_CONFIG_FILENAME

SANDBOX_DIR_NAME = ".sandbox"
PROJECTS_SUBDIR = "projects"
CONFIG_SUBDIR = "config"
DEV_LOG_FILENAME = "dev.log"

# Environment values treated as "on" for the CK_* toggles.
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})

# Names of the dev-mode entrypoint (with/without executable suffix).
_DEV_ENTRYPOINT_NAMES = frozenset({"ck-dev", "ck-dev.exe"})


class SandboxViolationError(RuntimeError):
    """Raised when dev mode would write to real production state.

    A *guardrail* exception: writing to the global registry
    (``~/.config/ck/``), a real project's ``.ck/`` tree, or its
    ``.ck.json`` while ``IS_DEV`` is active is a bug, not a
    user error — fail loudly instead of corrupting production data.
    """


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


# --------------------------------------------------------------------------- #
# Dev-mode detection
# --------------------------------------------------------------------------- #


def _env_flag_on(env: dict, name: str) -> bool:
    """True when ``env[name]`` holds a truthy toggle value."""
    return str(env.get(name, "")).strip().lower() in _TRUTHY_ENV


def is_dev_mode(argv: Optional[list] = None,
                env: Optional[dict] = None) -> bool:
    """True when dev mode (write interception) should be active.

    Triggers (any one):

    1. The ``ck-dev`` entrypoint: ``argv[0]`` basename is ``ck-dev``
       (defaults to ``sys.argv``).
    2. ``CK_SANDBOX=1`` environment toggle.
    3. ``CK_DEV=1`` environment toggle.
    4. The ``--sandbox`` CLI flag present anywhere in ``argv``.

    ``argv``/``env`` are injectable for tests; both default to the
    process state. Pure function — no side effects.
    """
    if argv is None:
        argv = sys.argv
    if env is None:
        env = os.environ

    if argv:
        entry = Path(argv[0]).name
        if entry.lower() in _DEV_ENTRYPOINT_NAMES:
            return True
        if "--sandbox" in argv[1:]:
            return True

    return _env_flag_on(env, "CK_SANDBOX") or _env_flag_on(env, "CK_DEV")


# --------------------------------------------------------------------------- #
# Write-path interception
# --------------------------------------------------------------------------- #


def _global_config_root() -> Path:
    """The real global config dir this install actually uses.

    ``cklib.config.GLOBAL_CONFIG_DIR`` is consulted dynamically (it
    is monkeypatched by other test suites), falling back to
    ``~/.config/ck``.
    """
    try:
        from . import config as _cfg
        return Path(_cfg.GLOBAL_CONFIG_DIR)
    except Exception:
        return Path.home() / ".config" / "ck"


def _intercept_global_config_path(target: Path) -> Optional[Path]:
    """Redirect a path inside the global config dir into the sandbox.

    Returns the sandboxed path, or None when ``target`` is not under
    the global config root.
    """
    root = _global_config_root()
    try:
        target_r = target.resolve()
        root_r = root.resolve()
    except OSError:
        return None
    if target_r == root_r:
        # Whole-dir target: redirect to the sandbox config dir root.
        return sandbox_config_dir()
    if root_r in target_r.parents:
        rel = target_r.relative_to(root_r)
        d = sandbox_config_dir()
        result = d / rel
        result.parent.mkdir(parents=True, exist_ok=True)
        return result
    return None


def _intercept_project_path(target: Path) -> Optional[Path]:
    """Redirect a real project write (``.ck/`` tree, ``.ck.json``).

    Returns the sandboxed path, or None when ``target`` is not a
    production project write.
    """
    try:
        t = target.resolve()
    except OSError:
        # Absolute-ize lexically; never fail interception on I/O.
        t = Path(os.path.abspath(str(target)))
    parts = t.parts
    if not parts or parts[0] != os.sep:
        return None  # unreachable after abspath; defensive only

    # A real project write touches either the ``.ck`` directory (or
    # something beneath it) or the project-level ``.ck.json`` file.
    if CK_DIR in parts:
        idx = parts.index(CK_DIR)
        project_root = Path(*parts[:idx])
        result = sandbox_project_dir(project_root) / Path(*parts[idx:])
    elif parts[-1] == PROJECT_CONFIG_FILENAME:
        project_root = Path(*parts[:-1])
        result = sandbox_project_dir(project_root) / PROJECT_CONFIG_FILENAME
    else:
        return None

    try:
        result.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return result


def resolve_write_path(target_path: Union[Path, str]) -> Path:
    """Resolve the destination for a write to ``target_path``.

    - Dev mode OFF: the original path is returned unchanged (writes
      go to production as normal; reads always use the original).
    - Dev mode ON: production writes are redirected into the
      sandbox — global-config paths to ``.sandbox/config/`` and
      real-project paths (``.ck/`` trees, ``.ck.json``) to
      ``.sandbox/projects/<hash>/`` — with parents auto-created.

    Non-production targets (e.g. a user's arbitrary scratch file)
    pass through unchanged even in dev mode: the sandbox exists to
    protect *Context Keeper production state*, not to kidnap all
    filesystem writes.
    """
    target = Path(target_path)

    if not is_dev_mode():
        return target

    # Already inside the sandbox: leave as-is (idempotent mapping).
    if is_within_sandbox(target):
        return target

    intercepted = _intercept_global_config_path(target)
    if intercepted is None:
        intercepted = _intercept_project_path(target)
    if intercepted is not None:
        log_sandbox_debug(
            f"Intercepted write -> {intercepted} (real path untouched: {target})"
        )
        return intercepted

    # Not Context-Keeper production state: pass through, but note it
    # in the debug log so unexpected writes are traceable.
    log_sandbox_debug(f"Pass-through write (not ck state): {target}")
    return target


def assert_no_real_write(target_path: Union[Path, str]) -> None:
    """Guardrail: raise ``SandboxViolationError`` when ``target_path``
    is a real production write attempted under dev mode.

    Call this before any write in dev mode; :func:`resolve_write_path`
    already redirects, so a real production path reaching this check
    means interception failed and we must fail loudly.
    """
    if not is_dev_mode():
        return
    target = Path(target_path)
    if is_within_sandbox(target):
        return
    if _intercept_global_config_path(target) is not None:
        raise SandboxViolationError(
            f"Dev-mode guardrail: refusing to write global-config "
            f"production path {target}"
        )
    if _intercept_project_path(target) is not None:
        raise SandboxViolationError(
            f"Dev-mode guardrail: refusing to write real project "
            f"path {target} (PLAN.md/.ck tree)"
        )


# --------------------------------------------------------------------------- #
# Debug logging (stderr / .sandbox/dev.log — never stdout)
# --------------------------------------------------------------------------- #


def debug_enabled(env: Optional[dict] = None) -> bool:
    """True when sandbox debug logging is on (``CK_DEBUG=1``, or
    ``-v``/``--verbose`` in argv)."""
    if env is None:
        env = os.environ
    if _env_flag_on(env, "CK_DEBUG"):
        return True
    argv = sys.argv
    return any(a in ("-v", "--verbose") for a in argv[1:])


def log_sandbox_debug(msg: str, *, env: Optional[dict] = None) -> None:
    """Emit a sandbox debug line — NEVER to stdout.

    With debug enabled (``CK_DEBUG=1`` or ``-v``/``--verbose``):
    writes ``[DEBUG] <msg>`` to stderr and appends to
    ``.sandbox/dev.log``. Otherwise a silent no-op. All I/O errors
    are swallowed: logging must never break the command it traces.
    """
    try:
        if not debug_enabled(env):
            return
        line = f"[DEBUG] {msg}"
        print(line, file=sys.stderr)
        log_path = sandbox_root() / DEV_LOG_FILENAME
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    except Exception:
        pass


__all__ = [
    "SANDBOX_DIR_NAME",
    "PROJECTS_SUBDIR",
    "CONFIG_SUBDIR",
    "DEV_LOG_FILENAME",
    "SandboxViolationError",
    "sandbox_root",
    "project_hash",
    "ensure_sandbox_dir",
    "sandbox_project_dir",
    "sandbox_config_dir",
    "clean_sandbox",
    "is_within_sandbox",
    "is_dev_mode",
    "resolve_write_path",
    "assert_no_real_write",
    "debug_enabled",
    "log_sandbox_debug",
]
