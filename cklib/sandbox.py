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
stable no matter where ``ck`` is invoked from. The ``ck-dev`` wrapper
additionally pins the anchor via ``CK_SANDBOX_ROOT`` after resolving
the git work-tree root (see :func:`sandbox_root`).

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

# Persistent sandbox-session state (``.sandbox/state.json``). Written
# by ``ck-dev`` (entry), read by the plain-``ck`` interceptor and the
# session exit path; the environment mirror wins when both exist.
STATE_FILENAME = "state.json"

# Environment mirror of the active sandbox project (absolute path).
# Exported by the ``ck-dev`` wrapper for every delegated subcommand so
# nested ``ck`` calls banner against the same project without touching
# the state file. Tests may inject it directly.
SANDBOX_ACTIVE_ENV = "CK_SANDBOX_ACTIVE"

# Environment variable that pins the sandbox anchor to a specific
# checkout root. Set by the ``ck-dev`` wrapper after it resolves the
# git work-tree root, so every sandbox operation references
# ``$REPO_ROOT/.sandbox`` regardless of the caller's cwd.
SANDBOX_ROOT_ENV = "CK_SANDBOX_ROOT"

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


# Workspace anchor markers for upward root discovery. ``.git`` is
# the primary anchor (directory OR worktree file); ``.ckrc`` is the
# workspace marker for non-git checkouts of context-keeper.
_REPO_ANCHOR_MARKERS = (".git", ".ckrc")


def production_repo_root() -> Path:
    """Return the production checkout root: the parent of ``cklib``.

    Lexical twin of the default branch of :func:`sandbox_root` (the
    same computation minus the ``.sandbox`` leaf): this is the
    checkout the session subshell was spawned from — where
    production resumes after the session ends.
    """
    return Path(__file__).resolve().parent.parent


def find_repo_root(start: Optional[Union[Path, str]] = None) -> Optional[Path]:
    """Resolve the true repository root regardless of nesting depth.

    Deterministic fallback chain (first non-None result wins):

    1. **Upward anchor search from ``start``** (default: the current
       working directory) — the first directory containing an anchor
       marker (``.git`` directory or worktree file, ``.ckrc``
       workspace marker) wins. When ``start`` lies inside a
       ``.sandbox`` tree, the search continues PAST the sandbox
       boundary (deeper in-sandbox anchors lose to the enclosing
       workspace), which is exactly the ``ck-dev``-from-`
       ``.sandbox/projects/alpha`` scenario.
    2. **``production_repo_root()``** — the checkout that supplies
       ``cklib``. A deep in-sandbox cwd typically has no anchor of
       its own, so the checkout anchor wins this way too.
    3. **``Path.cwd()``** — last resort only, when even the running
       ``cklib`` has no anchor (e.g. a site-packages install run
       from an unrelated non-git directory); a dangling cwd returns
       None instead of raising.

    Returns None only when every stage fails (dangling cwd AND no
    resolvable package parent). Never mutates anything.
    """
    stage3: Optional[Path] = None
    try:
        stage3 = Path.cwd()
    except OSError:
        stage3 = None

    start_dir = Path(start).resolve() if start is not None else stage3
    if start_dir is not None:
        # From inside a sandbox tree, begin the walk ABOVE the closest
        # ``.sandbox`` boundary: fixture/mock projects below it may
        # carry their own git anchors that must never capture the
        # search (spec: walk UP PAST the sandbox directory).
        boundaries = [p for p in (start_dir, *start_dir.parents)
                      if p.name == SANDBOX_DIR_NAME]
        current = boundaries[0].parent if boundaries else start_dir
        while True:
            for marker in _REPO_ANCHOR_MARKERS:
                anchor = current / marker
                try:
                    if anchor.exists():
                        return current
                except OSError:
                    pass  # unreadable level: keep walking upward
            if current.parent == current:
                break  # filesystem root reached
            current = current.parent

    pkg_root = production_repo_root()
    for marker in _REPO_ANCHOR_MARKERS:
        try:
            if (pkg_root / marker).exists():
                return pkg_root
        except OSError:
            pass

    return stage3


def sandbox_root() -> Path:
    """Return the sandbox root: ``<repo_root>/.sandbox`` (absolute).

    The repository root is resolved via :func:`find_repo_root` —
    upward anchor search from the cwd (walking PAST a ``.sandbox``
    boundary), falling back to the checkout that supplies ``cklib``
    — so running ``ck``/``ck-dev`` from deep inside
    ``.sandbox/projects/alpha`` still anchors to the TOP-LEVEL
    workspace root, never nesting a second ``.sandbox`` inside the
    sandbox itself. The returned path is LEXICAL (not resolved): if
    ``.sandbox`` is a symlink, callers must be able to see the LINK
    itself — resolving here would make ``clean_sandbox`` follow the
    link and delete the pointed-to tree (guardrail violation).
    Containment checks resolve explicitly where needed
    (:func:`is_within_sandbox`).

    ANCHOR OVERRIDE: when :data:`SANDBOX_ROOT_ENV`
    (``CK_SANDBOX_ROOT``, set by the ``ck-dev`` wrapper after
    ``git rev-parse --show-toplevel``) points at a directory whose
    basename is exactly ``.sandbox``, that path wins over the
    package-parent default — the sandbox stays pinned to the git
    checkout that provides ``cklib`` no matter where the process
    runs from. The basename guard keeps a mistyped or unrelated
    override from turning ``clean_sandbox`` into a recursive delete
    of an arbitrary directory.
    """
    override = os.environ.get(SANDBOX_ROOT_ENV, "").strip()
    if override and Path(override).name == SANDBOX_DIR_NAME:
        return Path(override)
    repo_root = find_repo_root() or production_repo_root()
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


