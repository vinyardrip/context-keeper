"""Core orchestrator: ties together config, models, parser, registry, and git.

All PLAN.md mutations are AST-driven: read → mutate the in-memory
:class:`TaskList` → call :func:`render_plan` → atomic write. No
string-based mutations, no regex rewrites.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import git as gith
from . import registry
from . import ui
from .sandbox import (
    PROJECTS_SUBDIR,
    is_dev_mode,
    log_sandbox_debug,
    project_hash,
    resolve_write_path,
    sandbox_project_dir,
    sandbox_root,
)
from .config import (
    CK_DIR_NAME,
    DEFAULT_CK_GITIGNORE,
    DEFAULT_PLAN,
    DEFAULT_PROMPT,
    DEFAULT_REPO_BRANCH,
    DEFAULT_REPO_URL,
    GITIGNORE_FILENAME,
    GLOBAL_REGISTRY_FILE,
    GLOBAL_STATE_FILE,
    HISTORY_FILENAME,
    LEGACY_GLOBAL_CONFIG_FILE,
    compress_archives_enabled,
    history_limit,
    max_bak_files,
    LOCAL_GITIGNORE_ENTRIES,
    PLAN_FILENAME,
    PROMPT_FILENAME,
    README_FILENAME,
    SNAPSHOT_INSTALL_DIR,
    STATE_FILENAME,
    TRACKED_GITIGNORE_PROTECTIONS,
    UPDATE_CHECK_INTERVAL_HOURS,
    USER_INSTALL_PATH,
    VERSION,
    file_lock,
    find_project_root,
    get_editor,
    read_color_config,
    warn_if_sensitive_root,
)
from .models import Notes, Task, TaskList, TaskStatus
from .history import (
    archive_name,
    concat_archives,
    compress_to,
    fifo_cleanup,
    list_history_archives,
    read_archive,
)
from .parser import (
    _atomic_write_text,
    _find_completed_line,
    _find_section_end,
    load_repaired_plan,
    normalize_plan,
    parse_plan_file,
    render_plan,
    sanitize_task_text,
)


# Patterns that, if found at the top of a root ``.gitignore``, blanket-
# ignore everything. We append ``!``-negations to keep tracked files
# (PLAN.md, prompt.md, README.md) explicitly visible to Git.
_BLANKET_IGNORE_PATTERNS = (
    re.compile(r"^\s*\*\s*$"),                  # "*" (universal catch-all)
    re.compile(r"^\s*\.\s*$"),                  # "." (also catches everything)
    re.compile(r"^\s*\?\*\s*$"),                # "?*" (any single file)
    re.compile(r"^\s*\.ck/?\s*$"),              # ".ck" or ".ck/" → blanket on .ck
)

# A HISTORY.md *entry heading* is exactly a column-0 "### " followed
# by an ISO date prefix (e.g. "### 2026-09-07 16:04 | [3] task").
# Note bodies (which may contain arbitrary "### " text) are always
# wrapped in ``` fences by ``Notes.render``; the date anchor plus
# fence-state tracking excludes those false matches.
_HISTORY_ENTRY_RE = re.compile(r"^### \d{4}-\d{2}-\d{2}.*$")


def _iter_history_lines(content: str):
    """Yield ``(is_entry_heading, line_with_ends)`` per line.

    Lines inside ```/~~~ code fences are never entry headings. Fence
    state toggles on each fence marker, matching CommonMark behaviour
    closely enough for HISTORY.md (fences written by ``Notes.render``
    and ordinary hand edits).
    """
    in_fence = False
    for line in content.splitlines(keepends=True):
        stripped = line.lstrip()[:3]
        if stripped in ("```", "~~~"):
            in_fence = not in_fence
            yield False, line
            continue
        yield (not in_fence and bool(_HISTORY_ENTRY_RE.match(line))), line


def _count_history_entries(content: str) -> int:
    """Count real history entries (anchored headings, not body text)."""
    return sum(1 for is_entry, _ in _iter_history_lines(content) if is_entry)


def _last_history_entry(content: str) -> str:
    """Return the text from the last anchored entry heading to EOF.

    Falls back to "" when no anchored entry exists. This is stricter
    than ``content.rfind("### ")`` which could land inside a note
    body or code fence.
    """
    lines = list(_iter_history_lines(content))
    last_idx = -1
    for i, (is_entry, _) in enumerate(lines):
        if is_entry:
            last_idx = i
    if last_idx == -1:
        return ""
    return "".join(line for _, line in lines[last_idx:])


@dataclass
class FocusResult:
    """Result of a focus-mutating operation."""

    task_id: int
    title: str
    path: Path
    demoted_id: Optional[int] = None
    demoted_title: str = ""
    had_note: bool = False


@dataclass
class ProjectContext:
    """A resolved project context for READ operations (``ck st`` & co).

    ``source`` describes HOW the context was found:

    - ``"local"``  — the bound/start directory carries its own
      ``PLAN.md`` (real, or a dev-mode sandbox mirror);
    - ``"parent"`` — an ancestor directory (up to the Git repo root)
      carries the project;
    - ``"global"`` — nothing found locally: the most recently active
      project from the global registry.
    """

    root: Path
    source: str  # "local" | "parent" | "global"


# ---------------------------------------------------------------------- #
# Project context resolution helpers (local → parent → global registry)
# ---------------------------------------------------------------------- #


def _sandbox_plan_mirror(project_root: Path) -> Optional[Path]:
    """Sandboxed mirror of ``<project_root>/.ck/PLAN.md`` (dev mode).

    Pure path computation: unlike :func:`resolve_write_path` it never
    creates directories, so probing candidates during context
    resolution has no filesystem side effects. Returns None outside
    dev mode.
    """
    if not is_dev_mode():
        return None
    try:
        return (
            sandbox_root() / PROJECTS_SUBDIR
            / project_hash(project_root) / CK_DIR_NAME / PLAN_FILENAME
        )
    except (OSError, RuntimeError, ValueError):
        return None


def _has_resolvable_plan(project_root: Path) -> bool:
    """True when ``project_root`` has a PLAN.md (real or sandboxed)."""
    real = project_root / CK_DIR_NAME / PLAN_FILENAME
    if real.is_file():
        return True
    mirror = _sandbox_plan_mirror(project_root)
    return mirror is not None and mirror.is_file()


def _newest_real_plan(root_plan: Path, real: Path) -> Optional[Path]:
    """Newest of the two real plan candidates, or None when neither exists.

    The project-root ``PLAN.md`` wins ties (``>=``) so a hand-authored
    root plan is preferred over an equally fresh ``.ck`` copy.
    """
    try:
        if root_plan.is_file():
            if (not real.is_file()
                    or root_plan.stat().st_mtime_ns >= real.stat().st_mtime_ns):
                return root_plan
            return real
        return real if real.is_file() else None
    except OSError:
        return real if real.is_file() else None


def _sync_sandbox_plan_mirror(project_root: Path, *,
                              seed_missing: bool = False,
                              promote_missing: bool = False) -> None:
    """Bidirectional, mtime-based convergence of the plan copies (dev mode).

    Three on-disk candidates take part:

    - the project-root ``PLAN.md`` (``<root>/PLAN.md``),
    - the real ``<root>/.ck/PLAN.md``,
    - the sandboxed mirror (``.sandbox/projects/<hash>/.ck/PLAN.md``)
      — the copy dev-mode mutations actually write.

    Direction is decided STRICTLY by ``st_mtime_ns``:

    - MIRROR NEWER than the newest real plan → the last write was a
      dev mutation (``ck-dev add``/``start``/``done``/…): the mirror
      content is PROMOTED into the existing project-root ``PLAN.md``
      so read-after-write holds — ``ck-dev st`` right after
      ``ck-dev add`` shows the new task, and the root file itself
      contains it. Without this, the unidirectional model hid every
      dev mutation behind the (stale) root plan on the read path.
    - MIRROR OLDER → the real plan changed underneath the session
      (external editor, ``git pull``/``checkout``, a tool rewriting
      the file): the mirror is re-seeded so later mutations compose
      on current content instead of forking from a stale snapshot.
    - EQUAL mtimes / equal content → converged no-op. The content
      equality guard on both write branches prevents mtime
      ping-pong: every sync would otherwise bump the target's mtime
      past the source and flip the direction on the next read
      forever.

    The promotion target is ONLY the project-root ``PLAN.md`` — it
    is not a sandbox-protected path, so the write is a deliberate
    real (production) side effect. The real ``.ck/`` tree stays
    immutable in dev mode (production-immutability contract); it
    re-converges indirectly because the promoted root plan becomes
    the newest real source for later re-seeds.

    MISSING TARGET (``promote_missing=True`` — read/display path
    ONLY): when the project-root ``PLAN.md`` does NOT exist yet (a
    project bootstrapped entirely in dev mode — e.g.
    ``.sandbox/projects/<hash>/`` fixtures or an uninitialized
    directory with just ``.ck/PLAN.md``), the dev mutation has no
    real file to promote into and display would silently fall back
    to the (stale-seeming) ``.ck`` copy. With the flag set, the
    root plan is CREATED from the mirror content so the mutation is
    durable and read-after-write holds. WITHOUT the flag (the
    mutation path) a missing root plan is still never created:
    dev mutations must not materialize production files behind the
    user's back — display falls back to the real ``.ck/PLAN.md``
    (or the mirror) as usual.

    SOURCE SELECTION: the authoritative real plan is the NEWEST of
    the two real candidates (see :func:`_newest_real_plan`) — a
    root-level plan edited more recently than ``.ck/PLAN.md`` wins,
    so the mirror (and every later dev mutation) composes on the
    content the read commands actually display.

    SEEDING: with ``seed_missing=True`` (MUTATION path only —
    :meth:`ContextKeeper._plan_load_path`) a MISSING mirror is
    created from the newest real plan, so the FIRST dev mutation of
    a session forks from current root content instead of the stale
    ``.ck`` scaffold. Read paths never pass the flag: a read must
    not materialize the ``.sandbox/`` skeleton.

    Called from BOTH the mutation paths
    (:meth:`ContextKeeper._plan_load_path`, i.e. every
    ``add``/``start``/``done``/``note``/``save`` read-modify-write
    cycle), ``ck-dev edit`` (:meth:`ContextKeeper.edit_plan`) AND
    the read path (:meth:`ContextKeeper._plan_display_path`, i.e.
    ``st``/``list``/``st --all``). Healing on every read
    converges the copies eagerly — an external edit followed by
    nothing but ``ck-dev st`` still leaves the mirror up to date,
    and a pending dev mutation is promoted by the very next read
    (including first-time CREATION of the root plan when the
    project carries only ``.ck/PLAN.md``).

    No-ops (silently) outside dev mode, when there is no real file,
    or on any filesystem error — this is a freshness heuristic and
    must never break a command.
    """
    if not is_dev_mode():
        return
    real = project_root / CK_DIR_NAME / PLAN_FILENAME
    root_plan = project_root / PLAN_FILENAME
    mirror = _sandbox_plan_mirror(project_root)
    if mirror is None:
        return
    try:
        if not mirror.is_file():
            if not seed_missing:
                return
            # First dev mutation of the session: seed the working
            # copy from the NEWEST real plan so the mutation composes
            # on current content instead of forking from a stale
            # ``.ck`` snapshot.
            source = _newest_real_plan(root_plan, real)
            if source is None:
                return
            mirror.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(mirror, source.read_text(encoding="utf-8"))
            log_sandbox_debug(
                f"Seeded PLAN.md mirror from newest real plan "
                f"({source} -> {mirror})"
            )
            return

        # Authoritative real source: the NEWEST on-disk plan — the
        # project-root PLAN.md when it exists and was edited after
        # .ck/PLAN.md (or when .ck/PLAN.md is absent), else .ck/PLAN.md.
        source = _newest_real_plan(root_plan, real)
        if source is None:
            return
        mirror_ns = mirror.stat().st_mtime_ns
        source_ns = source.stat().st_mtime_ns
        if mirror_ns > source_ns:
            # The mirror is the NEWEST copy overall: a dev mutation
            # wrote last — promote its content into the root plan.
            # Target is ONLY the root plan (not sandbox-protected);
            # when it does not exist yet (dev-bootstrapped project
            # with only ``.ck/PLAN.md``), it is created — but only
            # on the READ path (``promote_missing=True``): a dev
            # mutation must never materialize production files.
            text = mirror.read_text(encoding="utf-8")
            if root_plan.is_file():
                if root_plan.read_text(encoding="utf-8") != text:
                    # Real, atomic write: the root plan is NOT an
                    # intercepted path, so this deliberately updates
                    # production (read-after-write promotion).
                    _atomic_write_text(root_plan, text)
                    log_sandbox_debug(
                        f"Promoted newer PLAN.md mirror to root plan "
                        f"({mirror} -> {root_plan})"
                    )
            elif promote_missing:
                # First-ever materialization of the root plan from a
                # dev-only session: display requires it (read-after-
                # write across ``ck-dev add`` → ``ck-dev st``), so the
                # mutation becomes durable in the real project.
                # CONTENT GUARD: a re-seeded mirror (real file edited
                # underneath the session) re-emerges with a NEWER
                # mtime but IDENTICAL content to the source — that is
                # a healing artifact, not a dev mutation. Creating a
                # duplicate root copy for it would let a bare read
                # fabricate production files; only content that
                # actually differs from the newest real plan (a real
                # dev mutation composed on it) is promoted.
                if source.read_text(encoding="utf-8") != text:
                    _atomic_write_text(root_plan, text)
                    log_sandbox_debug(
                        f"Created root PLAN.md from newer mirror "
                        f"({mirror} -> {root_plan})"
                    )
        elif mirror_ns < source_ns:
            # Real plan changed underneath the session: re-seed the
            # stale mirror so later mutations compose on current
            # content.
            text = source.read_text(encoding="utf-8")
            if mirror.read_text(encoding="utf-8") != text:
                # Atomic write; the mirror path already lives inside
                # the sandbox, so interception passes it through.
                _atomic_write_text(mirror, text)
                log_sandbox_debug(
                    f"Re-seeded stale PLAN.md mirror from newer real "
                    f"file ({source} -> {mirror})"
                )
        # EQUAL mtimes (or equal content): converged — the equality
        # guards above prevent mtime ping-pong across repeated reads.
    except OSError:
        return  # freshness/convergence must never break a command


