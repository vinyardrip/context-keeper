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
``git ls-remote`` (read-only). The ``ls-remote`` probe is capped by
a HARD sub-second timeout (:data:`NETWORK_CHECK_TIMEOUT`) so a slow
or unreachable remote can never stall the CLI execution loop: on
timeout the check fails silently (no traceback, no stderr noise).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

# Hard sub-second cap for network-bound update-check probes. A
# background notifier must never block the CLI: if the remote cannot
# answer within this window, the check silently fails.
NETWORK_CHECK_TIMEOUT = 0.8


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


def repo_root(path: Path | None = None) -> Optional[Path]:
    """Absolute top-level directory of the Git work tree containing
    ``path`` (default: cwd), or None when Git is unavailable / ``path``
    is outside any work tree.

    Read-only: a single ``git rev-parse --show-toplevel`` probe. Used
    to BOUND upward project-context traversal (``ck st`` must not
    escape the repository boundary when looking for a PLAN.md).
    """
    if not _resolve_git():
        return None
    cwd = str(path) if path else None
    result = _run(["rev-parse", "--show-toplevel"], cwd=cwd, timeout=5.0)
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    if not top or top.startswith("-"):
        return None
    try:
        return Path(top).resolve()
    except (OSError, ValueError):
        return None


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
    if name.startswith("-"):
        # Option-injection guard.
        return False
    cwd = str(path) if path else None
    result = _run(["remote", "get-url", "--", name], cwd=cwd, timeout=5.0)
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
    if remote.startswith("-") or branch.startswith("-"):
        # Option-injection guard: a refspec like "--exec=..." would be
        # parsed by git as an option, not a revision.
        return None
    cwd = str(path) if path else None
    result = _run(["rev-parse", f"refs/remotes/{remote}/{branch}"],
                  cwd=cwd, timeout=5.0)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def ls_remote(remote: str, branch: str, path: Path | None = None,
              *, timeout: float = NETWORK_CHECK_TIMEOUT) -> Optional[str]:
    """``git ls-remote <remote> <branch>`` with a HARD sub-second timeout.

    Read-only network probe used by the non-blocking update notifier.
    Returns the remote tip SHA, or ``None`` on ANY failure (git
    missing, invalid refspec, timeout, network error). Never raises,
    never prints: a slow or unreachable remote must fail silently
    instead of stalling the CLI or polluting stderr.
    """
    git = _resolve_git()
    if not git:
        return None
    if not remote or not branch:
        return None
    if remote.startswith("-") or branch.startswith("-"):
        # Option-injection guard (mirrors _assert_safe_remote_write):
        # a "remote" such as ``--upload-pack=...`` would be parsed by
        # git as an option, not a refspec.
        return None
    cwd = str(path) if path else None
    try:
        result = subprocess.run(
            [git, "ls-remote", remote, branch],
            capture_output=True, text=True, cwd=cwd,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        # Hard deadline hit: fail silently, no traceback, no stderr.
        return None
    except OSError:
        return None
    if result.returncode != 0:
        return None
    lines = (result.stdout or "").strip().splitlines()
    if not lines:
        return None
    parts = lines[0].split()
    if not parts:
        return None
    return parts[0].strip() or None


def is_dirty(path: Path | None = None) -> bool:
    """Return True if the work tree has uncommitted or untracked changes.

    Uses ``git status --porcelain`` so we ignore colour codes.

    FAIL-CLOSED: any error state (git missing, command failure,
    timeout, non-repo) returns True ("assume dirty") so callers such
    as the ``ck update`` flow never fast-forward over a work tree
    whose state could not be verified. Use :func:`is_git_repo` first
    to distinguish "not a repo" from "repo status unknown".
    """
    if not _resolve_git():
        # Cannot verify → assume dirty.
        return True
    cwd = str(path) if path else None
    result = _run(["status", "--porcelain"], cwd=cwd, timeout=5.0)
    if result.returncode != 0:
        # git failed (timeout, lock contention, corrupt index,
        # dubious ownership, …) — cannot prove the tree is clean.
        return True
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


def local_commit(message: str, path: Path | None = None,
                 stage: Optional[list[str]] = None) -> bool:
    """Commit locally with ``message``, staging only explicit paths.

    ``stage`` is a list of *repo-relative* paths (e.g. ``[".ck"]``)
    that are added before committing. When ``stage`` is empty or
    None, nothing is staged — only already-staged changes are
    committed. This deliberately avoids ``git add .`` which would
    sweep unrelated untracked files (potentially secrets) into the
    commit.

    Returns True if a commit was created. NEVER pushes.
    """
    if not _resolve_git():
        return False
    cwd = str(path) if path else None
    if stage:
        add = _run(["add", "--", *stage], cwd=cwd, timeout=15.0)
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

    Rejects leading ``-`` (option injection: a "remote" such as
    ``--upload-pack=...`` would be parsed by git as an option, not a
    refspec) in addition to shell metacharacters.
    """
    if not remote or not branch:
        raise ValueError("remote and branch must be non-empty")
    if remote.startswith("-") or branch.startswith("-"):
        raise ValueError(f"unsafe remote/branch (option-like): {remote!r}/{branch!r}")
    if any(c in remote for c in (";", "&", "|", "`", "$", "\n")):
        raise ValueError(f"unsafe remote name: {remote!r}")
    if any(c in branch for c in (";", "&", "|", "`", "$", "\n", " ")):
        raise ValueError(f"unsafe branch name: {branch!r}")