# --------------------------------------------------------------------------- #
# Sandbox session state (ck-dev entry/exit + interceptor)
# --------------------------------------------------------------------------- #


def _state_path() -> Path:
    """``.sandbox/state.json`` — the persistent session marker."""
    return sandbox_root() / STATE_FILENAME


def _write_state(project: Path, repo_root: Path) -> None:
    """Persist the active sandbox session (best-effort atomic)."""
    import json
    import tempfile

    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "project": str(project),
        "repo_root": str(repo_root),
    }
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".state-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_state() -> Optional[dict]:
    """Load the session state file; None when absent/corrupt."""
    import json

    try:
        raw = _state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def active_sandbox_project(env: Optional[dict] = None) -> Optional[Path]:
    """The active sandbox project directory, or None.

    Resolution order (first valid wins):

    1. :data:`SANDBOX_ACTIVE_ENV` (``CK_SANDBOX_ACTIVE``) — exported
       by the ``ck-dev`` wrapper for delegated subcommands; accepted
       when the directory still exists.
    2. The ``project`` key of ``.sandbox/state.json`` (written by
       ``ck-dev`` entry) — accepted when the directory still exists.

    A stale marker (project folder deleted, e.g. by
    ``sandbox clean``) resolves to None: the interceptor then warns
    instead of printing a wrong project path.
    """
    if env is None:
        env = os.environ
    raw = str(env.get(SANDBOX_ACTIVE_ENV, "")).strip()
    if raw:
        p = Path(raw)
        if p.is_dir():
            return p
    data = _read_state()
    if data:
        raw = str(data.get("project", "")).strip()
        if raw:
            p = Path(raw)
            if p.is_dir():
                return p
    return None


def _pick_default_sandbox_project() -> Optional[Path]:
    """Best existing fixture project for implicit ``ck-dev`` entry.

    Preference order: ``alpha`` (the canonical active fixture), then
    the first project directory carrying a ``.ck/`` tree (sorted for
    determinism), then the first project directory at all. Returns
    None when no usable project exists under ``.sandbox/projects/``.
    """
    projects_dir = sandbox_root() / PROJECTS_SUBDIR
    try:
        candidates = sorted(
            p for p in projects_dir.iterdir() if p.is_dir()
        )
    except OSError:
        return None
    alpha = projects_dir / "alpha"
    if alpha.is_dir():
        return alpha
    for p in candidates:
        if (p / CK_DIR).is_dir():
            return p
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------- #
# Dynamic binary path resolution (banner + exit confirmation)
# --------------------------------------------------------------------------- #

# Resolved at import: True when running on Windows (where PATHEXT
# makes a bare "ck" ambiguous and shutil.which is the only reliable
# runtime lookup).
_ON_WINDOWS = os.name == "nt"


def _which(name: str, path_value: Optional[str] = None) -> Optional[Path]:
    """Resolve ``name`` on ``PATH`` to an absolute path (or None).

    Runtime equivalent of ``command -v <name>``. ``path_value``
    defaults to the CURRENT ``PATH`` environment variable; callers
    that must simulate an inherited subshell PATH pass it explicitly.
    All errors (missing PATH, OSError) degrade to None.
    """
    try:
        found = shutil.which(name, path=path_value)
    except (OSError, ValueError):
        return None
    return Path(found) if found else None


def _session_repaired_path(env: dict) -> Optional[str]:
    """``PATH`` with the checkout entrypoint dir re-pinned to FRONT.

    Self-healing for session subshells whose startup files (e.g.
    ``.zshrc``) re-export ``PATH`` and push the sandbox entry below
    user/system paths. The session contract requires the checkout's
    dev binary to win the ``ck`` lookup race, so resolution inside a
    session shell runs against this repaired value. Returns None
    when the checkout has no runnable entrypoint (nothing to pin).
    """
    entry_dir = sandbox_entrypoint_dir()
    if entry_dir is None:
        return None
    parts = [p for p in str(env.get("PATH", "")).split(os.pathsep)
             if p]
    entry_str = str(entry_dir)
    parts = [p for p in parts if p != entry_str]
    return os.pathsep.join([entry_str, *parts])


def activate_sandbox_path(env: Optional[dict] = None) -> Optional[Path]:
    """Pin the checkout's entrypoint dir to the FRONT of ``PATH``.

    The session-contract PATH repair, applied to a LIVE environment:
    the directory holding the local dev ``ck`` becomes the highest
    priority entry so the banner (and every resolution in this
    process, including the spawned subshell's inherited environment)
    sees the dev binary — never a globally installed one. Idempotent:
    an existing occurrence is moved to the front, not duplicated.

    Called by :func:`enter_sandbox` before the warning banner is
    rendered and by :func:`sandbox_shell_env` when building the child
    environment. Returns the pinned entry dir (None when the checkout
    has no runnable entrypoint; ``PATH`` is then left untouched).
    """
    if env is None:
        env = os.environ
    entry_dir = sandbox_entrypoint_dir()
    if entry_dir is None:
        return None
    env["PATH"] = _prepend_path_entry(env.get("PATH", ""), entry_dir)
    return entry_dir


def _strict_session_routing(env: dict) -> bool:
    """True when binary resolution must route STRICTLY to the checkout.

    Active only INSIDE a session subshell (the session-shell marker is
    truthy): the session contract requires the local dev binary, so a
    globally installed ``ck`` winning the raw ``PATH`` race must be
    overridden. Outside a session shell, PATH order is the user's
    explicit choice and is honored.
    """
    return _env_flag_on(env, SANDBOX_SHELL_ENV)