def _upward_context_candidates(start: Path) -> list:
    """Directories from ``start`` upward for context resolution.

    Inside a Git work tree the walk stops AT (and includes) the
    repository root — a ``.ck/`` project living above the repo
    boundary is deliberately NOT picked up. Outside Git the walk
    continues to the filesystem root (the classic
    :func:`find_project_root` behaviour).
    """
    try:
        curr = start.resolve()
    except (OSError, RuntimeError):
        curr = Path(os.path.abspath(str(start)))
    git_top = gith.repo_root(curr)
    out = [curr]
    for parent in curr.parents:
        if git_top is not None:
            try:
                parent.relative_to(git_top)
            except ValueError:
                break  # escaped the Git repository boundary
        out.append(parent)
    return out


def _global_context() -> Optional[ProjectContext]:
    """The most recently active registered project, when usable.

    Registry entries are already sorted by ``last_seen`` (newest
    first); the first one whose folder still exists AND carries a
    resolvable PLAN.md (real or sandboxed) wins. Never raises and
    never creates the registry (no lock churn on a machine where
    nothing was ever registered).
    """
    try:
        if not registry.has_registry():
            return None
        entries = registry.list_projects()
    except Exception:
        # Read-side degradation: a locked/corrupt registry must not
        # crash a read-only command — behave as "no global context".
        return None
    for entry in entries:
        raw = getattr(entry, "path", "")
        if not raw:
            continue
        try:
            root = Path(raw)
        except (TypeError, ValueError):
            continue
        if _has_resolvable_plan(root):
            return ProjectContext(root=root, source="global")
    return None


