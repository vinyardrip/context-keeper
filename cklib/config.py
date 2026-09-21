"""Configuration constants and path resolution for Context Keeper."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

VERSION = "0.4.0"
CK_DIR_NAME = ".ck"
HISTORY_LIMIT = 5

PLAN_FILENAME = "PLAN.md"
HISTORY_FILENAME = "HISTORY.md"
PROMPT_FILENAME = "prompt.md"
STATE_FILENAME = "state.json"
GITIGNORE_FILENAME = ".gitignore"
README_FILENAME = "README.md"

GLOBAL_CONFIG_DIR = Path.home() / ".config" / "ck"
GLOBAL_REGISTRY_FILE = GLOBAL_CONFIG_DIR / "projects.json"
GLOBAL_STATE_FILE = GLOBAL_CONFIG_DIR / "state.json"
LEGACY_GLOBAL_CONFIG_FILE = Path.home() / ".ckrc"

# Update notifier throttle window.
UPDATE_CHECK_INTERVAL_HOURS = 24

INSTALL_PATH = "/usr/local/bin/ck"
USER_INSTALL_PATH = Path.home() / ".local" / "bin" / "ck"
# Physical package snapshot written by `ck install` (production
# isolation): the installed launcher at USER_INSTALL_PATH is a regular
# file with no adjacent cklib, so it resolves THIS static copy at
# runtime. NOTE: the `ck` launcher duplicates this computation (it
# cannot import cklib before the source root is resolved) — keep the
# two in sync.
SNAPSHOT_INSTALL_DIR = Path.home() / ".local" / "share" / "ck"
DEFAULT_REPO_URL = "https://github.com/vinyardrip/context-keeper.git"
DEFAULT_REPO_BRANCH = "main"
REMOTE_URL = "https://raw.githubusercontent.com/vinyardrip/context-keeper/main/ck"

LOCAL_GITIGNORE_ENTRIES: tuple[str, ...] = (
    ".ck/*.bak",
    ".ck/state.json",
)

TRACKED_GITIGNORE_PROTECTIONS: tuple[str, ...] = (
    ".ck/PLAN.md",
    ".ck/prompt.md",
    ".ck/README.md",
    ".ck/.gitignore",
)

PROJECT_CONFIG_FILENAME = ".ck.json"

# Palette slots read from the project config's optional "colors"
# mapping (see cklib.ui.PALETTE_SLOTS).
COLOR_CONFIG_KEY = "colors"

DEFAULT_PLAN = """# {project_name}

## Current Sprint
- [] Describe the first task

## Completed
"""

DEFAULT_PROMPT = """# Context Keeper: AI System Instructions \U0001f916

You are a Senior Engineer assistant for the **Context Keeper (ck)** CLI utility.
Your goal is to generate or update the `PLAN.md` file based on the user's technical requirements.

## \u26a0\ufe0f STRICT FORMATTING RULES (DO NOT DEVIATE):

1. **Active Task Syntax**: Use strictly `- []` (Dash, Space, Empty Brackets).
   - \u2705 CORRECT: `- [] Task description`
   - \u274c WRONG: `-[] Task` (no space after dash)
   - \u274c WRONG: `- [ ] Task` (space inside brackets)

2. **Focused Task Syntax**: Use strictly `- [>]` to mark the currently active focus.
   - Only one task may be focused at a time.

3. **Completed Task Syntax**: Use strictly `- [x]`.

4. **Flat Structure**: Do NOT use nested lists, tabs, or indentation. Every task must be a top-level list item.

5. **Task Selection Logic**: The `ck` utility identifies the focused task (`[>]`) or the first open task (`[]`) as the "Current Active Task".

6. **Character Limit**: Keep task descriptions concise (under 80 characters).
"""

DEFAULT_CK_GITIGNORE = """# Context Keeper - Auto-generated
# Exclude backups and dynamic state from Git tracking

# History archives (rotated backups)
*.bak

# Dynamic state (regenerated automatically)
state.json

# Transient cross-process lock files
*.lock