def _local_dev_binary() -> Optional[Path]:
    """The checkout's LOCAL dev ``ck`` binary (or None).

    Direct checkout lookup, independent of ``PATH``: the repository
    root's own ``ck`` wrapper, then the ``bin/`` layout. Executability
    is enforced the same way :func:`has_runnable_entrypoint` does.
    """
    dev_root = production_repo_root()
    for candidate in (dev_root / "ck", dev_root / "bin" / "ck"):
        try:
            if not candidate.is_file():
                continue
            if os.name == "nt" or os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


def active_binary_path(env: Optional[dict] = None) -> Optional[Path]:
    """The ACTIVE ``ck`` executable, resolved dynamically at runtime.

    Mirrors ``command -v ck`` (equivalently ``which -a ck | head -1``
    / ``type -P ck``): the first executable ``ck`` on the current
    ``PATH`` — the exact binary a bare ``ck`` typed right now would
    run. Never hardcoded; the lookup is always live.

    STRICT SESSION ROUTING: inside a session subshell
    (:data:`SANDBOX_SHELL_ENV` truthy) the checkout's LOCAL DEV
    binary is the session contract — a globally installed ``ck``
    must never win the lookup race, even when the caller's PATH
    (e.g. after an rc file re-export) ranks it first. When a PATH hit
    still points OUTSIDE the checkout (like ``~/.local/bin/ck``), it
    is overridden by the checkout's dev binary so sandbox sessions
    always report/execute local code.

    SESSION SELF-HEALING: inside a session subshell
    (:data:`SANDBOX_SHELL_ENV` truthy) startup files may have
    re-exported ``PATH`` below user/system paths; resolution then
    runs FIRST against the repaired PATH (:func:`_session_repaired_path`
    — checkout entrypoint dir re-pinned to the front), so the active
    binary is the LOCAL DEV binary per the session contract. When the
    repair finds nothing runnable the raw PATH is consulted as before.

    Fallback chain when PATH lookup fails (first hit wins):

    1. the directory of ``sys.argv[0]`` holding a runnable
       ``ck`` (the wrapper's own location),
    2. the checkout entrypoint dir (:func:`sandbox_entrypoint_dir`).

    Returns None when nothing resolvable exists (pure-Python callers
    must tolerate that). Injecting ``env`` is a test seam only.
    """
    if env is None:
        env = os.environ
    in_session = _env_flag_on(env, SANDBOX_SHELL_ENV)
    if in_session:
        repaired = _session_repaired_path(env)
        if repaired is not None:
            ck = _which("ck", path_value=repaired)
            if ck is not None:
                return ck
    ck = _which("ck", path_value=str(env.get("PATH", "")))
    if ck is not None:
        if in_session and _strict_session_routing(env):
            resolved = ck.resolve() if ck.exists() else ck
            dev_root = production_repo_root()
            if dev_root not in resolved.parents and resolved != (
                dev_root / "ck"
            ):
                # A global binary won the raw PATH race inside the
                # session: strictly re-route to the checkout's local
                # dev binary (the session contract).
                dev = _local_dev_binary()
                if dev is not None:
                    return dev
        return ck
    # Fallback 1: the running launcher's own directory.
    try:
        if sys.argv and sys.argv[0]:
            launch_dir = Path(sys.argv[0]).resolve().parent
            candidate = launch_dir / "ck"
            if candidate.is_file() and (
                _ON_WINDOWS or os.access(candidate, os.X_OK)
            ):
                return candidate
    except (OSError, ValueError):
        pass
    # Fallback 2: the checkout's entrypoint directory.
    entry_dir = sandbox_entrypoint_dir()
    if entry_dir is not None:
        candidate = entry_dir / "ck"
        if candidate.is_file() and (
            _ON_WINDOWS or os.access(candidate, os.X_OK)
        ):
            return candidate
    return None


def resolved_global_binary_path(env: Optional[dict] = None) -> Path:
    """``ck`` binary that owns production after the session ends.

    Dynamically resolved (never hardcoded): a NEW ``PATH`` lookup —
    with the sandbox's PATH-priority entry REMOVED — simulates the
    environment the caller returns to once the session subshell has
    exited. Falls back to the physical global install
    (``~/.local/bin/ck``), then to the checkout wrapper, then to a
    ``ck``-named placeholder.
    """
    if env is None:
        env = os.environ
    parts = [
        p for p in str(env.get("PATH", "")).split(os.pathsep)
        if p
    ]
    checkout_entry = sandbox_entrypoint_dir()
    if checkout_entry is not None:
        entry_str = str(checkout_entry)
        parts = [p for p in parts if p != entry_str]
    ck = _which("ck", path_value=os.pathsep.join(parts))
    if ck is not None:
        return ck
    try:
        fallback = Path.home() / ".local" / "bin" / "ck"
    except (OSError, RuntimeError):
        fallback = None
    if fallback is not None and fallback.is_file():
        return fallback
    if checkout_entry is not None and (checkout_entry / "ck").is_file():
        return checkout_entry / "ck"
    return Path("ck")


# --------------------------------------------------------------------------- #
# Sandbox warning banner (dynamic binary row)
# --------------------------------------------------------------------------- #

# Banner content: the SANDBOX MODE label and the exit hint are static
# (rigid visual integrity); the ACTIVE BINARY row is the one
# deliberately dynamic element — resolved live on every render. The
# BOX WIDTH is dynamic too: computed from the longest content row,
# so long binary paths widen the box and short ones narrow it —
# never wrapping text or breaking the borders.
_BANNER_LABEL = "⚠️  SANDBOX MODE ACTIVE"
_BANNER_HINT = "Type 'exit' or press Ctrl+D to return to production"
_BANNER_BINARY_LABEL = "Active binary:"


# --------------------------------------------------------------------------- #
# Interactive sandbox subshell (bare `ck-dev`)
# --------------------------------------------------------------------------- #