class ContextKeeper:
    """High-level orchestrator. One instance per CLI invocation."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root: Optional[Path] = root or find_project_root()
        # Resolution anchor for READ operations (see resolve_context):
        # the directory the caller bound explicitly, else the process
        # cwd — NEVER find_project_root()'s unbounded walk result, so
        # the Git-repo-root traversal bound cannot be pre-escaped.
        self._start: Optional[Path] = (
            Path(root).resolve() if root is not None else None
        )
        # DANGLING CWD DEGRADATION: when the working directory has
        # been wiped underneath the process, ``find_project_root()``
        # returns None (see :func:`cklib.config.find_project_root`).
        # The keeper then runs ROOTLESS: global registry commands
        # (``ck st -g``, ``dashboard``, ``register``, ``unregister``,
        # ``prune``) operate purely on registry state, while local
        # commands are rejected by the CLI with a clean "not in a
        # valid project directory" message instead of a traceback.
        if self.root is None:
            self._start = None
            self.ck_path = None
            self.state_file = None
            self.plan_file = None
            self.history_file = None
            self.prompt_file = None
            self.readme_file = None
            self._color_overrides: Optional[dict] = {}
            return
        self.ck_path: Path = self.root / CK_DIR_NAME
        self.state_file: Path = self.ck_path / STATE_FILENAME
        self.plan_file: Path = self.ck_path / PLAN_FILENAME
        self.history_file: Path = self.ck_path / HISTORY_FILENAME
        self.prompt_file: Path = self.ck_path / PROMPT_FILENAME
        self.readme_file: Path = self.ck_path / README_FILENAME
        # Lazily-loaded palette overrides from .ck.json (see
        # color_overrides()).
        self._color_overrides: Optional[dict] = None

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _ensure_ck_dir(self) -> None:
        # ROOTLESS keeper (dangling cwd): there is no project
        # directory to create anything in — surface the same clean
        # error the CLI shows for local commands.
        if self.ck_path is None:
            raise ValueError(
                "Not in a valid project directory. "
                "cd into your project (or use a global command: ck st -g)."
            )
        # DEV MODE: the .ck/ tree is created inside the sandbox copy
        # for this project, never in the real project directory.
        if is_dev_mode():
            resolve_write_path(self.ck_path).mkdir(parents=True, exist_ok=True)
        else:
            self.ck_path.mkdir(parents=True, exist_ok=True)

    def _open_history_append(self):
        """Return (fh, real_path_untouched) for HISTORY.md appends.

        Production path: opens the real file in append mode.
        DEV MODE: appends go to the SANDBOXED copy — seeded from the
        original on first append so the dev session starts from the
        real history instead of a blank slate (entries are never
        lost, and the real file is never modified).
        """
        if not is_dev_mode():
            return self.history_file.open("a", encoding="utf-8")
        sandboxed = resolve_write_path(self.history_file)
        if not sandboxed.exists():
            if self.history_file.exists():
                sandboxed.write_text(
                    self.history_file.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            else:
                sandboxed.write_text("", encoding="utf-8")
        return sandboxed.open("a", encoding="utf-8")

    def _write_if_missing(self, path: Path, content: str, label: str,
                          *, echo: bool = True, printer=print) -> bool:
        if not path.exists():
            # Atomic even on first creation so a crash mid-init can
            # never leave a torn template file.
            _atomic_write_text(path, content)
            if echo:
                printer(f"[+] Created: {label}")
            return True
        if echo:
            printer(f"[i] Exists: {label}")
        return False

    def _check_tools(self) -> dict[str, bool]:
        py_ok = sys.version_info >= (3, 8)
        tools = {
            "python3.8+": py_ok,
            "git": shutil.which("git") is not None,
            "fzf": shutil.which("fzf") is not None,
            "jq": shutil.which("jq") is not None,
        }
        if not py_ok:
            v = sys.version_info
            print(
                f"Warning: Python {v.major}.{v.minor} detected. "
                "Python 3.8+ recommended."
            )
        return tools

    def _rotate_history(self) -> None:
        """Archive HISTORY.md when it exceeds the entry limit.

        The rotation is performed under ``file_lock`` on the history
        file and is *gapless*: the new (trimmed) content is written to
        a temp file FIRST, then the old file is renamed to the
        timestamped archive, then the temp file is moved into place.
        A crash at any point leaves either the old or the new file
        present — never a missing HISTORY.md.

        Entry counting and tail extraction match only column-0
        ``### <date>`` headings, so ``###`` text inside note bodies
        or code fences can never trigger a false rotation or corrupt
        the preserved tail.

        Archive format (``COMPRESS_ARCHIVES``, default True):
        ``HISTORY_<YYYYMMDD>_<HHMMSS>.md.gz`` — gzip-compressed with
        Python's built-in ``gzip`` module (fully cross-platform, no
        external binaries). When compression is disabled the legacy
        uncompressed ``HISTORY_<YYYYMMDD>_<HHMMSS>.md.bak`` name is
        preserved. Retention is enforced FIFO-style: the oldest
        archives beyond ``MAX_BAK_FILES`` are deleted after each
        rotation.
        """
        now = datetime.now()
        with file_lock(self.history_file):
            # DEV MODE: rotation is a WRITE flow — operate entirely on
            # the sandboxed copies (archive + replacement) while
            # reading the ORIGINAL history for the preserved tail.
            content = self.history_file.read_text(encoding="utf-8")
            last_entry = _last_history_entry(content)
            target_dir = (
                resolve_write_path(self.ck_path) if is_dev_mode()
                else self.ck_path
            )
            if is_dev_mode():
                # COPY-ON-FIRST-WRITE: the rotation consumes the
                # history file itself (rename), so the sandboxed
                # mirror must exist before the rename. Without this,
                # the first dev-mode rotation would archive an empty
                # tree and drop the real context on the floor.
                dev_history = resolve_write_path(self.history_file)
                if not dev_history.exists():
                    dev_history.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self.history_file, dev_history)
            compressed = compress_archives_enabled()
            # The archive is first renamed under the legacy .md.bak
            # name (honest extension for plain bytes); compression
            # then upgrades it to .md.gz. When compression fails the
            # file stays a valid uncompressed .md.bak — data is never
            # lost and extensions always match content.
            # Same-second rotations get a dedupe suffix so no
            # archive can ever silently overwrite another.
            seq = 0
            while (
                (target_dir / archive_name(now, compressed=False, seq=seq)).exists()
                or (target_dir / archive_name(now, compressed=True, seq=seq)).exists()
            ):
                seq += 1
            plain = target_dir / archive_name(now, compressed=False, seq=seq)
            gz = target_dir / archive_name(now, compressed=True, seq=seq)
            header = (
                f"# History {self.root.name}\n"
                f"Archive: {gz.name if compressed else plain.name}\n\n"
            )
            new_text = header + last_entry
            # Write the replacement content to a temp file first so
            # there is never a window where HISTORY.md is absent.
            # fsync before the renames so a crash cannot leave an
            # empty renamed file.
            fd, tmp_name = tempfile.mkstemp(
                dir=str(target_dir), prefix=".ck-history-", suffix=".tmp"
            )
            try:
                try:
                    mode = os.stat(self.history_file).st_mode & 0o777
                except OSError:
                    mode = 0o644
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                    fh.write(new_text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp_name, mode)
                # In dev mode the REAL history file is never renamed:
                # the sandbox copy takes the rotation instead.
                rotated = resolve_write_path(self.history_file) \
                    if is_dev_mode() else self.history_file
                if rotated.exists():
                    rotated.rename(plain)
                if compressed:
                    # Gzip-compress the renamed archive: compress to a
                    # temp sibling first, then atomically replace. On
                    # failure the plain .md.bak stays in place (still
                    # a complete archive) and the temp file is removed.
                    tmp_gz = target_dir / f".{gz.name}.tmp"
                    try:
                        compress_to(plain, tmp_gz)
                        os.replace(tmp_gz, gz)
                        plain.unlink()
                    except OSError:
                        try:
                            os.unlink(tmp_gz)
                        except OSError:
                            pass
                os.replace(tmp_name, rotated)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            # FIFO retention: prune the oldest archives beyond
            # MAX_BAK_FILES (both .md.gz and legacy .md.bak count
            # against the same budget).
            pruned = fifo_cleanup(target_dir, max_bak_files())
        message = (
            f"History reached limit ({history_limit()} entries) "
            "and was archived. Context preserved."
        )
        if pruned:
            message += f" ({len(pruned)} oldest archive(s) purged.)"
        print(message)

    def _read_state(self) -> dict:
        path = self.state_file
        # DEV MODE read-your-writes: once a sandboxed state.json copy
        # exists (written by a previous dev-mode mutation such as
        # ``ck note``), reads prefer it so chained commands compose;
        # with no sandbox copy yet, reads come from the real file.
        # The mirror probe uses create=False — a read must never
        # materialize the .sandbox/ skeleton.
        if is_dev_mode():
            sandboxed = resolve_write_path(self.state_file, create=False)
            if sandboxed.exists():
                path = sandboxed
        if path is None or not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_state(self, state: dict) -> None:
        """Atomic-replace write of ``state.json`` under ``file_lock``.

        Concurrent ``ck`` invocations in the same project serialise on
        ``.ck/state.json.lock``; the tmp+rename writer guarantees
        readers never observe a torn file. DEV MODE: both the mkdir
        and the atomic write are redirected into the sandbox (the
        atomic writer intercepts itself; the lock too).
        """
        if is_dev_mode():
            resolve_write_path(self.ck_path).mkdir(parents=True, exist_ok=True)
        else:
            self.ck_path.mkdir(parents=True, exist_ok=True)
        text = json.dumps(state, indent=2, ensure_ascii=False)
        with file_lock(self.state_file):
            _atomic_write_text(self.state_file, text)

    # ------------------------------------------------------------------ #
    # Gitignore helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _detect_blanket_ignore(gitignore_text: str) -> bool:
        """Return True if the file contains a blanket ignore rule."""
        for line in gitignore_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for pat in _BLANKET_IGNORE_PATTERNS:
                if pat.match(line):
                    return True
        return False

    @staticmethod
    def _ensure_root_gitignore(root: Path) -> list[str]:
        """Append local-only entries and tracked-file negations.

        If the existing root ``.gitignore`` contains a blanket ignore
        rule (``*``, ``.``, ``?*``), this method *additionally*
        appends ``!``-negations for tracked Context Keeper files
        (PLAN.md, prompt.md, README.md) so they remain visible to Git.

        Existing entries are detected by EXACT LINE match — a
        substring check would false-suppress when the entry text
        merely appears inside a comment (e.g. ``# .ck/state.json``)
        or as part of a longer pattern.
        """
        gi = root / ".gitignore"
        additions: list[str] = []
        if gi.exists():
            current = gi.read_text(encoding="utf-8")
        else:
            current = ""

        # Exact-line set: strip trailing whitespace only (gitignore
        # semantics), keep the line content verbatim.
        existing_lines = {ln.rstrip() for ln in current.splitlines()}

        for entry in LOCAL_GITIGNORE_ENTRIES:
            if entry not in existing_lines:
                additions.append(entry)

        # Negations: required when a blanket ignore rule is present.
        blanket = ContextKeeper._detect_blanket_ignore(current)
        negations: list[str] = []
        if blanket:
            for tracked in TRACKED_GITIGNORE_PROTECTIONS:
                negation = f"!{tracked}"
                if negation not in existing_lines:
                    negations.append(negation)

        if additions or negations:
            block_lines: list[str] = ["", "# Context Keeper"]
            block_lines.extend(additions)
            block_lines.extend(negations)
            block = "\n".join(block_lines) + "\n"
            # DEV MODE: appends go to the sandboxed project copy; the
            # real project .gitignore stays untouched.
            if is_dev_mode():
                gi = sandbox_project_dir(root) / ".gitignore"
            with gi.open("a", encoding="utf-8") as fh:
                fh.write(block)
        return additions + negations

    @staticmethod
    def _ensure_ck_gitignore(ck_path: Path) -> None:
        """Ensure ``.ck/.gitignore`` exists with the default content."""
        gi = ck_path / GITIGNORE_FILENAME
        if not gi.exists():
            # DEV MODE: template writes land in the sandbox copy.
            gi = resolve_write_path(gi)
            gi.write_text(DEFAULT_CK_GITIGNORE, encoding="utf-8")

    # ------------------------------------------------------------------ #
    # AST-based mutations
    # ------------------------------------------------------------------ #

    def _plan_load_path(self) -> Path:
        """Plan path for MUTATING read-modify-write cycles.

        DEV MODE read-your-writes: prefer the sandboxed copy once it
        exists (written by a previous dev-mode mutation) so chained
        commands compose; with no sandbox copy yet it is SEEDED from
        the newest real plan (root ``PLAN.md`` preferred over the
        ``.ck`` copy — see :func:`_sync_sandbox_plan_mirror`) so the
        FIRST dev mutation composes on current content instead of
        forking from a stale ``.ck`` snapshot. Only this MUTATION
        path seeds a missing mirror; read paths never materialize
        ``.sandbox/``.

        STALENESS GUARD: a mirror that is OLDER than the real
        PLAN.md means the real file was edited externally (editor,
        ``git pull``, …) after the dev session last touched it —
        the mirror is refreshed from the real file first, so the
        mutation composes on CURRENT content instead of forking
        from a stale snapshot (see
        :func:`_sync_sandbox_plan_mirror`).
        """
        plan_path = self.plan_file
        if plan_path is None:
            # ROOTLESS keeper — callers with a None plan path must
            # guard before calling (see _load_plan /
            # _require_bound_plan).
            return plan_path
        if is_dev_mode():
            # Mutations must compose on the NEWEST content wherever it
            # lives: converge the mirror first (bidirectional mtime
            # sync) and seed it when missing.
            _sync_sandbox_plan_mirror(self.root, seed_missing=True)
            sandboxed = resolve_write_path(plan_path, create=False)
            if sandboxed.exists():
                plan_path = sandboxed
        return plan_path

    def _plan_display_path(self) -> Path:
        """Plan path for READ-ONLY display (``ck st`` / ``ck list``).

        Precedence (BUG FIX — stale read state):

        1. The PROJECT-ROOT ``PLAN.md`` (``<root>/PLAN.md``) wins when
           it exists — it is the plan users hand-edit at the top of
           the project, so reads must reflect it. Returning the
           ``.ck/PLAN.md`` copy here made every read command
           (``st``/``list``/``st --all``) render the stale
           ``.ck/`` scaffold instead of the real root plan.
        2. Otherwise ``.ck/PLAN.md`` is displayed.
        3. In dev mode ONLY, a project that exists just in the
           sandbox (bootstrapped by ``ck-dev add``/``init`` in an
           uninitialized directory) falls back to its sandboxed
           mirror — the writes of the dev session stay visible
           instead of rendering a fake empty dashboard. A project
           WITH a real ``.ck/PLAN.md`` additionally gets the newer
           mirror promoted into the (possibly first-created) root
           plan — see the MISSING ROOT PLAN note below.

        MIRROR SYNC: whenever a real plan exists (root or ``.ck/``),
        this path also converges the sandbox mirror first — strictly
        ``st_mtime_ns``-based and BIDIRECTIONAL (see
        :func:`_sync_sandbox_plan_mirror`): a mirror newer than the
        real plan (the last write was a dev mutation) is PROMOTED
        into the root plan so read-after-write holds, while a STALE
        mirror is re-seeded from the newest real plan. Reads display
        fresh content either way, and converged copies are left
        untouched (no mtime churn on repeated reads).

        MISSING ROOT PLAN (dev-bootstrapped projects — only
        ``.ck/PLAN.md`` on disk, e.g. inside
        ``.sandbox/projects/<hash>/``): promotion now CREATES the
        root plan from the newer mirror (``promote_missing=True``
        on this read path only), so a task added by ``ck-dev add``
        is immediately visible to ``st``/``list`` AND
        durable in the real project. The method then returns the
        freshly promoted root plan — exactly the state a project
        with a pre-existing root plan is already in. Only a project
        with NO real plan anywhere (uninitialized directory, the
        dev session lives purely in the mirror) keeps falling back
        to the plain ``.ck/PLAN.md`` or the mirror — a read must
        never fabricate production files in a directory the user
        never initialized.
        """
        root_plan = self.root / PLAN_FILENAME
        if root_plan.is_file():
            _sync_sandbox_plan_mirror(self.root)
            return root_plan
        if self.plan_file.is_file():
            # No root plan: the dev mutation lives in the mirror —
            # promote (and, when nothing real exists yet, CREATE) the
            # root plan, then display the promoted file.
            _sync_sandbox_plan_mirror(self.root, promote_missing=True)
            if root_plan.is_file():
                return root_plan
            return self.plan_file
        mirror = _sandbox_plan_mirror(self.root)
        if mirror is not None and mirror.is_file():
            # Mirror-only project (no real plan anywhere): display
            # falls back to the mirror — no root plan is fabricated
            # for a directory the user never initialized.
            return mirror
        return self.plan_file

    def _require_bound_plan(self) -> None:
        """Write-command guard: a valid project plan must be bound.

        ``ck note`` / ``ck start`` invoked outside any initialized
        project (no local PLAN.md — real or sandboxed) must abort
        with a clear error instead of creating orphaned state
        entries. Note this intentionally does NOT fall back to the
        global registry: writes target the bound local project only.
        """
        if self.plan_file is None or not self._plan_load_path().is_file():
            raise ValueError(
                "No active project found. Run 'ck init' in a project "
                "directory first."
            )

    def _load_plan(self) -> TaskList:
        """Parse PLAN.md with AUTO-REPAIR.

        Corrupted content (pasted ANSI noise, orphaned control
        sequences) is cleaned out and atomically persisted back to
        disk on load — every mutating command self-heals the file as
        part of its read-modify-write pass. Returns an empty TaskList
        if the file is missing.

        DEV MODE read-your-writes: once a sandboxed PLAN.md copy
        exists (created by a previous dev-mode mutation), subsequent
        mutations read THAT copy so chained commands (add → start →
        done) compose instead of each clobbering the previous
        sandbox write. With no sandbox copy yet, reads come from the
        real PLAN.md and the first mutation seeds the sandbox. A
        mirror OLDER than the real file (external edit underneath
        the session) is re-seeded first — see
        :func:`_sync_sandbox_plan_mirror`.
        """
        if self.plan_file is None:
            # ROOTLESS keeper (dangling cwd): no project to load.
            raise ValueError(
                "No active project found. Run 'ck init' in a project "
                "directory first."
            )
        tl, _repaired = load_repaired_plan(self._plan_load_path())
        return tl

    def _commit_plan(self, task_list: TaskList) -> None:
        """Normalize (opt-in), then atomically write PLAN.md under lock.

        The lock serialises concurrent read-modify-write cycles (two
        ``ck add`` invocations in different terminals) so one task
        can't be silently lost. Note we take the lock only for the
        write; the caller's read+mutate is short-lived enough that
        the fail-closed lock prevents torn interleavings.
        """
        normalize_plan(task_list)
        text = render_plan(task_list)
        with file_lock(self.plan_file):
            _atomic_write_text(self.plan_file, text)

    def add_task(self, task_text: str) -> int:
        """Insert a new ``[ ] title`` task into PLAN.md and return its ID.

        The title is sanitized (ANSI escapes, control characters,
        and whitespace runs are stripped) so terminal output pasted
        into the CLI can never corrupt PLAN.md. The task is inserted
        at the end of ``## Current Sprint`` when that section exists,
        otherwise directly before ``## Completed``.
        """
        title = sanitize_task_text(task_text)
        if not title:
            raise ValueError("Task text is empty")

        self._ensure_ck_dir()

        if not self.plan_file.exists():
            tl = TaskList(source_text="")
            tl.add(title, status=TaskStatus.OPEN)
            self._commit_plan(tl)
            return 1

        tl = self._load_plan()
        section = _section_for_new_task(tl)
        insert_line = _sprint_insert_line(tl)
        new_task = tl.add(
            title, status=TaskStatus.OPEN, section=section,
            insert_line=insert_line,
        )

        # ``render_plan`` will place any task whose ``line_number``
        # exceeds the original source length just before the
        # ``## Completed`` header (or at EOF if none). We don't need
        # to manipulate line numbers manually.
        self._commit_plan(tl)
        # Adding the first task to an empty plan changes the active
        # task — keep the registry pointer consistent.
        self._sync_active_task(tl)
        return new_task.id

    def start(self, task_id: int) -> FocusResult:
        """Mark the given task as focused; demote any other focused task.

        ``task_id <= 0`` RESETS focus: the currently focused task (if
        any) is demoted to open and no new focus is set.

        When a previously focused task loses focus (moved to a NEW
        focus, or reset) the demotion is reported in
        :class:`FocusResult` (``demoted_*`` fields) so the CLI can
        surface the "Unfocused / Paused Context" handling, and the
        task is recorded in the ``paused_tasks`` registry (see
        :meth:`_register_paused`): a noted demotion moves its process
        note into the registry entry so the note stays bound to the
        paused task through further focus switches; a noteless one is
        recorded too so it never disappears into the generic skipped
        list. Re-focusing restores the note as the active process
        note; completing archives it to HISTORY.md.

        Aborts with a clear error when no project plan is bound (no
        initialized project) instead of fabricating an empty plan.
        """
        self._require_bound_plan()
        tl = self._load_plan()
        demoted: Optional[Task] = None
        target: Optional[Task] = None
        if task_id <= 0:
            # Focus RESET: demote the current focus (if any); no new
            # focus is set. ``TaskList.focus`` would raise on id 0.
            focused = tl.focused[0] if tl.focused else None
            if focused is not None:
                focused.status = TaskStatus.OPEN
                demoted = focused
        else:
            # Capture the previously focused task BEFORE ``focus()`
            # demotes it — afterwards it is no longer in ``focused``.
            focused_before = tl.focused[0] if tl.focused else None
            target = tl.focus(task_id)  # raises KeyError when unknown
            if focused_before is not None \
                    and focused_before.id != task_id:
                demoted = focused_before
        had_note = False
        if demoted is not None:
            note = self._note_for_task(demoted.id)
            had_note = bool(note)
            self._register_paused(demoted.id, demoted.title, note)
        if target is not None:
            # Re-focus side effect: a previously paused task regains
            # its registry note as the active process note (LIFO).
            self._restore_paused_note(target.id)
        self._commit_plan(tl)
        self._sync_active_task(tl)
        return FocusResult(
            task_id=target.id if target is not None else 0,
            title=target.title if target is not None else "",
            path=self.plan_file,
            demoted_id=demoted.id if demoted is not None else None,
            demoted_title=demoted.title if demoted is not None else "",
            had_note=had_note,
        )

    def done(self, spec: str) -> list[int]:
        """Mark one or more tasks as DONE.

        ``spec`` accepts single IDs (``3``), ranges (``2-4``), and
        comma-separated lists (``1,3,5``).

        Completing the task that carries the active-task process
        note (see :meth:`set_note`) automatically archives the note
        into ``HISTORY.md`` (dated entry) and clears it from the
        active-task state along with the completed task context.
        """
        ids = _parse_id_spec(spec)
        if not ids:
            raise ValueError(f"No valid task IDs in spec: {spec!r}")

        tl = self._load_plan()
        transitioned = tl.toggle_done(ids)
        if transitioned:
            completed = set(transitioned)
            # Archive the process note BEFORE clearing it: the note
            # is user data — it graduates into HISTORY.md instead of
            # vanishing with the completed task context.
            note_data = self.get_note()
            if note_data is not None \
                    and note_data.get("id") in completed:
                archived = tl.by_id(note_data["id"])
                if archived is not None:
                    self._archive_note_to_history(
                        archived, note_data["note"])
            # Paused-task registry: archived notes for completed
            # entries, then drop ALL completed tasks from the
            # registry — a done task is finished, never paused.
            for entry in self._paused_tasks():
                if entry["id"] in completed and entry["note"]:
                    archived = tl.by_id(entry["id"])
                    if archived is not None:
                        self._archive_note_to_history(
                            archived, entry["note"])
            self._purge_paused_tasks(completed)
            self._commit_plan(tl)
            # Completing the focused task changes which task is
            # active; the registry pointer must follow (task IDs are
            # positional and can shift, so re-derive from the AST).
            self._sync_active_task(tl)
            self._purge_note_if_completed(completed)
        return transitioned

    def _sync_active_task(self, tl: TaskList) -> None:
        """Reconcile the registry's active-task pointer with the AST.

        Task IDs are positional: they shift when tasks are removed,
        completed, or the file is edited externally. A stale
        ``active_task_id`` in the registry would then point at the
        WRONG task. Re-derive the active task from the AST (focused
        first, else first open) and update the registry whenever it
        differs from what is stored.
        """
        active = tl.active()
        registry.update_active_task(
            self.root,
            task_id=active.id if active is not None else None,
            task_title=active.title if active is not None else "",
        )

    def edit_note(self, task_id: int, note: str) -> None:
        """Append a note to HISTORY.md for ``task_id``.

        The note is durably persisted to HISTORY.md (as a dated
        entry) rather than mutating an in-memory AST that was
        silently discarded — the old behaviour was a no-op that
        lost the caller's data.
        """
        tl = self._load_plan()
        target = tl.by_id(task_id)
        if target is None:
            raise KeyError(f"No task with id {task_id}")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = Notes(
            task_id=task_id,
            task_title=target.title,
            timestamp=ts,
            comment=note,
            body="",
        )
        self._ensure_ck_dir()
        with file_lock(self.history_file):
            with self._open_history_append() as fh:
                fh.write(entry.render())

    # ------------------------------------------------------------------ #
    # COMMAND: note (active-task process note / scratchpad)
    # ------------------------------------------------------------------ #

    def set_note(self, text: str) -> dict:
        """Attach or update a short process note on the CURRENT
        active task (focused task, else first open task).

        The note lives in ``.ck/state.json`` under ``active_task``::

            {"active_task": {"id": 2, "title": "...", "note": "...",
                             "updated_at": "..."}}

        It is displayed by ``ck st`` (``* Note: <text>``) and purged
        automatically by :meth:`done` when the noted task is
        completed. Returns the stored note metadata dict.

        Aborts with a clear error when no project plan is bound (no
        initialized project) — no orphaned state entries are created.
        """
        note = sanitize_task_text(text)
        if not note:
            raise ValueError("Note text is empty")
        self._require_bound_plan()
        tl = self._load_plan()
        active = tl.active()
        if active is None:
            raise ValueError(
                "No active task to attach a note to "
                "(plan is empty or every task is done)"
            )
        state = self._read_state()
        state["active_task"] = {
            "id": active.id,
            "title": active.title,
            "note": note,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._write_state(state)
        return state["active_task"]

    def get_note(self) -> Optional[dict]:
        """Return the stored active-task note dict, or None.

        Only a structurally valid entry counts: ``note`` must be a
        non-empty string and ``id`` an int. Anything else (missing,
        corrupt, hand-edited) is treated as "no note".
        """
        data = self._read_state().get("active_task")
        if not isinstance(data, dict):
            return None
        note = data.get("note")
        tid = data.get("id")
        if isinstance(note, str) and note and isinstance(tid, int) \
                and not isinstance(tid, bool):
            return data
        return None

    # ------------------------------------------------------------------ #
    # Paused-task registry (Unfocused / Paused Context)
    # ------------------------------------------------------------------ #

    def _paused_tasks(self) -> list[dict]:
        """Return the stored ``paused_tasks`` registry (valid entries
        only), most recently paused first.

        Each entry is ``{"id": int, "title": str, "note": str}``
        (``note`` may be empty). Entries whose task no longer exists
        in the plan are filtered out on read — externally removed or
        completed tasks never surface as paused.
        """
        tl = self._load_plan()
        raw = self._read_state().get("paused_tasks")
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        seen: set[int] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            tid = item.get("id")
            title = item.get("title")
            note = item.get("note")
            if not isinstance(tid, int) or isinstance(tid, bool) \
                    or tid in seen:
                continue
            if not isinstance(title, str) or not isinstance(note, str):
                continue
            if tl.by_id(tid) is None:
                continue
            seen.add(tid)
            out.append({"id": tid, "title": title, "note": note})
        return out

    def _register_paused(self, task_id: int, title: str,
                         note: str) -> None:
        """Record a focus-loss demotion in the ``paused_tasks``
        registry.

        When the demoted task carried the active process note, the
        note MOVES into the registry entry: it stays bound to the
        paused task through further focus switches instead of being
        orphaned under (or wiped by) the next ``set_note``. The most
        recently paused task is kept first (LIFO restore order).
        Read-only projects never gain a state file.
        """
        state = self._read_state()
        paused = state.get("paused_tasks")
        entries: list[dict] = [e for e in paused if isinstance(e, dict)] \
            if isinstance(paused, list) else []
        # Drop any stale entry for this task, then prepend.
        entries = [e for e in entries if e.get("id") != task_id]
        entries.insert(0, {
            "id": task_id,
            "title": title,
            "note": note,
        })
        # Move the active note into the registry (never lose data).
        if note:
            active = state.get("active_task")
            if isinstance(active, dict) and active.get("id") == task_id:
                state.pop("active_task", None)
        state["paused_tasks"] = entries
        self._write_state(state)

    def _restore_paused_note(self, task_id: int) -> None:
        """Re-focus side effect: restore a paused task's registry
        note as the active process note (LIFO — the most recently
        paused task wins) and remove the registry entry."""
        state = self._read_state()
        paused = state.get("paused_tasks")
        if not isinstance(paused, list):
            return
        kept: list[dict] = []
        restored: Optional[dict] = None
        for entry in paused:
            if (isinstance(entry, dict) and entry.get("id") == task_id
                    and restored is None):
                restored = entry
                continue
            kept.append(entry)
        if restored is None:
            return
        if restored.get("note"):
            state["active_task"] = {
                "id": task_id,
                "title": restored.get("title", ""),
                "note": restored["note"],
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        state["paused_tasks"] = kept
        self._write_state(state)

    def pending_focus_loss(self) -> Optional[dict]:
        """Peek at what an immediate focus switch would demote.

        Returns ``{"id", "title", "has_note"}`` for the currently
        focused task, or None when no focus is set or no plan is
        bound. Read-only: the CLI uses this to decide whether the
        interactive note prompt applies BEFORE mutating anything.
        """
        if self.plan_file is None or not self.plan_file.exists():
            return None
        try:
            tl = self._load_plan()
        except (OSError, ValueError):
            return None
        focus = tl.focused[0] if tl.focused else None
        if focus is None:
            return None
        return {
            "id": focus.id,
            "title": focus.title,
            "has_note": self._note_for_task(focus.id) != "",
        }

    def is_already_focused(self, task_id: int) -> bool:
        """True when ``task_id`` is the currently focused task.

        Read-only check used by the CLI to make ``ck start <ID>``
        idempotent: re-focusing the already-focused task must be a
        clean no-op (no note prompt, no plan mutation)."""
        if self.plan_file is None or not self.plan_file.exists():
            return False
        try:
            tl = self._load_plan()
        except (OSError, ValueError):
            return False
        focus = tl.focused[0] if tl.focused else None
        return focus is not None and focus.id == task_id

    def _note_for_task(self, task_id: int) -> str:
        """Return the stored process-note text when it belongs to
        ``task_id`` (structurally valid entry only), else ""."""
        data = self.get_note()
        if data is None or data.get("id") != task_id:
            return ""
        return data["note"]

    def _drop_note(self) -> None:
        """Remove the stored active-task note when present. Writes
        state only when something is actually removed (read-only
        projects never gain a state file)."""
        state = self._read_state()
        if "active_task" not in state:
            return
        state.pop("active_task", None)
        self._write_state(state)

    def _archive_note_to_history(self, task: Task, note: str) -> None:
        """Append ``note`` to ``HISTORY.md`` as a dated Notes entry.

        Called by :meth:`done` when the noted task is completed: the
        note is user data — it graduates into the durable history
        instead of vanishing with the task context. Goes through
        ``_open_history_append`` so DEV MODE appends land in the
        sandboxed copy (the real HISTORY.md is never touched).
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = Notes(
            task_id=task.id,
            task_title=task.title,
            timestamp=ts,
            comment=note,
            body="",
        )
        self._ensure_ck_dir()
        with file_lock(self.history_file):
            with self._open_history_append() as fh:
                fh.write(entry.render())

    def _purge_note_if_completed(self, completed_ids: set) -> None:
        """Drop the active-task process note when its task was just
        completed. Writes state only when something is actually
        removed (read-only projects never gain a state file)."""
        state = self._read_state()
        active = state.get("active_task")
        if not isinstance(active, dict):
            return
        tid = active.get("id")
        if isinstance(tid, int) and tid in completed_ids:
            state.pop("active_task", None)
            self._write_state(state)

    def _purge_paused_tasks(self, completed_ids: set) -> None:
        """Remove completed tasks from the paused_tasks registry.
        Writes state only when an entry is actually removed."""
        state = self._read_state()
        paused = state.get("paused_tasks")
        if not isinstance(paused, list) or not completed_ids:
            return
        kept = [
            e for e in paused
            if not (isinstance(e, dict) and e.get("id") in completed_ids)
        ]
        if len(kept) != len(paused):
            state["paused_tasks"] = kept
            self._write_state(state)

    # ------------------------------------------------------------------ #
    # COMMAND: init
    # ------------------------------------------------------------------ #

    def init(self, *, register: bool = False) -> None:
        """Initialize the local project structure.

        Registration is opt-in so local initialization never mutates the
        global registry unless explicitly requested.
        """
        # ROOTLESS keeper (dangling cwd): there is no directory to
        # initialize into — abort with the clean local-command error
        # instead of crashing on the dead working directory.
        if self.ck_path is None or self.root is None:
            raise ValueError(
                "Not in a valid project directory. "
                "cd into your project and re-run `ck init`."
            )
        # Boundary guard: initializing in $HOME, /tmp, or the
        # filesystem root affects every command run beneath it.
        if not self.ck_path.exists():
            warn_if_sensitive_root(self.root)
        self._ensure_ck_dir()

        state = self._read_state()
        state.update({
            "version": VERSION,
            "project_name": self.root.name,
            "created_at": state.get("created_at") or datetime.now().isoformat(),
            "last_update": datetime.now().isoformat(),
            "current_step": state.get("current_step") or "Init",
            "tools": self._check_tools(),
        })
        self._write_state(state)

        added = self._ensure_root_gitignore(self.root)
        if added:
            print(f"[+] Root .gitignore updated: +{', +'.join(added)}")

        self._write_if_missing(
            self.plan_file,
            DEFAULT_PLAN.format(project_name=self.root.name),
            PLAN_FILENAME,
        )
        self._write_if_missing(
            self.prompt_file,
            DEFAULT_PROMPT,
            PROMPT_FILENAME,
        )
        self._write_if_missing(
            self.readme_file,
            _default_readme(self.root.name),
            README_FILENAME,
        )
        self._ensure_ck_gitignore(self.ck_path)

        if register:
            entry = registry.register_project(self.root, name=self.root.name)
            print(f"[ok] Registered: {entry.name} -> {entry.path}")
        print(f"[ok] Context Keeper v{VERSION} initialized at {self.root}")

    # ------------------------------------------------------------------ #
    # Project context resolution (read operations)
    # ------------------------------------------------------------------ #

    def resolve_context(self) -> Optional[ProjectContext]:
        """Resolve the project context for READ operations.

        Hierarchy (first match wins):

        1. LOCAL — the bound directory (explicit ``root``, else the
           process cwd) carries its own ``PLAN.md`` (real, or a
           dev-mode sandbox mirror);
        2. UPWARD — ancestor directories up to the Git repository
           root (or the filesystem root outside Git) carry the
           project;
        3. GLOBAL — nothing found locally: fall back to the most
           recently active project in the global registry.

        Returns None when no context can be resolved anywhere;
        callers render the explicit uninitialized state (see
        :func:`cklib.ui.render_no_project`) instead of fake metrics.

        ROOTLESS keeper (dangling cwd): a keeper whose ``root`` is
        ``None`` cannot inspect any local directory, so the LOCAL
        and UPWARD tiers are skipped entirely and resolution falls
        straight through to the GLOBAL registry tier.
        """
        if self._start is None:
            try:
                start = Path.cwd()
            except (OSError, RuntimeError):
                start = None
        else:
            start = self._start
        if start is None:
            # No local anchor at all (dangling cwd / rootless
            # keeper): global registry tier only.
            return _global_context()
        for i, candidate in enumerate(_upward_context_candidates(start)):
            if _has_resolvable_plan(candidate):
                return ProjectContext(
                    root=candidate,
                    source="local" if i == 0 else "parent",
                )
        return _global_context()

    def _keeper_for(self, root: Path) -> "ContextKeeper":
        """A :class:`ContextKeeper` bound to ``root`` (``self`` when
        the roots are identical), so status rendering (project name,
        note state, plan paths) reflects the RESOLVED context rather
        than the invocation directory."""
        if self.root is None:
            return ContextKeeper(root=root)
        try:
            same = self.root.resolve() == root.resolve()
        except (OSError, RuntimeError):
            same = Path(self.root) == Path(root)
        return self if same else ContextKeeper(root=root)

    def color_overrides(self) -> dict:
        """Palette overrides for the project bound to this keeper.

        Reads the optional ``"colors"`` mapping from ``.ck.json`` at
        the Context Keeper project root (cached per instance; see
        :func:`cklib.config.read_color_config`). Returns ``{}`` when
        nothing is configured — every palette slot then inherits the
        terminal's NATIVE text color (no low-contrast gray). Invalid
        values are rejected later at resolve time, never raising
        here.
        """
        if self._color_overrides is None:
            if self.root is None:
                self._color_overrides = {}
                return self._color_overrides
            try:
                self._color_overrides = read_color_config(self.root)
            except Exception:
                self._color_overrides = {}
        return self._color_overrides

    # ------------------------------------------------------------------ #
    # COMMAND: status / dashboard
    # ------------------------------------------------------------------ #

    def status(self) -> str:
        """Return the single-project status block as a string.

        The plan context is resolved through the full hierarchy
        (local → ancestors up to the Git repo root → global registry).
        When NO context resolves, an explicit uninitialized-state
        message is returned — never a fake ``0/0`` dashboard. The
        resolution heals a stale dev-mode mirror first (see
        :meth:`_plan_display_path`).
        """
        ctx = self.resolve_context()
        if ctx is None:
            return ui.render_no_project()
        keeper = self._keeper_for(ctx.root)
        tl = parse_plan_file(keeper._plan_display_path())
        return _render_local_status(keeper, tl, source=ctx.source)

    def tasks(self) -> str:
        """Return the resolved project's task list for STDOUT.

        A clean, pipe-friendly rendering (no editor, no decorations
        that would confuse grep/cut): one line per task as
        ``[<status marker>] <id>. <title>``, grouped by section with
        ``##``-style headers. Suitable for ``ck tasks | grep ...``.

        The plan context is resolved through the same hierarchy as
        ``ck st`` (stale-mirror healing included); with no
        resolvable context the empty-list hint is returned (no fake
        metrics involved).
        """
        ctx = self.resolve_context()
        if ctx is None:
            return "No tasks. Add one with `ck add <text>`."
        keeper = self._keeper_for(ctx.root)
        tl = load_repaired_plan(keeper._plan_display_path())[0]
        return _render_tasks_listing(tl)

    def notes(self) -> str:
        """Return all active process notes for the resolved project.

        Structured two-section listing:

            [>] Active Focus:
               - [<id>] <title>
                 * Note: <text>          (when a note is attached)
            [!] Unfocused / Paused Context:
               - [<id>] <title>
                 * Note: <text>          (each paused task's note)

        With no notes anywhere (active or paused) a single ``[i] No
        active process notes found.`` line is returned. The plan
        context is resolved through the same hierarchy as ``ck st``.
        """
        ctx = self.resolve_context()
        if ctx is None:
            return "No tasks. Add one with `ck add <text>`."
        keeper = self._keeper_for(ctx.root)
        tl = parse_plan_file(keeper._plan_display_path())
        return _render_notes_listing(keeper, tl)

    def full_plan_text(self) -> Optional[str]:
        """Full PLAN.md text of the resolved context (None if missing).

        Used by ``ck st --all``: the printed plan follows the same
        context resolution as the status block above it.
        """
        ctx = self.resolve_context()
        if ctx is None:
            return None
        path = self._keeper_for(ctx.root)._plan_display_path()
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def dashboard(self, *, verbose: bool = False) -> str:
        """Return the cross-project dashboard as a string.

        ``verbose=True`` renders the block view with full per-project
        triad context instead of the compact table.

        ROOT-INDEPENDENT: the dashboard reads only the global
        registry, so it renders identically from a keeper whose
        ``root`` is ``None`` (dangling working directory) — the cwd
        marker simply marks no project.
        """
        return _render_dashboard(
            self,
            list_projects=registry.list_projects,
            parse_plan_file=_safe_parse_plan,
            verbose=verbose,
        )

    # ------------------------------------------------------------------ #
    # COMMAND: register / unregister / prune (global registry)
    # ------------------------------------------------------------------ #

    def register(self, path: Optional[Path] = None,
                 name: Optional[str] = None) -> "registry.ProjectEntry":
        """Register a project in the global registry.

        ``path`` defaults to ``self.root`` (the current project). The
        project's last-seen timestamp is refreshed. ROOTLESS keeper
        (dangling cwd): ``path`` becomes mandatory — registering
        with no explicit path and no resolvable project root raises
        the same user-facing error the CLI shows for local commands.
        """
        if path is not None:
            target = Path(path).resolve()
        elif self.root is not None:
            target = self.root.resolve()
        else:
            raise ValueError(
                "Not in a valid project directory. "
                "Pass an explicit path: ck register --path <dir>"
            )
        project_name = name or target.name
        return registry.register_project(target, name=project_name)

    def unregister(self, path: Optional[Path] = None,
                   name: Optional[str] = None) -> bool:
        """Remove a project from the global registry.

        Resolution order:
        1. ``path`` (if given)
        2. ``name`` (matches the registry ``name`` field)
        3. ``self.root`` (default: current working project)

        Returns True if anything was removed. Succeeds gracefully
        when the path is not in the registry or the folder is
        missing from disk.
        """
        if path is not None:
            return registry.remove_project(Path(path))
        if name is not None:
            return registry.remove_project_by_name(name)
        if self.root is None:
            raise ValueError(
                "Not in a valid project directory. "
                "Pass an explicit --path or --name to unregister."
            )
        return registry.remove_project(self.root)

    def prune(self) -> list[str]:
        """Drop registry entries whose folders are gone from disk.

        Pure global-registry operation: works regardless of the
        local project root (or its absence).
        """
        return registry.prune_missing()

    # ------------------------------------------------------------------ #
    # COMMAND: save (two-step Notes + local commit)
    # ------------------------------------------------------------------ #

    def save(self, *, input_fn=input, stdin_read=sys.stdin.read,
             printer=print) -> Optional[str]:
        """Two-step save: optional local-history note, then optional Git commit.

        Step 1 appends a dated entry to ``.ck/HISTORY.md`` — Context
        Keeper's own plain-text context log. This is LOCAL history
        and involves no Git whatsoever. Step 2 optionally creates a
        LOCAL Git commit of the ck artifacts. Prompts and
        confirmations explicitly label which step is which so a user
        can never mistake a local history entry for a Git commit (or
        vice versa).

        If Git is unavailable and the user declines initialization,
        the commit phase is bypassed gracefully — no subprocess
        errors, no prompts past that point.
        """
        if self.state_file is None or not self.state_file.exists():
            printer("ERROR: Run `ck init` first.")
            return None

        tl = self._load_plan()
        active = tl.active()
        if active is None:
            printer("[!] No active task to save.")
            return None

        task_id, task_title = active.id, active.title
        history_rel = f"{CK_DIR_NAME}/{HISTORY_FILENAME}"
        printer(f"-> Active task: [{task_id}] {task_title}")

        note_recorded = False
        try:
            # ---- Step 1: LOCAL HISTORY (HISTORY.md, no Git) ------ #
            printer("")
            printer(
                f"Step 1: local history - append entry to "
                f"{history_rel} (not a Git commit)"
            )
            body = self._prompt_note(input_fn, printer)

            comment = input_fn(
                "Short summary (saved with the history entry; "
                "reused as default commit subject): "
            ).strip() or "update"

            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            note = Notes(
                task_id=task_id,
                task_title=task_title,
                timestamp=ts,
                comment=comment,
                body=body,
            )
            # Append the note and evaluate rotation under the same lock
            # so concurrent ``ck save`` invocations serialise (no
            # interleaved partial note blocks, no double rotation).
            # Entry counting matches only anchored "### <date>" headings —
            # "### " inside note bodies/code fences is not an entry.
            # DEV MODE: the append lands in the sandboxed copy; the
            # rotation count is then read from that same copy.
            with file_lock(self.history_file):
                with self._open_history_append() as fh:
                    fh.write(note.render())
                if is_dev_mode():
                    history_text = resolve_write_path(
                        self.history_file).read_text(encoding="utf-8")
                else:
                    history_text = self.history_file.read_text(
                        encoding="utf-8")
                needs_rotation = (
                    _count_history_entries(history_text) > history_limit()
                )
            note_recorded = True
            printer(
                f"[ok] Saved entry to {history_rel} "
                "(local context history - not a Git commit)."
            )

            state = self._read_state()
            state["last_update"] = datetime.now().isoformat()
            state["current_step"] = task_title
            self._write_state(state)

            if needs_rotation:
                self._rotate_history()

            # ---- Step 2: LOCAL GIT COMMIT (separate from history) - #
            printer("")
            printer(
                "Step 2: Git commit - optional, "
                "separate from local history"
            )
            if is_dev_mode():
                # Guardrail: a dev-mode session must never create a
                # real Git commit (or init a repo) in the project.
                # The sandboxed history entry above is already saved.
                printer(
                    f"[i] Dev mode: Git commit skipped "
                    f"(sandbox). Entry saved in {history_rel}."
                )
                return None
            if not gith.is_git_repo(self.root):
                ans = input_fn(
                    "Not a Git repo. Initialize one for "
                    "local commits? (y/N): "
                ).strip().lower()
                if ans != "y":
                    printer(
                        f"[i] No commit created. Your entry is "
                        f"saved in {history_rel} (local history only)."
                    )
                    return None
                if not gith.init_repo(self.root):
                    printer(
                        f"ERROR: Git init failed; commit skipped "
                        f"(entry stays saved in {history_rel})."
                    )
                    return None

            default_msg = f"{task_title}: {comment}".strip(": ")
            custom = input_fn(
                "Git commit message [Enter=accept / type custom]: "
            ).strip()
            msg = custom or default_msg

            if input_fn(
                f"Create LOCAL Git commit \"{msg}\"? (y/N): "
            ).strip().lower() != "y":
                printer(
                    f"[i] No commit created. Your entry is saved "
                    f"in {history_rel} (local history only)."
                )
                return None
        except EOFError:
            # Non-interactive stdin ran out mid-flow. Abort cleanly:
            # whatever was durably written (note) stays; the commit
            # phase simply never runs.
            if note_recorded:
                printer(
                    f"ERROR: Input closed - entry saved to "
                    f"{history_rel}, no Git commit created."
                )
            else:
                printer("ERROR: Input closed - save aborted (nothing written).")
            return None

        # Stage ONLY the Context Keeper artifacts (PLAN.md, HISTORY.md,
        # prompt.md, README.md, .ck/.gitignore). Never ``git add .`` —
        # that would sweep unrelated untracked files (potentially
        # secrets) into the commit — and never stage the whole .ck/
        # directory, which would include transient *.lock files.
        # Pathspecs are cwd-relative, so they resolve correctly even
        # when the Git work tree root is a parent of the project.
        ck_artifacts = [
            f"{CK_DIR_NAME}/{PLAN_FILENAME}",
            f"{CK_DIR_NAME}/{HISTORY_FILENAME}",
            f"{CK_DIR_NAME}/{PROMPT_FILENAME}",
            f"{CK_DIR_NAME}/{README_FILENAME}",
            f"{CK_DIR_NAME}/{GITIGNORE_FILENAME}",
        ]
        stage = [p for p in ck_artifacts if (self.root / p).exists()]
        if gith.local_commit(msg, path=self.root, stage=stage):
            printer("[ok] Git commit created (local only - no push).")
            return msg
        printer(
            f"[!] Commit failed (entry remains saved in {history_rel})."
        )
        return None

    def _prompt_note(self, input_fn: Callable[[str], str],
                     printer=print) -> str:
        """Prompt for a note body. Returns stripped body or ""."""
        skip = input_fn(
            "Add a note for this task? [s=skip / e=editor / Enter=type]: "
        ).strip().lower()
        if skip == "s" or skip == "skip":
            return ""
        if skip == "e" or skip == "editor":
            # DEV MODE: the editor scratch file lives in the sandbox
            # copy of .ck/, never in the real project tree.
            tmp_dir = (
                resolve_write_path(self.ck_path) if is_dev_mode()
                else self.ck_path
            )
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp = tmp_dir / ".ck_note.tmp"
            tmp.write_text("", encoding="utf-8")
            try:
                subprocess.run([get_editor(self.root), str(tmp)], check=False)
                return tmp.read_text(encoding="utf-8").strip()
            finally:
                tmp.unlink(missing_ok=True)

        printer("Enter note text (empty line to finish):")
        lines: list[str] = []
        try:
            while True:
                line = input_fn("> ")
                if not line:
                    break
                lines.append(line)
        except EOFError:
            pass
        return "\n".join(lines).strip()

    # ------------------------------------------------------------------ #
    # COMMAND: edit / log (open in editor)
    # ------------------------------------------------------------------ #

    def edit_plan(self) -> None:
        # ROOTLESS keeper: no project directory to open an editor in.
        if self.root is None or self.plan_file is None:
            raise ValueError(
                "Not in a valid project directory — nothing to edit."
            )
        # DEV MODE: the editor edits the SANDBOXED copy; the real
        # PLAN.md is opened read-only in effect (never handed to the
        # editor as a write target).
        target = (
            resolve_write_path(self.plan_file) if is_dev_mode()
            else self.plan_file
        )
        if is_dev_mode() and not target.exists() and self.plan_file.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                self.plan_file.read_text(encoding="utf-8"), encoding="utf-8"
            )
        elif is_dev_mode() and target.exists() and self.plan_file.exists():
            # Mirror exists: refresh it when the real file changed on
            # disk since the last dev touch, so the editor opens
            # CURRENT content instead of a stale snapshot.
            _sync_sandbox_plan_mirror(self.root)
        subprocess.run([get_editor(self.root), str(target)], check=False)

    def edit_log(self) -> None:
        # ROOTLESS keeper: no project directory to open an editor in.
        if self.root is None or self.history_file is None:
            raise ValueError(
                "Not in a valid project directory — no history to edit."
            )
        target = (
            resolve_write_path(self.history_file) if is_dev_mode()
            else self.history_file
        )
        if is_dev_mode() and not target.exists() and self.history_file.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                self.history_file.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        subprocess.run([get_editor(self.root), str(target)], check=False)

    # ------------------------------------------------------------------ #
    # COMMAND: log --all (archive-aware read-only view)                   #
    # ------------------------------------------------------------------ #

    def read_full_history(self) -> str:
        """Full history text: archives (old→new) then the live file.

        Every rotation archive in ``.ck/`` is read — ``.md.gz``
        transparently decompressed, legacy ``.md.bak`` read as plain
        text — in chronological order and concatenated, followed by
        the current ``HISTORY.md``. A corrupted archive contributes a
        warning line in place instead of failing the whole view.

        In dev mode the sandboxed mirror is preferred when it exists
        (write-your-writes), while archive listing always includes the
        ORIGINAL ``.ck/`` so real rotation archives stay visible.
        """
        if self.root is None or self.ck_path is None:
            raise ValueError(
                "Not in a valid project directory — no history to read."
            )
        dirs: list = []
        ck_dir = resolve_write_path(self.ck_path) \
            if is_dev_mode() else self.ck_path
        if ck_dir.is_dir():
            dirs.append(ck_dir)
        # Real .ck/ keeps archives written outside the dev session.
        if is_dev_mode() and self.ck_path != ck_dir \
                and self.ck_path.is_dir():
            dirs.append(self.ck_path)
        paths = list_history_archives(dirs[0]) if dirs else []
        for extra in dirs[1:]:
            paths.extend(list_history_archives(extra))
        text, warnings = concat_archives(paths)
        parts: list = []
        if warnings:
            parts.append("\n".join(warnings))
        if text:
            parts.append(text)
        live = self.history_file
        if is_dev_mode() and live is not None:
            mirrored = resolve_write_path(live)
            if mirrored.exists():
                live = mirrored
        if live is not None and live.exists():
            live_text = live.read_text(encoding="utf-8")
            if live_text.strip():
                parts.append(live_text)
        if not parts:
            return "[i] No history entries found."
        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    # COMMAND: update (git pull --ff-only)                                #
    # ------------------------------------------------------------------ #

    @property
    def install_dir(self) -> Path:
        """Directory containing the ``cklib`` package — the one to ``git pull``."""
        return Path(__file__).resolve().parent.parent

    def update(self, *, remote: str = "origin",
               branch: str = DEFAULT_REPO_BRANCH,
               timeout: float = 30.0) -> "UpdateResult":
        """Self-update via ``git fetch`` + ``git pull --ff-only``.

        Returns an :class:`UpdateResult` describing what happened.
        Never raises for routine update outcomes; only for programmer
        errors (invalid remote/branch).

        DEV MODE: always aborts — a sandboxed session must never
        fetch from remotes or mutate the installation work tree.
        """
        if is_dev_mode():
            return UpdateResult(
                ok=False,
                message="Dev mode: self-update disabled in sandbox "
                        "mode (no remote fetch, no work-tree pull).",
                action="abort",
            )
        repo_dir = self.install_dir
        if not gith.is_git_repo(repo_dir):
            return UpdateResult(
                ok=False,
                message="Error: ContextKeeper was not installed via Git. "
                        "Updates require a git clone setup.",
                action="abort",
            )

        if gith.is_dirty(repo_dir):
            return UpdateResult(
                ok=False,
                message="Local changes detected. Please stash or commit "
                        "your changes before updating.",
                action="abort",
            )

        if not gith.fetch(remote, branch, path=repo_dir, timeout=timeout):
            return UpdateResult(
                ok=False,
                message=f"Failed to fetch {remote}/{branch}.",
                action="fetch-failed",
            )

        ok, msg = gith.pull_ff_only(remote, branch, path=repo_dir,
                                    timeout=timeout)
        if not ok:
            return UpdateResult(
                ok=False,
                message=msg or f"git pull --ff-only {remote}/{branch} failed.",
                action="pull-failed",
            )

        # Update the global state with the latest execution timestamp.
        try:
            _write_global_state_timestamp("last_update_check")
            _write_global_state_timestamp("last_update_success")
        except OSError:
            pass

        return UpdateResult(
            ok=True,
            message=msg or f"Updated to {remote}/{branch}.",
            action="updated",
        )

    # ------------------------------------------------------------------ #
    # Non-blocking daily update notifier                                   #
    # ------------------------------------------------------------------ #

    def maybe_notify_update(self, *, now: Optional[datetime] = None,
                            fetch_fn=None,
                            stderr=None) -> bool:
        """Print a one-line notice to ``stderr`` when an update is
        available, at most once every ``UPDATE_CHECK_INTERVAL_HOURS``.

        Strict guard clauses (each aborts instantly — no subprocess,
        no network, no state mutation):

        - ``CK_SANDBOX=1`` (dev/sandbox mode);
        - ``CK_DISABLE_UPDATE_CHECK=1`` (explicit opt-out);
        - ``install_dir`` is not a Git repository root (no ``.git``);
        - the last check is younger than the 24h throttle window.

        - Reads ``~/.config/ck/state.json`` to find the previous
          check timestamp.
        - If the interval has elapsed, performs a fast non-blocking
          ``ls-remote`` to find ``origin/main``'s tip.
        - Always refreshes ``last_update_check`` regardless of
          network outcome.
        - Returns True if a notice was emitted.

        The function never raises; failures are swallowed and the
        timestamp is still updated. ``stderr`` defaults to the
        current ``sys.stderr`` at call time (lazy).
        """
        # --- Early mandatory guards (must precede ALL network logic).
        if os.environ.get("CK_SANDBOX") == "1":
            return False
        if os.environ.get("CK_DISABLE_UPDATE_CHECK") == "1":
            return False
        try:
            if not (self.install_dir / ".git").exists():
                return False
        except OSError:
            return False

        if now is None:
            now = datetime.now(timezone.utc)
        if stderr is None:
            stderr = sys.stderr
        state = _read_global_state()
        last_iso = state.get("last_update_check", "")
        due = True
        if last_iso:
            try:
                last_dt = datetime.fromisoformat(last_iso)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                due = (now - last_dt) > timedelta(
                    hours=UPDATE_CHECK_INTERVAL_HOURS
                )
            except (ValueError, TypeError):
                due = True

        if not due:
            return False

        # Refresh timestamp regardless of network outcome.
        _write_global_state_timestamp("last_update_check", now=now)

        # Lazy import of fetch_fn to keep tests easy.
        check = fetch_fn or _default_remote_head_check
        try:
            result = check(self.install_dir)
        except Exception:
            return False

        if result is None:
            return False
        local_sha, remote_sha = result
        if local_sha and remote_sha and local_sha != remote_sha:
            try:
                print(
                    "Notice: A new version of ck is available. "
                    "Run 'ck update' to upgrade.",
                    file=stderr,
                )
            except OSError:
                pass
            return True
        return False

    def info(self) -> str:
        """Diagnostic information about the installation."""
        lines: list[str] = []
        lines.append(f"ck version: {VERSION}")
        lines.append(f"install_dir: {self.install_dir}")
        in_repo = gith.is_git_repo(self.install_dir)
        lines.append(f"in_git_repo: {in_repo}")
        if in_repo:
            br = gith.current_branch(self.install_dir) or "?"
            sha = gith.head_sha(self.install_dir) or "?"
            lines.append(f"branch: {br}")
            lines.append(f"head: {sha[:12]}")
        lines.append(f"user_install_path: {USER_INSTALL_PATH}")
        lines.append(
            "install_mode: physical copy (never a symlink; refreshed "
            "only by `ck install` / `ck update`)"
        )
        lines.append(f"package_snapshot: {SNAPSHOT_INSTALL_DIR / 'cklib'}")
        return "\n".join(lines)