# Local config overrides
config.local.json
"""


def _is_sensitive_root(path: Path) -> bool:
    """True if ``path`` is a filesystem boundary where a ``.ck/``
    project is almost certainly a mistake (home dir, /tmp, /, ...)."""
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    if home is not None and path == home:
        return True
    if path == path.parent:  # filesystem root ("/" or drive root)
        return True
    return path in _SENSITIVE_TMP_ROOTS


_SENSITIVE_TMP_ROOTS: tuple[Path, ...] = (
    Path("/tmp"),
    Path("/var/tmp"),
    Path("/usr/tmp"),
    Path(tempfile.gettempdir()),
)


def warn_if_sensitive_root(root: Path) -> bool:
    """Emit a stderr warning when ``root`` is a filesystem boundary.

    Returns True when a warning was emitted (caller may want to
    reflect it in UX). Never raises.
    """
    try:
        root = root.resolve()
        sensitive = _is_sensitive_root(root)
    except (OSError, RuntimeError):
        return False
    if sensitive:
        try:
            print(
                f"Warning: initializing a Context Keeper project "
                f"in {root} is unusual - this affects every command "
                f"run from anywhere beneath it. Consider using a "
                f"dedicated project directory instead.",
                file=sys.stderr,
            )
        except OSError:
            pass
        return True
    return False


def find_project_root(start: Path | None = None) -> Optional[Path]:
    """Walk up from ``start`` (default: cwd) until ``.ck/`` is found.

    If no ``.ck/`` exists, returns ``start`` (or cwd). The caller can
    pass the result through :func:`warn_if_sensitive_root` when a
    NEW project is about to be created there (``ck init``).

    DANGLING WORKING DIRECTORY: when the process cwd no longer
    exists on disk (the directory was wiped/rebuilt underneath the
    caller — e.g. a sandbox re-initialization or an external
    ``rm -rf``), ``Path.cwd()``/``os.getcwd()`` raise
    ``FileNotFoundError``. Instead of crashing, returns ``None`` so
    callers can degrade gracefully: global commands (``ck st -g``,
    ``ck dashboard``, ``ck register``) do not need a local root at
    all, and local commands surface a clean "not in a valid
    project directory" message instead of a traceback.
    """
    try:
        curr = (start or Path.cwd()).resolve()
    except FileNotFoundError:
        # The cwd descriptor itself is dangling; nothing to walk.
        return None
    except (OSError, RuntimeError):
        # Other resolution failures (permission loops, symlink
        # cycles) degrade the same way — a broken environment is
        # treated as "no resolvable root", never a crash.
        return None
    for parent in [curr, *curr.parents]:
        if (parent / CK_DIR_NAME).is_dir():
            return parent
    return curr


def _read_project_editor_config(root: Path | None = None) -> str:
    """Read the ``"editor"`` key from the project config file.

    Looks for ``.ck.json`` in the Context Keeper project root
    (the directory containing ``.ck/``, found by walking up from
    ``root`` or the cwd). Returns ``""`` when the file is missing,
    unreadable, or has no non-empty string ``"editor"`` value. Never
    raises — a malformed project config must not break the CLI.
    """
    try:
        project_root = find_project_root(root)
        config_path = project_root / PROJECT_CONFIG_FILENAME
        if not config_path.is_file():
            return ""
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    editor = data.get("editor")
    if isinstance(editor, str) and editor.strip():
        return editor.strip()
    return ""


def read_color_config(root: Path | None = None) -> dict:
    """Read the optional ``"colors"`` palette overrides from ``.ck.json``.

    Looks for the Context Keeper project root (the directory
    containing ``.ck/``, found by walking up from ``root`` or the
    cwd) and reads its ``.ck.json``::

        {"colors": {"text": "cyan", "muted": "blue",
                    "border": "bold black", "accent": "magenta"}}

    Returns a dict of slot name -> config value for the known slots
    only (``text``, ``muted``, ``border``, ``accent``); non-string
    values are dropped. Value *contents* are validated later by
    :func:`cklib.ui.resolve_color` — anything unrecognised degrades
    gracefully to the terminal's native text color. Never raises:
    a missing/unreadable/malformed config yields ``{}``.
    """
    try:
        project_root = find_project_root(root)
        config_path = project_root / PROJECT_CONFIG_FILENAME
        if not config_path.is_file():
            return {}
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    colors = data.get(COLOR_CONFIG_KEY)
    if not isinstance(colors, dict):
        return {}
    from .ui import PALETTE_SLOTS

    return {
        slot: colors[slot]
        for slot in PALETTE_SLOTS
        if isinstance(colors.get(slot), str)
    }


def get_editor(root: Path | None = None,
               *, env: dict | None = None) -> str:
    """Resolve the user's preferred editor.

    Strict precedence:

    1. Project config — the ``"editor"`` key in ``.ck.json`` at
       the Context Keeper project root (highest priority).
    2. ``$VISUAL`` environment variable.
    3. ``$EDITOR`` environment variable.
    4. System fallback — ``nano`` if installed, else ``vi``.

    ``env`` defaults to ``os.environ`` (injectable for tests).
    Values are stripped; empty/unset variables are skipped.
    """
    # 1. Project config (.ck.json -> "editor")
    project_editor = _read_project_editor_config(root)
    if project_editor:
        return project_editor

    environ = env if env is not None else os.environ

    # 2. $VISUAL, 3. $EDITOR
    for var in ("VISUAL", "EDITOR"):
        value = (environ.get(var) or "").strip()
        if value:
            return value

    # 4. System fallback: nano if available, otherwise vi.
    for fallback in ("nano", "vi"):
        if shutil.which(fallback):
            return fallback
    return "vi"


def parse_version(ver_str: str) -> tuple[int, ...]:
    """Parse '0.1.0' → (0, 1, 0). Falls back to (0, 0, 0)."""
    try:
        return tuple(int(x) for x in ver_str.strip().split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


# ---------------------------------------------------------------------------
# Cross-process / cross-thread file locking
# ---------------------------------------------------------------------------

import contextlib
import os
import time
from typing import Iterator


class LockTimeoutError(RuntimeError):
    """Raised when a file lock cannot be acquired within ``timeout``.

    The lock is *fail-closed*: mutating operations must abort rather
    than proceed without mutual exclusion (a silent unlock would turn
    a read-modify-write into a lost update).
    """


@contextlib.contextmanager
def file_lock(path: Path | str, *, timeout: float = 5.0,
              poll: float = 0.05) -> Iterator[None]:
    """Acquire an advisory exclusive lock on ``path``.

    ``path`` is typically the *target* of an operation (e.g. the
    registry file). The lock is acquired on a sibling ``.lock`` file so
    concurrent processes (or threads) serialise on the same lockfile.

    - Uses POSIX ``fcntl.flock`` when available. The import is guarded
      and performed lazily so platforms without ``fcntl`` (e.g.
      Windows) can still import this module.
    - FAIL-CLOSED: if the lock cannot be acquired within ``timeout``
      (or the lock file cannot be opened at all), raises
      :class:`LockTimeoutError` instead of proceeding unlocked.
    - The lock is always released on exit, even if the body raises.
    - DEV MODE: when the sandbox context is active
      (``ck-dev`` / ``CK_SANDBOX`` / ``--sandbox`` / cwd inside
      ``.sandbox/``), the lock is taken on the SANDBOX sibling of the
      redirected path so ``*.lock`` files are never created inside
      real production directories (``.ck/`` or ``~/.config/ck/``)
      and dev-context writers serialise on the same files they
      actually write. The import is lazy to avoid a config <->
      sandbox import cycle.

    Usage::

        with file_lock(registry_path):
            data = json.loads(registry_path.read_text())
            ...
            registry_path.write_text(json.dumps(data))
    """
    p = Path(path)
    try:
        from .sandbox import dev_context_active, resolve_write_path
        if dev_context_active():
            p = resolve_write_path(p, force=True)
    except Exception:
        pass  # interception is best-effort for locks; never block I/O
    lock_path = p.with_name(p.name + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise LockTimeoutError(
            f"cannot create lock directory for {lock_path}: {e}"
        ) from e

    # Import fcntl lazily and defensively: on non-POSIX platforms
    # ``fcntl`` may not exist. In that case we cannot guarantee
    # cross-process mutual exclusion, so single-process semantics are
    # the best we can offer — the context manager still yields (and
    # still serialises threads via a process-wide mutex).
    try:
        import fcntl as _fcntl
    except ImportError:
        _fcntl = None

    deadline = time.monotonic() + timeout
    fd: int | None = None
    try:
        if _fcntl is not None:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            except OSError as e:
                raise LockTimeoutError(
                    f"cannot open lock file {lock_path}: {e}"
                ) from e

            while True:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() > deadline:
                        raise LockTimeoutError(
                            f"could not acquire lock on {lock_path} "
                            f"within {timeout}s (held by another "
                            "process?)"
                        )
                    time.sleep(poll)
        yield
    finally:
        if fd is not None:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