# Prompt marker prepended (best-effort, via the inherited PS1/PROMPT
# environment variables ONLY — never rc files) so the user always
# sees when a dev sandbox session is active.
SANDBOX_PROMPT_MARK = "(sandbox) "

# Marker exported INSIDE the session subshell (children can detect
# the session shell without re-reading state files).
SANDBOX_SHELL_ENV = "CK_SANDBOX_SHELL"

# Spawn overrides: NO wins over FORCE. Default (neither set) spawns
# only on interactive terminals (stdin isatty) — piped/CI invocations
# keep the deterministic banner+dashboard output.
CK_DEV_NO_SUBSHELL_ENV = "CK_DEV_NO_SUBSHELL"
CK_DEV_FORCE_SUBSHELL_ENV = "CK_DEV_FORCE_SUBSHELL"


def already_in_sandbox_session(env: Optional[dict] = None) -> bool:
    """True when the environment already belongs to a sandbox session.

    The matryoshka-guard input: ``CK_SANDBOX`` / ``CK_DEV`` truthy in
    the INHERITED environment (checked before the ``ck-dev`` wrapper
    forces its own toggles) means a parent session subshell is active
    and bare ``ck-dev`` must not spawn a nested one.
    """
    if env is None:
        env = os.environ
    return _env_flag_on(env, "CK_SANDBOX") or _env_flag_on(env, "CK_DEV")


def print_nesting_guard(file=None) -> None:
    """Warn about an already-active session, then show the dashboard.

    The spec'd nesting guard: no second subshell, no session rewrite —
    just the warning and the current cross-project view.
    """
    out = file or sys.stdout
    print("⚠️  Already in sandbox mode", file=out)
    print("[i] No nested session started. Type 'exit' to return to "
          "production.", file=out)
    print(file=out)
    try:
        from .core import ContextKeeper
        print(ContextKeeper().dashboard(), file=out)
    except Exception:
        pass  # the warning already succeeded; never fail on render


def has_runnable_entrypoint(directory: Path) -> bool:
    """True when ``directory`` holds an EXECUTABLE ``ck`` / ``ck-dev``.

    Existence alone is not enough for PATH routing: a shell SKIPS a
    non-executable file during lookup, so prepending a directory that
    merely *contains* ``ck`` would leave an earlier entry (e.g. a
    globally installed ``~/.local/bin/ck``) winning the race. On
    Windows there is no exec bit — a present file is the contract.
    """
    for name in ("ck", "ck-dev"):
        candidate = directory / name
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        if os.name == "nt" or os.access(candidate, os.X_OK):
            return True
    return False


def sandbox_entrypoint_dir(repo_root: Optional[Path] = None) -> Optional[Path]:
    """Directory inside the checkout that holds the ``ck`` entrypoint.

    Prepended to ``PATH`` for the session subshell so a bare ``ck``
    resolves to THIS checkout's wrapper (and therefore the current
    dev sources) instead of a globally installed system binary.

    Resolution order (first RUNNABLE winner — see
    :func:`has_runnable_entrypoint`, existence alone is not enough):

    1. the repository root itself — where the ``ck`` / ``ck-dev``
       wrappers live in a source checkout (``<repo>/ck``),
    2. ``<repo>/bin`` — the conventional layout when entrypoints are
       shimmed there,
    3. an active virtualenv's scripts directory (``$VIRTUAL_ENV/bin``,
       or ``Scripts`` on Windows) when the checkout is installed
       editable into a venv.

    Returns None when nothing usable is found (e.g. a pip install
    with no checkout): the caller then leaves ``PATH`` untouched.
    """
    root = repo_root if repo_root is not None else production_repo_root()
    for candidate in (root, root / "bin"):
        try:
            if has_runnable_entrypoint(candidate):
                return candidate
        except OSError:
            pass
    venv = os.environ.get("VIRTUAL_ENV", "").strip()
    if venv:
        scripts = "Scripts" if os.name == "nt" else "bin"
        venv_bin = Path(venv) / scripts
        if venv_bin.is_dir():
            return venv_bin
    return None


def _prepend_path_entry(path_value: str, entry: Path) -> str:
    """Prepend ``entry`` to a ``PATH`` value, de-duplicated.

    An existing occurrence is removed first so the checkout dir also
    moves to the FRONT (a globally installed ``~/.local/bin`` ahead
    of it would otherwise keep winning). Empty entries are dropped.
    """
    entry_str = str(entry)
    parts = [p for p in path_value.split(os.pathsep) if p]
    parts = [p for p in parts if p != entry_str]
    return os.pathsep.join([entry_str, *parts])


def _pin_pythonpath(env: dict) -> None:
    """Force ``$REPO_ROOT`` to POSITION 0 of ``PYTHONPATH``.

    The root is :func:`production_repo_root` (the checkout supplying
    the running ``cklib``). An existing occurrence is MOVED to the
    front (never duplicated); absent it is prepended. Guarantees every
    child of the session (nested ``ck`` calls, editor subprocesses,
    spawned tooling) imports ``cklib`` from THIS checkout — never a
    site-packages copy or a stale snapshot — even after rc files
    re-export the variable.
    """
    repo_root = str(production_repo_root())
    parts = [p for p in str(env.get("PYTHONPATH", "")).split(os.pathsep)
             if p]
    parts = [p for p in parts if p != repo_root]
    env["PYTHONPATH"] = os.pathsep.join([repo_root, *parts])