# ---------------------------------------------------------------------- #
# Update notifier helpers
# ---------------------------------------------------------------------- #


def _read_global_state() -> dict:
    """Best-effort read of ``~/.config/ck/state.json``."""
    try:
        if GLOBAL_STATE_FILE.exists():
            return json.loads(GLOBAL_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _write_global_state_timestamp(key: str,
                                  *, now: Optional[datetime] = None) -> None:
    """Write a single timestamp key to ``~/.config/ck/state.json``.

    Atomic-replace; creates the parent directory if needed. Data is
    fsync'ed before rename; existing permissions are preserved; a
    symlinked state file is resolved so the link survives.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    real = GLOBAL_STATE_FILE
    try:
        if GLOBAL_STATE_FILE.is_symlink():
            real = GLOBAL_STATE_FILE.resolve()
    except OSError:
        real = GLOBAL_STATE_FILE
    # DEV MODE: reads seed from the original; the write itself is
    # redirected to the sandboxed state copy.
    read_from = real
    if is_dev_mode():
        real = resolve_write_path(real)
    real.parent.mkdir(parents=True, exist_ok=True)
    state: dict = {}
    if read_from.exists():
        try:
            state = json.loads(read_from.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    if not isinstance(state, dict):
        state = {}
    state[key] = now.isoformat()
    # Atomic write
    fd, tmp = tempfile.mkstemp(
        dir=str(real.parent), prefix=".ck-state-", suffix=".tmp"
    )
    try:
        try:
            mode = os.stat(real).st_mode & 0o777
        except OSError:
            mode = 0o644
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _default_remote_head_check(repo_dir: Path
                               ) -> Optional[Tuple[str, str]]:
    """Read-only check: compare local HEAD with its tracked upstream.

    Resolves the CURRENT branch's upstream (``@{upstream}``) rather
    than assuming ``origin/main`` — fork/feature-branch installs no
    longer produce perpetual false "update available" notices. Falls
    back to ``origin/<current-branch>`` when no upstream is tracked.

    The network call is capped at a hard sub-second deadline
    (:data:`gith.NETWORK_CHECK_TIMEOUT`) so a slow network cannot
    stall interactive commands.
    """
    local = gith.head_sha(repo_dir)
    if not local:
        return None
    branch = gith.current_branch(repo_dir)
    if not branch or branch.startswith("-"):
        return None
    if not shutil.which("git"):
        return None

    # Prefer the branch's configured upstream (e.g. fork/feature).
    remote_ref: Optional[str] = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--symbolic-full-name",
             f"{branch}@{{upstream}}"],
            capture_output=True, text=True,
            timeout=gith.NETWORK_CHECK_TIMEOUT, check=False,
            cwd=str(repo_dir),
        )
        if result.returncode == 0:
            remote_ref = result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        remote_ref = None
    if not remote_ref or remote_ref.startswith("-"):
        remote_ref = f"refs/remotes/origin/{branch}"

    # Map the tracking ref to (remote, branch) for ls-remote. The
    # tracking ref looks like refs/remotes/<remote>/<branch>.
    remote_name, ls_branch = "origin", branch
    if remote_ref.startswith("refs/remotes/"):
        parts = remote_ref[len("refs/remotes/"):].split("/", 1)
        if len(parts) == 2 and parts[0] and not parts[0].startswith("-") \
                and parts[1] and not parts[1].startswith("-"):
            remote_name, ls_branch = parts[0], parts[1]

    remote = gith.ls_remote(remote_name, ls_branch, repo_dir)
    if not remote:
        return None
    return (local, remote)


@dataclass
class UpdateResult:
    """Outcome of a :meth:`ContextKeeper.update` invocation."""

    ok: bool
    message: str
    action: str  # "abort" | "fetch-failed" | "pull-failed" | "updated"

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------- #
# AST placement helpers (no string juggling)
# ---------------------------------------------------------------------- #

def _section_for_new_task(tl: TaskList) -> str:
    """Pick a sensible section for a brand-new OPEN task.

    Strategy: hang the new task under the most recent section that
    is not the ``Completed`` section.
    """
    for sec in reversed(tl.sections):
        if sec.title.lower() == "completed":
            continue
        return sec.title
    return ""


def _sprint_insert_line(tl: TaskList) -> Optional[int]:
    """1-based source line where ``ck add`` should insert a new task.

    Resolution order (mirrors the spec for clean insertion):

    1. End of the ``## Current Sprint`` section (after its last
       content line, before the next header).
    2. Directly before the ``## Completed`` header.
    3. ``None`` → the renderer's default: before ``## Completed``
       or at EOF.
    """
    src_lines = tl.source_text.splitlines()
    # Fast path: no document → let the renderer default handle it.
    if not src_lines:
        return None
    sprint_end = _find_section_end(src_lines, "Current Sprint")
    if sprint_end is not None:
        return sprint_end + 1  # 0-based → 1-based
    completed = _find_completed_line(src_lines)
    if completed is not None:
        return completed + 1  # insert at (before) the Completed header
    return None


# ---------------------------------------------------------------------- #
# Presentation helpers
# ---------------------------------------------------------------------- #

def _status_triad(tl: TaskList, note_data: Optional[dict] = None
                  ) -> tuple:
    """Resolve the compact PREV / FOCUS / NEXT triad used by the
    VERBOSE dashboard blocks (``ck dashboard -v``).

    Returns ``(prev, focus, next, note_id, note_text)``:

    - PREV: nearest completed ``[x]`` task prior to the FOCUS task
      (last done overall when no focus is set).
    - FOCUS: the explicitly focused ``[>]`` task — NO fallback to
      the first open task (the hint line directs the user to set a
      focus instead).
    - NEXT: first open ``[ ]`` task after the FOCUS task (first
      open overall when no focus is set).
    - ``note_id`` / ``note_text``: the stored active-task process
      note, attributed only while its task still exists in the plan.
      A noted OPEN task that is NOT the focus is a paused/unfocused
      task — ``note_id`` points at it so the verbose block renders a
      dedicated "Unfocused / Paused Context" entry instead of
      pinning the note under Focus/Next.
    """
    focus = tl.focused[0] if tl.focused else None

    prev = None
    if focus is not None:
        for t in tl.tasks:
            if t.id == focus.id:
                break
            if t.status == TaskStatus.DONE:
                prev = t
    elif tl.done:
        prev = tl.done[-1]

    nxt = None
    start = 0
    if focus is not None:
        for i, t in enumerate(tl.tasks):
            if t.id == focus.id:
                start = i + 1
                break
    for t in tl.tasks[start:]:
        if t.status == TaskStatus.OPEN:
            nxt = t
            break

    note_id: Optional[int] = None
    note_text = ""
    if isinstance(note_data, dict):
        nid = note_data.get("id")
        ntext = note_data.get("note")
        if isinstance(nid, int) \
                and not isinstance(nid, bool) \
                and isinstance(ntext, str) and ntext \
                and tl.by_id(nid) is not None:
            note_id, note_text = nid, ntext

    return prev, focus, nxt, note_id, note_text


# Maximum number of tasks listed BY NAME per WORK CONTEXT section
# (``<< Done`` and ``[!] Skipped``); anything beyond folds into the
# header's "(N tasks)" count plus a "... (+N more ...)" overflow line
# so a long tail of gaps can never flood the block. The displayed
# tasks are the ones CLOSEST to the active focus.
_CONTEXT_SHOWN_LIMIT = 2


@dataclass
class _StatusContext:
    """Resolved 4-element WORK CONTEXT view state for ``ck st``.

    - ``done``: every completed task before the progress point,
      ordered so the ones closest to the focus come last — the
      renderer shows the last 2 (display limit) and folds the rest
      into a count header + overflow line.
    - ``skipped``: open tasks left behind — with an explicit focus,
      ONLY the opens positioned BEFORE it (passed over); without a
      focus, the stranded opens after the last completed task
      (classic gaps) also qualify. The current and upcoming tasks
      are always excluded. Listed by name in the rendering.
    - ``current`` / ``is_focus``: the explicitly focused task, or
      the first open task auto-resolved as the Next candidate
      (display only — the plan state is never mutated).
    - ``upcoming``: the first open task after ``current``.
    """

    done: list
    skipped: list
    current: Optional[Task]
    is_focus: bool
    upcoming: Optional[Task]


def _status_context(tl: TaskList) -> _StatusContext:
    """Resolve the 4-element WORK CONTEXT (``ck st``).

    The progress point is the explicit focus when set; otherwise the
    FIRST open task is auto-resolved as the Next candidate (without
    mutating the plan). Done context is every completion before the
    point (focus case) or overall (candidate case) — the renderer
    applies the 2-task display limit. Skipped tasks are the
    passed-over opens before an explicit focus, or the stranded
    opens (auto-candidate case), minus the current and upcoming
    tasks.
    """
    focus = tl.focused[0] if tl.focused else None
    opens = tl.open

    if focus is not None:
        current, is_focus = focus, True
    elif opens:
        current, is_focus = opens[0], False
    else:
        current, is_focus = None, False

    pos = {t.id: i for i, t in enumerate(tl.tasks)}
    last_done_pos = -1
    for i, t in enumerate(tl.tasks):
        if t.status == TaskStatus.DONE:
            last_done_pos = i

    if is_focus and current is not None:
        done = [
            t for i, t in enumerate(tl.tasks)
            if t.status == TaskStatus.DONE and i < pos[current.id]
        ]
    else:
        done = list(tl.done)

    upcoming = None
    if current is not None:
        for t in tl.tasks[pos[current.id] + 1:]:
            if t.status == TaskStatus.OPEN:
                upcoming = t
                break

    skipped: list = []
    if current is not None:
        cur_pos = pos[current.id]
        excluded = {current.id}
        if upcoming is not None:
            excluded.add(upcoming.id)
        # With an explicit FOCUS, only opens POSITIONED BEFORE it were
        # passed over — every open after the focus stays Pending /
        # Upcoming. The stranded-after-last-done heuristic is reserved
        # for the no-focus view (auto-resolved Next candidate), where
        # it surfaces classic plan gaps by name.
        if is_focus:
            skipped = [
                t for i, t in enumerate(tl.tasks)
                if t.status == TaskStatus.OPEN
                and t.id not in excluded
                and i < cur_pos
            ]
        else:
            skipped = [
                t for i, t in enumerate(tl.tasks)
                if t.status == TaskStatus.OPEN
                and t.id not in excluded
                and (i < cur_pos
                     or (last_done_pos != -1 and i > last_done_pos))
            ]

    return _StatusContext(
        done=done,
        skipped=skipped,
        current=current,
        is_focus=is_focus,
        upcoming=upcoming,
    )


def _render_local_status(ck: ContextKeeper, tl: TaskList,
                         *, palette: Optional["ui.Palette"] = None,
                         source: str = "local") -> str:
    """Render the compact single-project status block.

    Structure (spec-exact in plain-text mode, PURE ASCII — no emoji
    or box-drawing glyphs, so no terminal font fallback is ever
    needed). The WORK CONTEXT section is a strict 4-element vertical
    list: every active section renders a header line with its task
    items indented on new lines below it, one ``- [ID] Title [st]``
    per line::

        =============================================================
         > <project_name> [v<version>]
         [%] Progress: <done>/<total> tasks done (<pct>%)
         -> CURRENT FOCUS: [#<id>] <title>   (explicit focus only)
            * Note: <process note>            (only when set)

         -> WORK CONTEXT:
            << Done (N tasks):               last 2, closest to focus
               - [<id>] <title> [x]
               - [<id>] <title> [x]
               ... (+<N> more done)
            [!] Skipped (N tasks):                by name, capped
               - [<id>] <title> [ ]
               - [<id>] <title> [ ]
               ... (+<N> more skipped)
            [>] Focus:                          (focus set via ck start)
               - [<id>] <title>
            [>] Next:                        (auto-candidate, no focus;
               - [<id>] <title> [ ]              never mutates)
            >> Upcoming:                          (follows Focus/Next)
               - [<id>] <title> [ ]
            * Note: <process note>                    (only when set)
            Unfocused / Paused Context:  (noted open task that lost focus)
               - [<id>] <title>
                 * Note: <note text>
        =============================================================

    DISPLAY LIMITS: ``<< Done`` and ``[!] Skipped`` show at most
    ``_CONTEXT_SHOWN_LIMIT`` (2) tasks each — the ones closest to the
    active focus. When a section holds more than 2 tasks its header
    gains a ``(N tasks)`` count and the item list ends with an
    overflow line: ``... (+N more done)`` / ``... (+N more skipped)``.
    ``[>] Focus`` / ``[>] Next`` / ``>> Upcoming`` carry a single item
    and never overflow. Empty sections keep the inline hint form
    (``<< Done: (none completed)``) on the header line.

    Skipped tasks are the opens left behind — passed over before the
    focus/next progress point, or stranded after the last completed
    task — so gaps surface BY NAME instead of the abstract
    ``(gaps: X-Y)`` range (which still summarizes the progress
    line).

    UNFOCUSED / PAUSED CONTEXT: a still-open task that lost focus
    (via ``ck start <NEW_ID>`` or a focus reset) while carrying the
    process note is rendered in a dedicated block — its note travels
    with it. The single active note is NOT duplicated under
    Focus/Next when the noted task is paused.

    ``source`` mirrors :class:`ProjectContext` resolution: a
    ``"global"`` fallback appends an accent ``<- <path>`` hint to the
    header so the user sees WHICH registered project was loaded from
    an unrelated directory.

    COLOR / CONTRAST: when the output stream supports ANSI (TTY, or
    FORCE_COLOR/CLICOLOR_FORCE; never under NO_COLOR), the palette
    from :mod:`cklib.ui` highlights the hierarchy — bold cyan
    project name, bold section header, green done line, yellow
    skipped line, bold yellow focus/next line, bold cyan note.
    Secondary text (hints, progress labels, ratios at 0%), structural
    borders and accent metadata INHERIT the terminal's native text
    color by default — no DIM, no hardcoded dark gray — and remain
    restyleable via the ``colors`` mapping in the project's
    ``.ck.json`` (see :meth:`ContextKeeper.color_overrides`). The
    plain-text fallback is byte-identical to the colorless rendering
    (a disabled palette is the identity transform). ``palette``
    overrides auto-detection (tests pin a deterministic palette).
    """
    if palette is not None:
        p = palette
    else:
        p = ui.get_palette(colors=ck.color_overrides())
    bar = "=" * 61
    lines: list[str] = []
    lines.append(p.border(bar))
    header = (
        f" > {p.bold_cyan(ck.root.name)} "
        f"{p.accent(f'[v{VERSION}]')}"
    )
    if source == "global":
        header += f" {p.accent(f'<- {ck.root}')}"
    lines.append(header)

    done_count = len(tl.done)
    total = tl.total
    pct = tl.completion_pct
    pct_str = f"({pct}%)"
    progress = (
        f" {p.muted('[%] Progress:')} {p.bold(f'{done_count}/{total}')} "
        f"tasks done {p.green(pct_str) if done_count else p.muted(pct_str)}"
    )
    gap_ids = tl.gap_ids()
    if gap_ids:
        progress += f" {p.muted(f'(gaps: {_collapse_ids(gap_ids)})')}"
    lines.append(progress)

    # 0) Top-level focus visibility: the active task is duplicated at
    #    the very top (immediately below header/progress) so the user
    #    never has to scan the list blocks for it. Explicit focus
    #    only — the auto-resolved Next candidate is NOT advertised
    #    as a focus (the [>] Next section keeps that nuance).
    top_focus = tl.focused[0] if tl.focused else None
    if top_focus is not None:
        top_note = ""
        note_data = ck.get_note()
        if note_data is not None and note_data.get("id") == top_focus.id \
                and note_data.get("note"):
            top_note = note_data["note"]
        else:
            for entry in ck._paused_tasks():
                if entry["id"] == top_focus.id and entry["note"]:
                    top_note = entry["note"]
                    break
        lines.append("")
        lines.append(
            " -> CURRENT FOCUS: "
            + p.bold_yellow(f"[#{top_focus.id}] {top_focus.title}"))
        if top_note:
            lines.append(p.bold_cyan(f"    * Note: {top_note}"))

    lines.append("")
    lines.append(p.bold(" -> WORK CONTEXT:"))

    ctx = _status_context(tl)

    # 1) << Done: the completions immediately before the progress
    #    point — last 2 shown, the rest folded into count + overflow.
    if ctx.done:
        shown = ctx.done[-_CONTEXT_SHOWN_LIMIT:]
        hidden = len(ctx.done) - len(shown)
        if hidden > 0:
            lines.append(
                p.green(f"    << Done ({len(ctx.done)} tasks):"))
        else:
            lines.append(p.green("    << Done:"))
        for t in shown:
            lines.append(p.green(f"       - [{t.id}] {t.title} [x]"))
        if hidden > 0:
            lines.append(p.green(f"       ... (+{hidden} more done)"))
    else:
        lines.append(p.muted("    << Done: (none completed)"))

    # Active note + paused-task ledger: the ``paused_tasks``
    # registry is the single source of truth for the "Unfocused /
    # Paused Context" block — EVERY open task that previously held
    # focus appears there (with its bound note, if any), so unnoted
    # or previously paused tasks can never vanish into the generic
    # skipped list. (A note in the legacy ``active_task`` slot
    # pointing at a non-focused open task — e.g. state written by an
    # older version — renders as a paused entry too.)
    note_data = ck.get_note()
    note_id = note_data["id"] if note_data is not None else None
    note_text = note_data["note"] if note_data is not None else ""
    paused_display: list = []  # (id, title, note) triples
    paused_ids: set = set()
    for entry in ck._paused_tasks():
        p_task = tl.by_id(entry["id"])
        if p_task is None or p_task.status != TaskStatus.OPEN:
            continue
        if ctx.current is not None and p_task.id == ctx.current.id:
            continue
        if p_task.id in paused_ids:
            continue
        paused_ids.add(p_task.id)
        paused_display.append((p_task.id, p_task.title, entry["note"]))
    # Legacy bridge: an active_task note pointing at a non-focused
    # open task (state written by an older version) renders as a
    # paused entry too.
    noted_task = tl.by_id(note_id) if note_id is not None else None
    if (noted_task is not None
            and noted_task.status == TaskStatus.OPEN
            and noted_task.id not in paused_ids
            and (ctx.current is None or noted_task.id != ctx.current.id)):
        paused_ids.add(noted_task.id)
        paused_display.append(
            (noted_task.id, noted_task.title, note_text))
    # The note bound to the DISPLAYED Focus/Next task renders inline
    # under it — whether it sits in the active slot or in the paused
    # registry (e.g. right after a re-focus or a focus reset).
    inline_note = ""
    if ctx.current is not None:
        if note_id == ctx.current.id and note_text:
            inline_note = note_text
        else:
            for entry in ck._paused_tasks():
                if entry["id"] == ctx.current.id and entry["note"]:
                    inline_note = entry["note"]
                    break

    # 2) [!] Skipped: passed-over / stranded opens, BY NAME (capped,
    #    closest to the focus first). Tasks rendered in the
    #    Unfocused / Paused Context block are excluded here so a
    #    paused task never doubles up in the generic skipped list.
    skipped_visible = [
        t for t in ctx.skipped if t.id not in paused_ids
    ]
    if skipped_visible:
        shown = skipped_visible[:_CONTEXT_SHOWN_LIMIT]
        hidden = len(skipped_visible) - len(shown)
        if hidden > 0:
            lines.append(
                p.yellow(
                    f"    [!] Skipped ({len(skipped_visible)} tasks):"))
        else:
            lines.append(p.yellow("    [!] Skipped:"))
        for t in shown:
            lines.append(p.yellow(f"       - [{t.id}] {t.title} [ ]"))
        if hidden > 0:
            lines.append(
                p.yellow(f"       ... (+{hidden} more skipped)"))
    else:
        lines.append(p.muted("    [!] Skipped: (none)"))

    # 3) [>] Focus (explicit) or Next (auto-resolved candidate —
    #    display only, the plan is never mutated).
    if ctx.is_focus and ctx.current is not None:
        lines.append("    [>] Focus:")
        lines.append(
            f"       - "
            f"{p.bold_yellow(f'[{ctx.current.id}] {ctx.current.title}')}"
        )
        if inline_note:
            lines.append(p.bold_cyan(f"       * Note: {inline_note}"))
    elif ctx.current is not None:
        t = ctx.current
        lines.append("    [>] Next:")
        lines.append(
            f"       - {p.bold_yellow(f'[{t.id}] {t.title}')} [ ]"
        )
        if inline_note:
            lines.append(p.bold_cyan(f"       * Note: {inline_note}"))
    else:
        lines.append(p.muted("    [>] Next: (no open tasks)"))

    # 4) >> Upcoming: the next pending task after Focus/Next.
    if ctx.upcoming is not None:
        lines.append(p.muted("    >> Upcoming:"))
        lines.append(
            p.muted(
                f"       - [{ctx.upcoming.id}] "
                f"{ctx.upcoming.title} [ ]"
            )
        )
    else:
        lines.append(p.muted("    >> Upcoming: (none)"))

    # 5) Unfocused / Paused Context: every still-open task that
    #    previously held focus (the paused_tasks ledger), each with
    #    its bound note when one was attached — unnoted pauses stay
    #    visible too instead of sinking into the skipped list.
    if paused_display:
        lines.append(p.bold_yellow("    Unfocused / Paused Context:"))
        for pid, ptitle, pnote in paused_display:
            lines.append(f"       - [{pid}] {ptitle}")
            if pnote:
                lines.append(p.bold_cyan(f"         * Note: {pnote}"))

    lines.append(p.border(bar))
    return "\n".join(lines)


def _render_tasks_listing(tl: TaskList) -> str:
    """Render the local task list for ``ck list`` (STDOUT output).

    Format (grep/cut-friendly, no editor involved):

    ::

        # <project tasks>            (omitted when no sections exist)
        ## Current Sprint
        [ ] 1. first open task
        [>] 2. focused task
        [x] 3. done task
        ## Completed
        [x] 4. old done task

    Status markers mirror PLAN.md syntax: ``[ ]`` open, ``[>]``
    focused, ``[x]`` done. Empty plans render a single hint line.
    """
    if not tl.tasks:
        return "No tasks. Add one with `ck add <text>`."

    lines: list[str] = []
    current_section = object()  # sentinel: "no section yet"
    for t in tl.tasks:
        if t.section and t.section != current_section:
            lines.append(f"## {t.section}")
            current_section = t.section
        marker = t.status.canonical_marker
        lines.append(f"[{marker}] {t.id}. {t.title}")
    return "\n".join(lines)


def _render_notes_listing(ck: ContextKeeper, tl: TaskList) -> str:
    """Render all active process notes (``ck notes``).

    Two structured sections: the focused task's note (``[>] Active
    Focus:``) and every paused task's bound note (``[!] Unfocused /
    Paused Context:``). A paused entry WITHOUT a note is still
    listed (with a muted ``(no note)`` marker) so the paused context
    stays fully visible; the section itself is omitted only when no
    paused tasks exist. No notes anywhere -> the single ``[i] No
    active process notes found.`` line.
    """
    p = ui.get_palette(colors=ck.color_overrides())
    note_data = ck.get_note()
    note_id = note_data["id"] if note_data is not None else None
    note_text = note_data["note"] if note_data is not None else ""

    focus = tl.focused[0] if tl.focused else None
    active_note = ""
    if focus is not None:
        if note_id == focus.id and note_text:
            active_note = note_text
        else:
            for entry in ck._paused_tasks():
                if entry["id"] == focus.id and entry["note"]:
                    active_note = entry["note"]
                    break

    paused_entries: list = []
    for entry in ck._paused_tasks():
        p_task = tl.by_id(entry["id"])
        if p_task is None or p_task.status != TaskStatus.OPEN:
            continue
        if focus is not None and p_task.id == focus.id:
            continue
        paused_entries.append((p_task.id, p_task.title, entry["note"]))
    # Legacy bridge: an active_task note pointing at a non-focused
    # open task (state written by an older version) renders as a
    # paused entry here too.
    if note_id is not None and note_text \
            and (focus is None or note_id != focus.id):
        noted = tl.by_id(note_id)
        if (noted is not None and noted.status == TaskStatus.OPEN
                and all(pid != noted.id for pid, _, _ in paused_entries)):
            paused_entries.append((noted.id, noted.title, note_text))

    has_any = bool(active_note) or any(
        note for _, _, note in paused_entries)
    if not has_any and focus is None and not paused_entries:
        return "[i] No active process notes found."
    if not has_any:
        # Focus/paused tasks exist but carry no notes at all.
        return "[i] No active process notes found."

    out: list[str] = []
    if focus is not None:
        out.append(p.bold_yellow("[>] Active Focus:"))
        out.append(f"   - [{focus.id}] {focus.title}")
        if active_note:
            out.append(p.bold_cyan(f"     * Note: {active_note}"))
    if paused_entries:
        out.append(p.bold_yellow("[!] Unfocused / Paused Context:"))
        for pid, ptitle, pnote in paused_entries:
            out.append(f"   - [{pid}] {ptitle}")
            if pnote:
                out.append(p.bold_cyan(f"     * Note: {pnote}"))
            else:
                out.append(p.muted("     (no note)"))
    return "\n".join(out)


def _collapse_ids(ids: list) -> str:
    """Collapse consecutive ID runs into ranges.

    ``[3, 4, 5, 6, 7, 8]`` → ``"3-8"``; ``[3, 4, 8]`` → ``"3-4, 8"``;
    single IDs stay as-is. Input is deduplicated and sorted
    defensively.
    """
    ordered = sorted(set(ids))
    if not ordered:
        return ""
    runs: list = []
    start = prev = ordered[0]
    for i in ordered[1:]:
        if i == prev + 1:
            prev = i
        else:
            runs.append((start, prev))
            start = prev = i
    runs.append((start, prev))
    return ", ".join(
        str(a) if a == b else f"{a}-{b}" for a, b in runs
    )


def _safe_parse_plan(plan_path: Path) -> "TaskList | None":
    """Defensive wrapper around ``parse_plan_file`` for the dashboard.

    Returns ``None`` on any I/O / parse error so the dashboard never
    crashes for a single corrupt project.
    """
    try:
        if not plan_path.exists():
            return None
        return parse_plan_file(plan_path)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _truncate(text: str, width: int) -> str:
    """Truncate ``text`` to ``width`` columns with an ellipsis."""
    if width <= 1:
        return text[:width]
    if len(text) <= width:
        return text
    return "..." + text[-(width - 3):]


def _truncate_ellipsis(text: str, width: int) -> str:
    """Fit ``text`` into ``width`` columns with a middle ASCII ellipsis.

    Used by the dashboard table: long project names and focus-task
    titles keep both their start and their end (the informative
    parts) — e.g. ``[3] [>] implement the very long...ring module``
    → the ``[<id>] [>]`` prefix and the title tail survive.
    Never returns a string longer than ``width``; the ellipsis is
    plain ``...`` (three ASCII dots), not a Unicode glyph.
    """
    if len(text) <= width:
        return text
    if width <= 3:
        return "." * width
    keep_start = (width - 3) // 2
    keep_end = width - 3 - keep_start
    return f"{text[:keep_start]}...{text[len(text) - keep_end:]}"


def _relative_time(iso: str, *, now: Optional[datetime] = None) -> str:
    """Format ``iso`` as a compact human-friendly relative timestamp.

    Examples: "just now", "42s ago", "5m ago", "3h ago", "yesterday",
    "2d ago", "2025-12-04 11:30" (for > 30 days). Returns "unknown"
    if the input is unparseable.
    """
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return "unknown"
    if dt.tzinfo is None:
        # Treat naive as UTC (registry convention).
        dt = dt.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    delta = ref - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        # Future timestamp (clock skew) — display absolute.
        return dt.strftime("%Y-%m-%d %H:%M")
    if secs < 5:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    minutes = secs // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days}d ago"
    return dt.strftime("%Y-%m-%d %H:%M")


def _render_dashboard(ck: Optional[ContextKeeper], *, list_projects,
                      parse_plan_file, verbose: bool = False) -> str:
    """Render the global dashboard.

    Default (compact table) — columns strictly by priority:

    1. Project      — name, ``*`` appended for the cwd project
    2. Focus Task   — ``[<id>] [>] <text>`` (ellipsis-truncated) or
                      ``(no focus)``
    3. Progress     — compact ``<done>/<total> (<pct>%)``
    4. Last Active  — compact relative time (``2m ago``, ``yesterday``)

    Verbose (``-v`` / ``--verbose``) — one block per project with the
    full PREV/FOCUS/NEXT triad context.

    Missing folders render a ``[MISSING]`` project tag; unparseable plans
    render ``corrupt`` — neither ever crashes the whole table.
    """
    entries = list_projects()
    if not entries:
        return (
            "Global Dashboard\n"
            "[i] No registered projects. Run `ck register` "
            "in a project directory to begin.\n"
        )

    cwd = None
    if ck is not None and ck.root is not None:
        try:
            cwd = str(Path(ck.root).resolve())
        except (OSError, ValueError):
            cwd = None

    # ---- collect per-entry state -------------------------------------
    states: list[dict] = []
    for entry in entries:
        path = Path(entry.path)
        on_disk = path.exists()
        plan_path = path / CK_DIR_NAME / PLAN_FILENAME
        tl_local = parse_plan_file(plan_path) if on_disk else None

        state = {
            "entry": entry,
            "is_cwd": bool(cwd and entry.path == cwd),
            "tl": tl_local,
            "condition": "ok",
        }
        if not on_disk:
            state["condition"] = "missing"
        elif tl_local is None:
            state["condition"] = "corrupt"
        states.append(state)

    rendered = (
        _render_dashboard_verbose(states)
        if verbose else _render_dashboard_table(states)
    )
    missing_count = sum(
        1 for state in states if state["condition"] == "missing"
    )
    if missing_count:
        rendered += (
            f"\n\n[i] Found {missing_count} missing project(s). "
            "Run 'ck prune' to cleanup."
        )
    return rendered


def read_paused_tasks(project_root: Path) -> list[dict]:
    """Read the ``paused_tasks`` registry for a project directory.

    Standalone reader for cross-project rendering (``ck dashboard
    -v``): loads ``<project>/.ck/state.json`` directly and returns
    the valid ``paused_tasks`` entries (``{"id": int, "title":
    str, "note": str}``) in stored order, tolerating absent or
    corrupt state. Mirrors :meth:`ContextKeeper._paused_tasks`
    structural validation minus the plan-membership check (the
    caller resolves tasks against its own parsed plan).
    """
    state_file = Path(project_root) / CK_DIR_NAME / STATE_FILENAME
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("paused_tasks")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        tid = item.get("id")
        title = item.get("title")
        note = item.get("note")
        if not isinstance(tid, int) or isinstance(tid, bool) \
                or tid in seen:
            continue
        if not isinstance(title, str) or not isinstance(note, str):
            continue
        seen.add(tid)
        out.append({"id": tid, "title": title, "note": note})
    return out


def read_active_task_note(project_root: Path) -> Optional[dict]:
    """Read the stored active-task note dict for a project directory.

    Standalone reader for cross-project rendering (``ck dashboard
    -v``): loads ``<project>/.ck/state.json`` directly and returns
    the structurally valid ``active_task`` entry (non-empty string
    ``note``, int ``id``), or None when absent, corrupt, or
    hand-edited. Mirrors :meth:`ContextKeeper.get_note` validation.
    """
    state_file = Path(project_root) / CK_DIR_NAME / STATE_FILENAME
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    active = data.get("active_task")
    if not isinstance(active, dict):
        return None
    note = active.get("note")
    tid = active.get("id")
    if isinstance(note, str) and note and isinstance(tid, int) \
            and not isinstance(tid, bool):
        return active
    return None


# ---------------------------------------------------------------------- #
# Dashboard: compact table (default)
# ---------------------------------------------------------------------- #

_DASH_HEADERS = ("Project", "Focus Task", "Progress", "Last Active")
_DASH_KEYS = ("project", "focus", "progress", "last")

# Content caps applied BEFORE width computation: Focus Task truncates
# at ~90 chars (spec band 80-100; the ``[<id>] [>]`` prefix and the
# title tail stay readable), Project names at 40. Progress and Last
# Active are naturally short.
_FOCUS_CAP = 90
_PROJECT_CAP = 40


def _render_dashboard_table(states: list) -> str:
    """Compact priority table: Project | Focus Task | Progress | Last Active."""
    rows: list[dict] = []
    for s in states:
        entry = s["entry"]
        tl_local = s["tl"]

        project = entry.name + (" *" if s["is_cwd"] else "")
        if s["condition"] == "missing":
            project = f"[MISSING] {project}"
        last = _relative_time(entry.last_seen)

        if s["condition"] == "missing":
            focus, progress = "n/a", "missing"
        elif s["condition"] == "corrupt":
            focus, progress = "n/a", "corrupt"
        else:
            if tl_local.focused:
                t = tl_local.focused[0]
                focus = f"[{t.id}] [>] {t.title}"
            else:
                focus = "(no focus)"
            done_count = len(tl_local.done)
            progress = f"{done_count}/{tl_local.total} ({tl_local.completion_pct}%)"

        rows.append({
            "project": _truncate_ellipsis(project, _PROJECT_CAP),
            "focus": _truncate_ellipsis(focus, _FOCUS_CAP),
            "progress": progress,
            "last": last,
        })

    widths: dict[str, int] = {}
    for h, k in zip(_DASH_HEADERS, _DASH_KEYS):
        widths[k] = max(len(h), *(len(r[k]) for r in rows))

    def _hr() -> str:
        return "+" + "+".join("-" * (widths[k] + 2) for k in _DASH_KEYS) + "+"

    def _row(values: tuple) -> str:
        cells = [
            f" {v.ljust(widths[k])} "
            for v, k in zip(values, _DASH_KEYS)
        ]
        return "|" + "|".join(cells) + "|"

    out: list[str] = ["GLOBAL DASHBOARD"]
    out.append(_hr())
    out.append(_row(_DASH_HEADERS))
    out.append(_hr())
    for r in rows:
        out.append(_row((r["project"], r["focus"], r["progress"], r["last"])))
    out.append(_hr())
    return "\n".join(out)


# ---------------------------------------------------------------------- #
# Dashboard: verbose block view (-v / --verbose)
# ---------------------------------------------------------------------- #


def _render_dashboard_verbose(states: list) -> str:
    """One isolated block per project with the full triad context.

    Structure (spec-exact, pure ASCII)::

        MY PROJECTS (<count>)

         > <project_name> [<active_marker>]     ([*] cwd, [ ] other)
            @ <path>
            [%] Progress: <done>/<total> (<pct>%)
            -> Context:
               << [<id>] <prev_text> [x]
               [>] [<id>] <focus_text>
               >> [<id>] <next_text> [ ]

        =============================================================

    When every task in a project is done, the triad collapses to a
    single line: ``-> Context: (all tasks completed)``. Missing
    folders and unparseable plans render a ``[!]`` label instead of
    the progress/triad lines.
    """
    bar = "=" * 61
    out: list[str] = [f"MY PROJECTS ({len(states)})", ""]

    for s in states:
        entry = s["entry"]
        tl_local = s["tl"]
        marker = "*" if s["is_cwd"] else " "
        name = entry.name
        if s["condition"] == "missing":
            name = f"[MISSING] {name}"
        out.append(f" > {name} [{marker}]")
        out.append(f"    @ {entry.path}")

        if s["condition"] != "ok":
            label = "missing" if s["condition"] == "missing" else "corrupt"
            out.append(f"    [!] {label}")
        else:
            done_count = len(tl_local.done)
            out.append(
                f"    [%] Progress: {done_count}/{tl_local.total} "
                f"({tl_local.completion_pct}%)"
            )
            if tl_local.total > 0 and done_count == tl_local.total:
                out.append(
                    "    -> Context: (all tasks completed)"
                )
            else:
                out.append("    -> Context:")
                note_data = read_active_task_note(entry.path)
                prev, focus, nxt, note_id, note_text = _status_triad(
                    tl_local, note_data)
                # Paused ledger: every open task that previously held
                # focus, with its bound note. The Focus/Next anchor is
                # excluded — its note renders inline instead (a
                # noteless pause stays visible; the anchor's bound
                # note is looked up from the registry below).
                anchor = focus if focus is not None else nxt
                paused_map: dict = {}
                for p_entry in read_paused_tasks(entry.path):
                    p_task = tl_local.by_id(p_entry["id"])
                    if p_task is None or p_task.status != TaskStatus.OPEN:
                        continue
                    if anchor is not None and p_task.id == anchor.id:
                        continue
                    paused_map[p_task.id] = (
                        p_task.title, p_entry["note"])
                # Legacy bridge: an active_task note pointing at a
                # non-anchored open task (state written by an older
                # version) renders as a paused entry too.
                if (note_id is not None and note_text
                        and (anchor is None or note_id != anchor.id)):
                    noted = tl_local.by_id(note_id)
                    if (noted is not None
                            and noted.status == TaskStatus.OPEN
                            and noted.id not in paused_map):
                        paused_map[noted.id] = (noted.title, note_text)
                # The note bound to the anchor renders inline under
                # it — from the active slot or the paused registry.
                inline_note = ""
                if anchor is not None:
                    if note_id == anchor.id and note_text:
                        inline_note = note_text
                    else:
                        for p_entry in read_paused_tasks(entry.path):
                            if (p_entry["id"] == anchor.id
                                    and p_entry["note"]):
                                inline_note = p_entry["note"]
                                break
                if prev is not None:
                    out.append(f"       << [{prev.id}] {prev.title} [x]")
                else:
                    out.append("       << (none completed)")
                if focus is not None:
                    out.append(f"       [>] [{focus.id}] {focus.title}")
                    if inline_note:
                        out.append(f"          * Note: {inline_note}")
                else:
                    out.append("       [>] (no focus selected)")
                if nxt is not None:
                    out.append(f"       >> [{nxt.id}] {nxt.title} [ ]")
                    if focus is None and inline_note:
                        out.append(f"          * Note: {inline_note}")
                else:
                    out.append("       >> (no open tasks)")
                if paused_map:
                    out.append(
                        "       Unfocused / Paused Context:")
                    for pid, (ptitle, pnote) in paused_map.items():
                        out.append(f"          - [{pid}] {ptitle}")
                        if pnote:
                            out.append(f"            * Note: {pnote}")

        # Each project is an isolated block: trailing blank line +
        # separator bar close it off before the next block.
        out.append("")
        out.append(bar)

    return "\n".join(out)


# ---------------------------------------------------------------------- #
# ID spec parsing
# ---------------------------------------------------------------------- #

def _parse_id_spec(spec: str) -> list[int]:
    """Parse IDs/ranges/lists: '3', '2-4', '1,3,5', '1-2,4,6-7'."""
    ids: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            try:
                lo_i, hi_i = int(lo), int(hi)
            except ValueError:
                continue
            step = 1 if lo_i <= hi_i else -1
            ids.extend(range(lo_i, hi_i + step, step))
        else:
            try:
                ids.append(int(chunk))
            except ValueError:
                continue
    seen: set[int] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


# ---------------------------------------------------------------------- #
# Default README
# ---------------------------------------------------------------------- #

def _default_readme(project_name: str) -> str:
    return (
        f"# {project_name}\n\n"
        "Context Keeper project directory. See [README.md](../../README.md) "
        "for full documentation.\n"
    )


__all__ = [
    "ContextKeeper",
    "FocusResult",
    "ProjectContext",
    "UpdateResult",
    "_parse_id_spec",
    "TRACKED_GITIGNORE_PROTECTIONS",
    "GLOBAL_REGISTRY_FILE",
    "GLOBAL_STATE_FILE",
    "LEGACY_GLOBAL_CONFIG_FILE",
    "_detect_blanket_ignore",
]
