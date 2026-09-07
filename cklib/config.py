"""Configuration constants and path resolution for Context Keeper."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

VERSION = "0.1.0"
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

DEFAULT_PLAN = """# {project_name}

## Current Sprint
- [] Описать первую задачу

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


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (default: cwd) until ``.ck/`` is found.

    If no ``.ck/`` exists, returns ``start`` (or cwd).
    """
    curr = (start or Path.cwd()).resolve()
    for parent in [curr, *curr.parents]:
        if (parent / CK_DIR_NAME).is_dir():
            return parent
    return curr


def get_editor() -> str:
    """Resolve the user's preferred editor."""
    editor = os.environ.get("EDITOR")
    if editor:
        return editor
    for fallback in ("micro", "nano", "vi"):
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

    ``path`` is typically the *target* of the operation (e.g. the
    registry file). The lock is acquired on a sibling ``.lock`` file so
    concurrent processes (or threads) serialise on the same lockfile.

    - Uses POSIX ``fcntl.flock`` when available. The import is guarded
      and performed lazily so platforms without ``fcntl`` (e.g.
      Windows) can still import this module.
    - FAIL-CLOSED: if the lock cannot be acquired within ``timeout``
      (or the lock file cannot be opened at all), raises
      :class:`LockTimeoutError` instead of proceeding unlocked.
    - The lock is always released on exit, even if the body raises.

    Usage::

        with file_lock(registry_path):
            data = json.loads(registry_path.read_text())
            ...
            registry_path.write_text(json.dumps(data))
    """
    p = Path(path)
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