def sandbox_shell_env(project: Path) -> dict:
    """The FULL environment for the sandbox session subshell.

    A copy of the inherited environment updated ONLY with the sandbox
    flags and paths — no shell config interception of any kind:

    - ``CK_SANDBOX=1`` (the spec'd toggle),
    - the active project mirror (``CK_SANDBOX_ACTIVE``),
    - the sandbox anchor (``CK_SANDBOX_ROOT``) — REQUIRED by the
      ``ck`` launcher's sandbox session pin: it derives the checkout
      root from this variable, so even a physical production copy
      executing inside the session resolves and imports the CHECKOUT's
      local dev code,
    - the session marker (``CK_SANDBOX_SHELL``),
    - ``PATH`` with the checkout's entrypoint directory PREPENDED so
      a bare ``ck`` inside the subshell executes the LOCAL dev code
      (and its current top-level flags), never a globally installed
      system binary,
    - ``PYTHONPATH`` with the repository root pinned to POSITION 0
      (:func:`_pin_pythonpath`) so child ``ck`` invocations import
      the workspace sources — never a site-packages copy,
    - a best-effort ``PS1``/``PROMPT`` prompt prefix: applied only
      when the user's own environment already exports one. Shells
      that ignore an inherited prompt variable fall back to the
      startup banner and the ``CK_SANDBOX=1`` marker; their rc files
      are never read, written or swapped.
    """
    existing = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep)
                if p]
    env = os.environ.copy()
    env["CK_SANDBOX"] = "1"
    env[SANDBOX_ACTIVE_ENV] = str(project)
    env[SANDBOX_ROOT_ENV] = str(sandbox_root())
    env[SANDBOX_SHELL_ENV] = "1"
    _pin_pythonpath(env)
    # PATH priority routing: the checkout's own entrypoint dir wins
    # the `ck` lookup race inside the subshell (same repair
    # :func:`enter_sandbox` applied to this process before spawn).
    activate_sandbox_path(env)
    mark = SANDBOX_PROMPT_MARK.strip()
    for var in ("PS1", "PROMPT"):
        current = env.get(var, "")
        if current.strip() and not current.lstrip().startswith(mark):
            env[var] = f"{mark} {current}"
    return env


def _should_spawn_shell(explicit: Optional[bool] = None) -> bool:
    """Decide whether bare ``ck-dev`` opens an interactive subshell.

    ``explicit`` (the library API) wins when given. Otherwise:
    ``CK_DEV_NO_SUBSHELL=1`` suppresses, ``CK_DEV_FORCE_SUBSHELL=1``
    forces (test/CI seam), and the default spawns only on a TRULY
    interactive terminal — BOTH stdin and stdout must be TTYs. A
    piped stdout (test harness, ``| tee``, CI capture) never spawns,
    even when the inherited stdin happens to be a terminal — this
    keeps subprocess-driven invocations deterministic and hang-free.
    """
    if explicit is not None:
        return explicit
    if _env_flag_on(os.environ, CK_DEV_NO_SUBSHELL_ENV):
        return False
    if _env_flag_on(os.environ, CK_DEV_FORCE_SUBSHELL_ENV):
        return True

    def _isatty(stream) -> bool:
        try:
            return bool(stream.isatty())
        except (OSError, ValueError, AttributeError):
            return False

    return _isatty(sys.stdin) and _isatty(sys.stdout)


def spawn_sandbox_shell(project: Path, *, shell: Optional[str] = None) -> int:
    """Run an interactive subshell bound to the sandbox session.

    The user's current shell (``$SHELL``, ``/bin/bash`` fallback) is
    spawned DIRECTLY, in the exact current working directory, with
    :func:`sandbox_shell_env` — the inherited environment plus the
    sandbox flags/paths. NO rc files, ``--rcfile`` argv or
    ``ZDOTDIR`` swaps are generated: the user's shell configuration
    loads untouched. The session is visible through the environment
    (``CK_SANDBOX=1``), the startup banner, and — when the shell
    honors an inherited ``PS1``/``PROMPT`` — the ``(sandbox) ``
    prompt marker. Returns the shell's exit code.
    """
    import subprocess

    shell_cmd = shell or os.environ.get("SHELL", "").strip() or "/bin/bash"
    child_env = sandbox_shell_env(project)
    print(f"[i] Starting sandbox subshell ({Path(shell_cmd).name}). "
          "Type 'exit' to return to production.")
    try:
        # No explicit cwd: the child inherits this process's exact
        # working directory (preserved, per spec).
        proc = subprocess.run([shell_cmd], env=child_env, check=False)
        return proc.returncode
    except OSError as e:
        print(
            f"ERROR: Could not start sandbox subshell {shell_cmd!r}: {e}",
            file=sys.stderr,
        )
        return 0  # entry itself succeeded


def enter_and_spawn(*, project: Optional[Union[Path, str]] = None,
                    spawn: Optional[bool] = None) -> int:
    """Bare ``ck-dev``: matryoshka guard → session setup → subshell.

    1. GUARD: when the inherited environment already carries the
       session toggles (``CK_SANDBOX`` / ``CK_DEV`` truthy — i.e. we
       are INSIDE a session subshell), print the nesting warning plus
       the current Global Dashboard and spawn NOTHING.
    2. Otherwise run the session setup (:func:`enter_sandbox`:
       banner + dashboard + persistent state).
    3. On success, spawn the interactive subshell
       (:func:`spawn_sandbox_shell`) when appropriate
       (:func:`_should_spawn_shell`); after the shell exits (the user
       typed ``exit``), clear the session for a clean return to
       production.

    ``spawn`` overrides the TTY/env decision (library/test seam).
    """
    if already_in_sandbox_session():
        print_nesting_guard()
        return 0
    code = enter_sandbox(project=project)
    if code != 0:
        return code
    if _should_spawn_shell(spawn):
        active = active_sandbox_project()
        if active is not None:
            rc = spawn_sandbox_shell(active)
            # Natural exit: typing `exit` (or EOF) in the subshell
            # ends the session — clear the persistent marker so the
            # next bare `ck-dev` starts fresh and `ck` commands stop
            # banner-ing against a dead session.
            try:
                exit_sandbox()
            except Exception:
                pass  # shell rc wins; cleanup must never mask it
            return rc
    return 0


