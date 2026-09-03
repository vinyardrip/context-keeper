"""Local-only Git helpers.

This module deliberately **never** runs ``git push``, ``git clone``,
or any other command that would write to a remote without explicit
opt-in. The update flow is the one exception: ``ck update`` is the
only sanctioned caller of ``git fetch`` and ``git pull --ff-only``,
and it is gated by:

- the directory being a real Git work tree;
- a clean ``git status --porcelain``;
- the user actually invoking ``ck update``.

Routine non-blocking update notifier uses ``git rev-parse`` and
``git ls-remote`` (read-only).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

# Tokens that MUST NEVER appear in a Git command we run. The
# "update" path uses ``fetch`` and ``pull --ff-only`` explicitly,
# so those are not in this set. ``push`` is always forbidden.
_FORBIDDEN_TOKENS = frozenset({"push", "clone"})


def _resolve_git() -> Optional[str]:
    return shutil.which("git")


def _run(cmd: list[str], *, cwd: Optional[str] = None,
         timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Run a Git command. Returns the CompletedProcess; never raises."""
    git = _resolve_git() or "git"
    try:
        return subprocess.run(
            [git, *cmd],
            capture_output=True, text=True, cwd=cwd, timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(
            args=[git, *cmd], returncode=1, stdout="", stderr="",
        )


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def is_git_repo(path: Path | None = None) -> bool:
    """Return True if ``path`` (default cwd) is inside a Git work tree."""
    if not _resolve_git():
        return False
    cwd = str(path) if path else None
    result = _run(["rev-parse", "--is-inside-work-tree"], cwd=cwd, timeout=5.0)
    return result.returncode == 0 and result.stdout.strip() == "true"


def current_branch(path: Path | None = None) -> Optional[str]:
    """Return the current branch name, or None if not in a Git repo."""
    if not _resolve_git():
        return None
    cwd = str(path) if path else None
    result = _run(["branch", "--show-current"], cwd=cwd, timeout=5.0)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def has_remote(path: Path | None = None, name: str = "origin") -> bool:
    """Return True if the named remote is configured."""
    if not _resolve_git():
        return False
    cwd = str(path) if path else None
    result = _run(["remote", "get-url", name], cwd=cwd, timeout=5.0)
    return result.returncode == 0 and bool(result.stdout.strip())


def head_sha(path: Path | None = None) -> Optional[str]:
    """Return the current HEAD SHA, or None if unavailable."""
    if not _resolve_git():
        return None
    cwd = str(path) if path else None
    result = _run(["rev-parse", "HEAD"], cwd=cwd, timeout=5.0)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def remote_sha(path: Path | None, remote: str, branch: str) -> Optional[str]:
    """Return the SHA of ``remote/branch`` from local refs."""
    if not _resolve_git():
        return None
    cwd = str(path) if path else None
    result = _run(["rev-parse", f"{remote}/{branch}"], cwd=cwd, timeout=5.0)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_dirty(path: Path | None = None) -> bool:
    """Return True if the work tree has uncommitted or untracked changes.

    Uses ``git status --porcelain`` so we ignore colour codes.
    Returns False for non-Git directories (caller should check
    ``is_git_repo`` first).
    """
    if not _resolve_git():
        return False
    cwd = str(path) if path else None
    result = _run(["status", "--porcelain"], cwd=cwd, timeout=5.0)
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


# ---------------------------------------------------------------------------
# Repo mutation (local only)
# ---------------------------------------------------------------------------


def init_repo(path: Path | None = None) -> bool:
    """Run ``git init`` in ``path`` (default cwd). Returns success."""
    if not _resolve_git():
        return False
    cwd = str(path) if path else None
    result = _run(["init"], cwd=cwd, timeout=15.0)
    return result.returncode == 0


def local_commit(message: str, path: Path | None = None) -> bool:
    """Stage everything and commit locally with ``message``.

    Returns True if a commit was created. NEVER pushes.
    """
    if not _resolve_git():
        return False
    cwd = str(path) if path else None
    add = _run(["add", "."], cwd=cwd, timeout=15.0)
    if add.returncode != 0:
        return False
    result = _run(["commit", "-m", message], cwd=cwd, timeout=15.0)
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Update flow (the one place where fetch / pull are allowed)
# ---------------------------------------------------------------------------


def fetch(remote: str, branch: str,
          path: Path | None = None,
          *, timeout: float = 30.0) -> bool:
    """``git fetch <remote> <branch>``. Network read only."""
    _assert_safe_remote_write(remote, branch)
    cwd = str(path) if path else None
    result = _run(["fetch", remote, branch], cwd=cwd, timeout=timeout)
    return result.returncode == 0


def pull_ff_only(remote: str, branch: str,
                 path: Path | None = None,
                 *, timeout: float = 30.0) -> Tuple[bool, str]:
    """``git pull --ff-only <remote> <branch>``.

    Returns ``(ok, message)`` where ``message`` is the combined
    stdout/stderr trimmed output. NEVER pushes.
    """
    _assert_safe_remote_write(remote, branch)
    cwd = str(path) if path else None
    result = _run(
        ["pull", "--ff-only", remote, branch], cwd=cwd, timeout=timeout,
    )
    msg = (result.stdout + result.stderr).strip()
    return result.returncode == 0, msg


def _assert_safe_remote_write(remote: str, branch: str) -> None:
    """Hard guard. ``git fetch`` and ``git pull --ff-only`` are the
    only network-touching commands this module is allowed to run.
    """
    if not remote or not branch:
        raise ValueError("remote and branch must be non-empty")
    if any(c in remote for c in (";", "&", "|", "`", "$", "\n")):
        raise ValueError(f"unsafe remote name: {remote!r}")
    if any(c in branch for c in (";", "&", "|", "`", "$", "\n", " ")):
        raise ValueError(f"unsafe branch name: {branch!r}")


# ---------------------------------------------------------------------------
# Defensive token check (kept for compatibility)
# ---------------------------------------------------------------------------


def assert_no_remote_writes(cmd: list[str]) -> None:
    """Hard guard. Raises ``RuntimeError`` if a forbidden token is in ``cmd``."""
    for token in _FORBIDDEN_TOKENS:
        if token in cmd:
            raise RuntimeError(
                f"Refusing to execute network-writing git command: {cmd!r}"
            )