def render_sandbox_banner(*, color: Optional[bool] = None) -> str:
    """The sandbox warning banner with the ACTIVE BINARY row.

    DYNAMIC-WIDTH box: the inner width is computed from the longest
    content row (label, active-binary path, exit hint), so the border
    expands for long binary paths and contracts for short ones —
    no wrapping, no broken borders:

        ┌──────────────────────────────────────────────────────────┐
        │ ⚠️  SANDBOX MODE ACTIVE                                  │
        │ Active binary: /home/user/.local/bin/ck                  │
        │ Type 'exit' or press Ctrl+D to return to production      │
        └──────────────────────────────────────────────────────────┘

    ``len()`` measures the padding only: terminal cells for emoji/
    wide glyphs do not participate in the character-count padding,
    so rows stay byte-stable across environments.

    With color enabled the whole box is painted bold yellow (the
    universal warning treatment). ``color`` defaults to the standard
    palette decision (:func:`cklib.ui.color_enabled`): ``NO_COLOR``
    wins, ``FORCE_COLOR``/``CLICOLOR_FORCE`` force it on, otherwise
    the target stream must be a TTY — so piped/non-interactive output
    stays byte-identical plain text.
    """
    binary = active_binary_path()
    binary_str = str(binary) if binary is not None else "ck (not found on PATH)"
    rows = (
        _BANNER_LABEL,
        f"{_BANNER_BINARY_LABEL} {binary_str}",
        _BANNER_HINT,
    )
    pad = max(len(row) for row in rows)
    inner = pad + 2  # content rows add '│ ' + ' │' (4); borders add 2
    lines = (
        f"┌{'─' * inner}┐",
        *(f"│ {row:<{pad}} │" for row in rows),
        f"└{'─' * inner}┘",
    )
    if color is None:
        try:
            from .ui import color_enabled
            color = color_enabled()
        except Exception:
            color = False
    if color:
        try:
            from .ui import Palette
            palette = Palette(True)
            return "\n".join(palette.bold_yellow(l) for l in lines)
        except Exception:
            pass
    return "\n".join(lines)


def print_sandbox_banner(file=None) -> None:
    """Print the static sandbox warning banner."""
    print(render_sandbox_banner(), file=file or sys.stdout)


def enter_sandbox(*, project: Optional[Union[Path, str]] = None) -> int:
    """Explicit sandbox entry (bare ``ck-dev``): session setup.

    - ensures ``.sandbox/`` exists (running the full fixture setup
      when the mock environment is missing);
    - resolves the active project: the explicit ``project``
      (validated to exist) or the best fixture project;
    - persists the session (``.sandbox/state.json``) so the plain
      ``ck`` interceptor and the session exit path can find it later;
    - pins the checkout's entrypoint dir to the FRONT of ``PATH``
      (:func:`activate_sandbox_path`) so the local dev binary wins
      the ``ck`` lookup race inside the session;
    - prints the sandbox warning banner to STDOUT (the active-binary
      row therefore reports the LOCAL DEV binary).

    Does NOT spawn the interactive subshell — the CLI composes this
    with :func:`spawn_sandbox_shell` so the session-setup output
    (banner + dashboard) stays deterministic and testable.

    Returns a process exit code (0 on success, 1 with a clean
    message when no usable sandbox project exists).
    """
    from .sandbox_setup import setup_sandbox

    root = sandbox_root()
    if not (root / STATE_FILENAME).exists() and not (
        root / CONFIG_SUBDIR / "projects.json"
    ).is_file():
        # Uninitialized sandbox: build the mock environment first.
        if not setup_sandbox():
            return 1
    else:
        ensure_sandbox_dir()

    if project is not None:
        chosen = Path(project)
        if not chosen.is_dir():
            print(
                f"ERROR: Sandbox project does not exist: {chosen}",
                file=sys.stderr,
            )
            return 1
    else:
        chosen = _pick_default_sandbox_project()
        if chosen is None:
            print(
                "ERROR: No sandbox projects available. "
                "Run `ck-dev sandbox setup` first.",
                file=sys.stderr,
            )
            return 1

    _write_state(chosen, production_repo_root())
    # Entry IS dev mode: activate interception so the registration
    # below lands in the SANDBOX registry and the dashboard reads it
    # back. Idempotent when the wrapper already set it.
    os.environ["CK_SANDBOX"] = "1"
    # ANCHOR EXPORT (launcher session pin): the `ck` launcher derives
    # the checkout root from this variable, so export it here too —
    # not only in the wrapper and the subshell env.
    os.environ[SANDBOX_ROOT_ENV] = str(sandbox_root())
    # SOURCE PIN (spec): inside the session, `ck -v` must reflect the
    # version defined in the LOCAL repository code. Force the checkout
    # to position 0 of sys.path so this process imports the workspace
    # sources — never a site-packages copy or a stale snapshot.
    repo_root = str(production_repo_root())
    if repo_root in sys.path:
        sys.path.remove(repo_root)
    sys.path.insert(0, repo_root)
    # PYTHONPATH PRIORITY (session contract): children (nested `ck`
    # calls, spawned shells) inherit the same source pinning.
    _pin_pythonpath(os.environ)
    # PATH PRIORITY (session contract): pin the checkout's dev binary
    # to the front of PATH BEFORE the banner renders, so the banner
    # reports the LOCAL dev binary — and the spawned subshell (built
    # from this process's environment) inherits the same priority.
    activate_sandbox_path()
    # Auto-register the active project so the dashboard lists it
    # immediately (requirement: `ck st -g` inside the sandbox must
    # never show an empty registry when a project exists).
    try:
        from . import registry
        registry.register_project(chosen, name=chosen.name)
    except Exception:
        pass  # registration is best-effort; entry must not fail on it
    print_sandbox_banner()
    print()
    # Immediately surface the cross-project view: the fixture
    # registry (built above when missing) makes the mock projects
    # visible on the dashboard out-of-the-box.
    try:
        from .core import ContextKeeper
        print(ContextKeeper().dashboard())
    except Exception:
        pass  # the banner already succeeded; never fail entry on render
    return 0


def exit_sandbox() -> int:
    """Leave sandbox mode (``exit`` inside the session subshell).

    Called from the subshell EXIT TRAP — after the user typed ``exit``
    (or pressed Ctrl+D) — and clears the persistent session marker.
    The confirmation is a SINGLE clean status line reporting the
    global ``ck`` binary that takes over, resolved DYNAMICALLY at the
    moment of exit (``command -v ck`` on the production PATH — with
    the sandbox's PATH-priority entry removed — falling back to the
    physical global install). Outside of a sandbox session (no state
    file and no active-project marker) it is a graceful no-op with an
    informational line — never an error, never a traceback.
    """
    state = _read_state()
    had_session = state is not None or bool(
        str(os.environ.get(SANDBOX_ACTIVE_ENV, "")).strip()
    )
    if had_session:
        try:
            _state_path().unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            print(
                f"ERROR: Could not clear sandbox state: {e}",
                file=sys.stderr,
            )
            return 1
        # SINGLE clean status line (legacy 'Back to production' line
        # removed — one confirmation, no duplicates).
        global_ck = resolved_global_binary_path()
        print(f"[ok] Exited sandbox mode. Active binary is now: {global_ck}")
    else:
        print("[i] Not in sandbox mode — nothing to exit.")
    return 0


def reject_dev_exit(argv: Optional[list] = None) -> int:
    """Safeguard: a top-level ``exit`` argument cannot close the session.

    A subshell session is a real interactive shell process; typing
    ``exit`` INSIDE it is the only way to leave, and Ctrl+D works
    too. A new top-level process cannot end that session — silently
    no-op-ing or, worse, launching a replacement subshell would
    mislead the user about what is active.

    Instead print the clear warning and return exit code 1 WITHOUT
    launching anything (guardrail semantics, ``__main__``-style;
    this never raises).
    """
    if argv is None:
        argv = sys.argv
    entry = Path(argv[0]).name if argv else "ck-dev"
    print(
        f"[!] '{entry} exit' cannot close an active subshell.\n"
        "[!] To exit sandbox mode, type 'exit' or press Ctrl+D."
    )
    return 1


def emit_interceptor_banner_if_needed(
    argv: Optional[list] = None,
    env: Optional[dict] = None,
    *,
    file=None,
) -> bool:
    """Plain ``ck`` protection banner for sandbox sessions.

    When the STANDARD ``ck`` entrypoint (never ``ck-dev`` itself)
    runs while dev mode is active — inside ``.sandbox/`` or with
    ``CK_SANDBOX=1`` — the exact sandbox warning banner is printed
    to STDOUT first, then the command proceeds transparently in
    sandbox mode.

    Skipped for help/version flows (``help`` subcommand; ``-h`` /
    ``--help`` / ``-v`` short-circuit earlier in ``main``), so
    documentation output stays clean. Returns True when a banner was
    emitted (test seam).
    """
    if argv is None:
        argv = sys.argv
    if env is None:
        env = os.environ

    if not argv:
        return False
    entry = Path(argv[0]).name.lower()
    if entry in _DEV_ENTRYPOINT_NAMES:
        return False  # ck-dev prints its own banners
    if not dev_context_active(argv=argv, env=env):
        return False
    if len(argv) > 1 and argv[1] == "help":
        return False

    active = active_sandbox_project(env)
    if active is not None:
        print_sandbox_banner(file=file)
    else:
        print(
            "⚠️  CK_SANDBOX is active, but no sandbox session is set up "
            "(no active project).\n"
            "   Run `ck-dev` to enter sandbox mode, or unset CK_SANDBOX "
            "to leave dev mode.",
            file=file or sys.stdout,
        )
        print(file=file or sys.stdout)
    return True


def dev_context_active(argv: Optional[list] = None,
                       env: Optional[dict] = None) -> bool:
    """True when the process runs inside a dev-sandbox context.

    Either dev mode is explicitly on (:func:`is_dev_mode` — entrypoint
    name, ``CK_SANDBOX``/``CK_DEV`` toggles, ``--sandbox`` flag) or the
    working directory already lives under the sandbox tree. Used by the
    plain-``ck`` interceptor, the active-project export, and the
    REGISTRY routing layer: a process inside ``.sandbox/`` reads and
    writes the sandbox registry even when the env toggles are unset.
    """
    if is_dev_mode(argv=argv, env=env):
        return True
    try:
        return is_within_sandbox(Path.cwd())
    except OSError:
        return False


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


def is_dev_entrypoint(argv: Optional[list] = None) -> bool:
    """True when ``argv[0]`` (default ``sys.argv``) names the dev
    entrypoint — the repo ``ck-dev`` wrapper, an installed
    ``~/.local/bin/ck-dev`` symlink, or a console script — no matter
    which directory it runs from.
    """
    if argv is None:
        argv = sys.argv
    if not argv:
        return False
    return Path(argv[0]).name.lower() in _DEV_ENTRYPOINT_NAMES


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

    if is_dev_entrypoint(argv):
        return True
    if argv and "--sandbox" in argv[1:]:
        return True

    # NOTE: deliberately NO cwd-based trigger here — a production
    # command run from a checkout that happens to contain a manual
    # ``.sandbox/`` must never silently enter dev mode. The cwd
    # context is applied at the REGISTRY routing layer instead (see
    # :func:`dev_context_active`).
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


def _intercept_global_config_path(target: Path,
                                  create: bool = True) -> Optional[Path]:
    """Redirect a path inside the global config dir into the sandbox.

    Returns the sandboxed path, or None when ``target`` is not under
    the global config root. With ``create=False`` the mapping is
    PURE (no directories are materialized) — read-side probes must
    stay side-effect free.
    """
    root = _global_config_root()
    try:
        target_r = target.resolve()
        root_r = root.resolve()
    except OSError:
        return None
    if target_r == root_r:
        # Whole-dir target: redirect to the sandbox config dir root.
        if create:
            return sandbox_config_dir()
        return sandbox_root() / CONFIG_SUBDIR
    if root_r in target_r.parents:
        rel = target_r.relative_to(root_r)
        result = sandbox_root() / CONFIG_SUBDIR / rel
        if create:
            result.parent.mkdir(parents=True, exist_ok=True)
        return result
    return None


def _intercept_project_path(target: Path,
                            create: bool = True) -> Optional[Path]:
    """Redirect a real project write (``.ck/`` tree, ``.ck.json``).

    Returns the sandboxed path, or None when ``target`` is not a
    production project write. With ``create=False`` the mapping is
    PURE (no directories are materialized) — read-side probes must
    stay side-effect free.
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
        base = (
            sandbox_project_dir(project_root) if create
            else sandbox_root() / PROJECTS_SUBDIR
            / project_hash(project_root)
        )
        result = base / Path(*parts[idx:])
    elif parts[-1] == PROJECT_CONFIG_FILENAME:
        project_root = Path(*parts[:-1])
        base = (
            sandbox_project_dir(project_root) if create
            else sandbox_root() / PROJECTS_SUBDIR
            / project_hash(project_root)
        )
        result = base / PROJECT_CONFIG_FILENAME
    else:
        return None

    if create:
        try:
            result.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return result


def resolve_write_path(target_path: Union[Path, str],
                       *, create: bool = True,
                       force: bool = False) -> Path:
    """Resolve the destination for a write to ``target_path``.

    - Dev mode OFF (and ``force=False``): the original path is
      returned unchanged (writes go to production as normal; reads
      always use the original).
    - Dev mode ON (or ``force=True``): production writes are
      redirected into the sandbox — global-config paths to
      ``.sandbox/config/`` and real-project paths (``.ck/`` trees,
      ``.ck.json``) to ``.sandbox/projects/<hash>/``.

    ``force=True`` applies the interception mapping even when the
    dev-mode toggles are off — used by the registry layer, whose
    routing follows the BROADER sandbox context (cwd inside the
    sandbox tree, see :func:`dev_context_active`) rather than the
    strict write-interception triggers.

    ``create=False`` performs the same mapping as a PURE PATH
    computation: no directory is created and nothing is logged.
    READ-side probes (does a sandboxed mirror exist?) must use it —
    a read must never materialize ``.sandbox/`` skeleton trees in
    the checkout.

    Non-production targets (e.g. a user's arbitrary scratch file)
    pass through unchanged even in dev mode: the sandbox exists to
    protect *Context Keeper production state*, not to kidnap all
    filesystem writes.
    """
    target = Path(target_path)

    if not force and not is_dev_mode():
        return target

    # Already inside the sandbox: leave as-is (idempotent mapping).
    if is_within_sandbox(target):
        return target

    intercepted = _intercept_global_config_path(target, create=create)
    if intercepted is None:
        intercepted = _intercept_project_path(target, create=create)
    if intercepted is not None:
        if create:
            log_sandbox_debug(
                f"Intercepted write -> {intercepted} "
                f"(real path untouched: {target})"
            )
        return intercepted

    if create:
        # Not Context-Keeper production state: pass through, but note
        # it in the debug log so unexpected writes are traceable.
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
    "SANDBOX_ROOT_ENV",
    "PROJECTS_SUBDIR",
    "CONFIG_SUBDIR",
    "DEV_LOG_FILENAME",
    "STATE_FILENAME",
    "SANDBOX_ACTIVE_ENV",
    "SandboxViolationError",
    "sandbox_root",
    "find_repo_root",
    "production_repo_root",
    "project_hash",
    "ensure_sandbox_dir",
    "sandbox_project_dir",
    "sandbox_config_dir",
    "clean_sandbox",
    "is_within_sandbox",
    "is_dev_entrypoint",
    "is_dev_mode",
    "resolve_write_path",
    "assert_no_real_write",
    "debug_enabled",
    "log_sandbox_debug",
    "active_sandbox_project",
    "dev_context_active",
    "activate_sandbox_path",
    "active_binary_path",
    "resolved_global_binary_path",
    "_pin_pythonpath",
    "_local_dev_binary",
    "render_sandbox_banner",
    "print_sandbox_banner",
    "enter_sandbox",
    "exit_sandbox",
    "reject_dev_exit",
    "emit_interceptor_banner_if_needed",
    "SANDBOX_PROMPT_MARK",
    "SANDBOX_SHELL_ENV",
    "CK_DEV_NO_SUBSHELL_ENV",
    "CK_DEV_FORCE_SUBSHELL_ENV",
    "already_in_sandbox_session",
    "print_nesting_guard",
    "has_runnable_entrypoint",
    "sandbox_entrypoint_dir",
    "sandbox_shell_env",
    "spawn_sandbox_shell",
    "enter_and_spawn",
]
