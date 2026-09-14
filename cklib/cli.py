"""Command-line entry point and dispatcher for Context Keeper."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from .config import (
    GLOBAL_CONFIG_DIR,
    GLOBAL_REGISTRY_FILE,
    GLOBAL_STATE_FILE,
    INSTALL_PATH,
    LEGACY_GLOBAL_CONFIG_FILE,
    LockTimeoutError,
    USER_INSTALL_PATH,
    VERSION,
)
from .core import ContextKeeper, UpdateResult
from . import git as gith
from .registry import RegistryCorruptError


HELP_TEXT = f"""Context Keeper CLI [v{VERSION}]

Usage: ck <command> [args]

Local (current project):
  init                       Initialize .ck/ locally (use --register to register globally)
  st [--all]                 Status of the current project
  tasks                      Print the local task list to STDOUT (no editor)
  list                       Local view: same as `ck tasks`
  start <ID>                 Mark task ID as focused ([>])
  done <ID|Range>            Mark task(s) as done ([x])
  add <text>                 Insert a new open task before ## Completed
  save                       Two-step save: optional note, then local commit
  edit                       Open PLAN.md in your editor
  log                        Open HISTORY.md in your editor
  info                       Diagnostic information about this installation

Global (all registered projects):
  st --global                Cross-project dashboard
  dashboard                  Cross-project dashboard (compact table)
  dashboard -v, --verbose    Detailed block view: full PREV/FOCUS/NEXT
                             triad context per project
  list -g, --global          Global view: same as `ck dashboard`
  register   [-n NAME] [--path PATH]  Add a project to the global registry
  unregister [--path PATH | NAME]     Remove a project from the registry
  prune                              Drop entries whose folders no longer exist

Editor resolution (ck edit / ck log):
  1. "editor" key in .ck.json at the project root
  2. $VISUAL
  3. $EDITOR
  4. nano (if installed), else vi

System commands:
  install                    Symlink ck to {USER_INSTALL_PATH}
  uninstall                  Remove the {USER_INSTALL_PATH} symlink
  update                     git fetch + git pull --ff-only origin main

Dev mode (ck-dev / CK_SANDBOX=1 / --sandbox):
  dev clean                  Remove the .sandbox/ directory (same as ck-clean)

Options:
  -v, --version              Show version
  -h, --help                 Show this help
"""


# Commands that may print a non-blocking update notice to stderr.
_NOTIFIER_COMMANDS = frozenset({
    "st", "status", "dashboard", "list", "tasks", "info", "prune",
})


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="ck",
        description="Context Keeper CLI",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="store_true",
                        help="Show version and exit")
    parser.add_argument("-h", "--help", action="store_true",
                        help="Show help and exit")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_init = sub.add_parser(
        "init", help="Initialize .ck/ locally (use --register for global registration)"
    )
    p_init.add_argument(
        "--register", action="store_true",
        help="Also register the project in the global registry",
    )

    p_st = sub.add_parser("st", help="Project status")
    p_st.add_argument("--all", action="store_true", help="Include full PLAN.md")
    p_st.add_argument("--global", dest="global_dash", action="store_true",
                      help="Show global dashboard instead")

    p_dash = sub.add_parser(
        "dashboard",
        help="Cross-project dashboard (compact table; -v for block view)",
    )
    p_dash.add_argument("-v", "--verbose", dest="verbose", action="store_true",
                        help="Block view: full triad context per project")

    p_list = sub.add_parser("list", help="Local task list; with -g/--global: dashboard")
    p_list.add_argument("-g", "--global", dest="global_dash", action="store_true",
                        help="Global view: all registered projects (same as dashboard)")

    sub.add_parser("tasks", help="Print the local task list to STDOUT")

    p_start = sub.add_parser("start", help="Focus a task")
    p_start.add_argument("task_id", type=int, help="Task ID to focus")

    p_done = sub.add_parser("done", help="Mark task(s) done")
    p_done.add_argument("spec", help="Task ID, range, or list (e.g. 3, 2-4)")

    p_add = sub.add_parser("add", help="Add a new task")
    p_add.add_argument("text", nargs="+", help="Task text")

    sub.add_parser("save", help="Two-step save + local commit")
    sub.add_parser("edit", help="Open PLAN.md in $EDITOR")
    sub.add_parser("log", help="Open HISTORY.md in $EDITOR")
    sub.add_parser("info", help="Show installation diagnostics")

    p_reg = sub.add_parser("register", help="Register a project globally")
    p_reg.add_argument("-n", "--name", default=None,
                       help="Display name (default: folder name)")
    p_reg.add_argument("--path", default=None,
                       help="Project path (default: current directory)")

    p_unreg = sub.add_parser("unregister", help="Unregister a project")
    p_unreg.add_argument("--path", default=None,
                         help="Project path to unregister")
    p_unreg.add_argument("name", nargs="?", default=None,
                         help="Project name to unregister")

    sub.add_parser("prune", help="Drop entries whose folders no longer exist")
    sub.add_parser("install", help=f"Symlink ck to {USER_INSTALL_PATH}")
    sub.add_parser("uninstall", help=f"Remove the {USER_INSTALL_PATH} symlink")
    sub.add_parser("update", help="Self-update via git pull --ff-only")
    sub.add_parser("help", help="Show this help")

    p_dev = sub.add_parser("dev", help="Dev-mode sandbox utilities")
    dev_sub = p_dev.add_subparsers(dest="dev_command", metavar="<action>")
    dev_sub.add_parser("clean", help="Remove the .sandbox/ directory")

    return parser


# Legacy positional parser (for backward compatibility with old ck
# invocations like `ck add foo bar`).
_LEGACY_CHOICES = (
    "init", "st", "dashboard", "start", "done", "add", "save",
    "edit", "log", "install", "uninstall", "update", "help",
    "list", "tasks", "register", "unregister", "prune", "info",
    "dev", "ck-clean",
)

# Flags each legacy command accepts. Anything else starting with "-"
# is reported as an error instead of being silently swallowed into
# task text / ignored.
_LEGACY_FLAGS: dict[str, frozenset] = {
    "init": frozenset({"--register"}),
    "st": frozenset({"--global", "--all"}),
    "dashboard": frozenset({"-v", "--verbose"}),
    "list": frozenset({"-g", "--global"}),
    "tasks": frozenset(),
    "start": frozenset(),
    "done": frozenset(),
    "add": frozenset(),
    "save": frozenset(),
    "edit": frozenset(),
    "log": frozenset(),
    "info": frozenset(),
    "register": frozenset({"-n", "--name", "--path"}),
    "unregister": frozenset({"--path"}),
    "prune": frozenset(),
    "install": frozenset(),
    "uninstall": frozenset(),
    "update": frozenset(),
    "help": frozenset(),
    "dev": frozenset(),
    "ck-clean": frozenset(),
}


def _reject_unknown_flags(cmd: str, rest: List[str]) -> Optional[List[str]]:
    """Validate legacy flags for ``cmd``.

    Returns the argument list with the ``--`` escape marker removed,
    or None (after printing an error) when an unknown flag appears.
    A literal ``--`` marks everything after it as positional text —
    e.g. ``ck add -- --not-a-flag``.
    """
    allowed = _LEGACY_FLAGS.get(cmd, frozenset())
    out: list[str] = []
    positional_only = False
    for tok in rest:
        if positional_only:
            out.append(tok)
            continue
        if tok == "--":
            positional_only = True
            continue
        if tok.startswith("-") and tok != "-":
            if tok not in allowed:
                print(
                    f"\u274c Unknown flag for `ck {cmd}`: {tok}\n"
                    f"   Run `ck --help` for usage."
                )
                return None
            out.append(tok)
        else:
            out.append(tok)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns a process exit code."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or "-h" in raw or "--help" in raw or (raw and raw[0] == "help"):
        print(HELP_TEXT)
        return 0
    # Version only when the FLAG LEADS the invocation: subcommands
    # own their flags (``ck dashboard -v`` is the verbose block view,
    # not a version query).
    if raw and (raw[0] == "-v" or raw[0] == "--version"):
        print(f"ck version {VERSION}")
        return 0

    # Always use legacy positional dispatch (handles all known
    # commands including the global registry ones).
    if raw and raw[0] in _LEGACY_CHOICES:
        return _legacy_dispatch(raw)

    # Otherwise fall through to argparse for forward-compatible flags.
    parser = build_parser()
    try:
        args = parser.parse_args(raw)
    except SystemExit as e:
        return int(e.code) if e.code is not None else 2

    ck = ContextKeeper()
    try:
        if args.command is None or args.command == "help":
            print(HELP_TEXT)
            return 0
        if args.command == "init":
            ck.init(register=args.register)
        elif args.command == "st":
            if getattr(args, "global_dash", False):
                _maybe_notify(ck)
                print(ck.dashboard())
            elif args.all:
                _maybe_notify(ck)
                print(ck.status())
                _print_full_plan(ck)
            else:
                _maybe_notify(ck)
                print(ck.status())
        elif args.command == "dashboard":
            _maybe_notify(ck)
            print(ck.dashboard(verbose=getattr(args, "verbose", False)))
        elif args.command == "list":
            if getattr(args, "global_dash", False):
                _maybe_notify(ck)
                print(ck.dashboard())
            else:
                _maybe_notify(ck)
                print(ck.tasks())
        elif args.command == "tasks":
            _maybe_notify(ck)
            print(ck.tasks())
        elif args.command == "info":
            print(ck.info())
        elif args.command == "start":
            result = ck.start(args.task_id)
            print(f"\u27a4 Focused [{result.task_id}]: {result.title}")
        elif args.command == "done":
            ids = ck.done(args.spec)
            print(f"\u2705 Marked done: {', '.join(map(str, ids))}")
        elif args.command == "add":
            text = " ".join(args.text)
            new_id = ck.add_task(text)
            print(f"\u2705 Added task (id={new_id}): {text}")
        elif args.command == "save":
            ck.save()
        elif args.command == "edit":
            ck.edit_plan()
        elif args.command == "log":
            ck.edit_log()
        elif args.command == "register":
            path = Path(args.path).resolve() if args.path else None
            entry = ck.register(path=path, name=args.name)
            print(f"\u2705 Registered: {entry.name} \u2192 {entry.path}")
        elif args.command == "unregister":
            path = Path(args.path).resolve() if args.path else None
            removed = ck.unregister(path=path, name=args.name)
            if removed:
                print("\u2705 Unregistered.")
            else:
                print("\u2139\ufe0f  Not in registry.")
        elif args.command == "prune":
            _maybe_notify(ck)
            pruned = ck.prune()
            if pruned:
                print(f"\U0001f5d1 Pruned {len(pruned)} missing entries:")
                for p in pruned:
                    print(f"   \u2022 {p}")
            else:
                print("\u2705 Nothing to prune.")
        elif args.command == "install":
            _install_user()
        elif args.command == "uninstall":
            _uninstall_user()
        elif args.command == "update":
            return _do_update(ck)
        elif args.command == "dev":
            return _run_dev_command(getattr(args, "dev_command", None))
        return 0
    except KeyError as e:
        print(f"\u274c {e}")
        return 2
    except ValueError as e:
        print(f"\u274c {e}")
        return 2
    except (LockTimeoutError, RegistryCorruptError) as e:
        # Fail-closed concurrency / data-integrity guards: surface a
        # clean diagnostic instead of an unlocked write or a wipe.
        print(f"\u274c {e}")
        return 3
    except EOFError:
        # Non-interactive stdin (piped / /dev/null / Ctrl-D) on an
        # interactive prompt — abort cleanly instead of a traceback.
        print("\u274c Non-interactive input: command aborted.")
        return 4
    except UnicodeDecodeError as e:
        # Non-UTF-8 PLAN.md / HISTORY.md / registry reads.
        print(f"\u274c Cannot decode file contents as UTF-8: {e}")
        return 4
    except OSError as e:
        # Unreadable files, permission errors, missing editors, …
        print(f"\u274c I/O error: {e}")
        return 4


def _maybe_notify(ck: ContextKeeper) -> None:
    """Run the non-blocking update notifier; swallow all errors."""
    try:
        ck.maybe_notify_update()
    except Exception:
        pass


def _print_full_plan(ck: ContextKeeper) -> None:
    print("\u2550" * 45)
    print(" \U0001f4cb FULL PLAN (PLAN.md)")
    print("\u2550" * 45)
    if ck.plan_file.exists():
        print(ck.plan_file.read_text(encoding="utf-8"))
    else:
        print("\u26a0\ufe0f PLAN.md not found.\n")


def _legacy_dispatch(raw: List[str]) -> int:
    """Dispatch legacy positional invocations (preserves old UX)."""
    cmd, rest = raw[0], raw[1:]
    validated = _reject_unknown_flags(cmd, rest)
    if validated is None:
        return 2
    rest = validated
    ck = ContextKeeper()
    notifiable = cmd in _NOTIFIER_COMMANDS
    try:
        if cmd == "init":
            ck.init(register="--register" in rest)
        elif cmd == "st":
            if notifiable:
                _maybe_notify(ck)
            if "--global" in rest:
                print(ck.dashboard())
            elif "--all" in rest:
                print(ck.status())
                _print_full_plan(ck)
            else:
                print(ck.status())
        elif cmd == "dashboard" or (
            cmd == "list" and ("-g" in rest or "--global" in rest)
        ):
            if notifiable:
                _maybe_notify(ck)
            verbose = cmd == "dashboard" and ("-v" in rest or "--verbose" in rest)
            print(ck.dashboard(verbose=verbose))
        elif cmd == "list":
            if notifiable:
                _maybe_notify(ck)
            print(ck.tasks())
        elif cmd == "tasks":
            if notifiable:
                _maybe_notify(ck)
            print(ck.tasks())
        elif cmd == "info":
            print(ck.info())
        elif cmd == "start":
            if not rest:
                print("\u274c Usage: ck start <ID>")
                return 2
            try:
                tid = int(rest[0])
            except ValueError:
                print(f"\u274c Invalid task ID: {rest[0]!r}")
                return 2
            result = ck.start(tid)
            print(f"\u27a4 Focused [{result.task_id}]: {result.title}")
        elif cmd == "done":
            if not rest:
                print("\u274c Usage: ck done <ID|range|list>")
                return 2
            ids = ck.done(rest[0])
            print(f"\u2705 Marked done: {', '.join(map(str, ids))}")
        elif cmd == "add":
            if not rest:
                print("\u274c Usage: ck add <text>")
                return 2
            text = " ".join(rest)
            new_id = ck.add_task(text)
            print(f"\u2705 Added task (id={new_id}): {text}")
        elif cmd == "save":
            ck.save()
        elif cmd == "edit":
            ck.edit_plan()
        elif cmd == "log":
            ck.edit_log()
        elif cmd == "register":
            name = None
            path = None
            i = 0
            while i < len(rest):
                tok = rest[i]
                if tok in ("-n", "--name") and i + 1 < len(rest):
                    name = rest[i + 1]
                    i += 2
                elif tok == "--path" and i + 1 < len(rest):
                    path = rest[i + 1]
                    i += 2
                else:
                    i += 1
            target = Path(path).resolve() if path else None
            entry = ck.register(path=target, name=name)
            print(f"\u2705 Registered: {entry.name} \u2192 {entry.path}")
        elif cmd == "unregister":
            path = None
            name = None
            for i, tok in enumerate(rest):
                if tok == "--path" and i + 1 < len(rest):
                    path = rest[i + 1]
                elif not tok.startswith("-"):
                    name = tok
            target = Path(path).resolve() if path else None
            removed = ck.unregister(path=target, name=name)
            if removed:
                print("\u2705 Unregistered.")
            else:
                print("\u2139\ufe0f  Not in registry.")
        elif cmd == "prune":
            if notifiable:
                _maybe_notify(ck)
            pruned = ck.prune()
            if pruned:
                print(f"\U0001f5d1 Pruned {len(pruned)} missing entries:")
                for p in pruned:
                    print(f"   \u2022 {p}")
            else:
                print("\u2705 Nothing to prune.")
        elif cmd == "install":
            _install_user()
        elif cmd == "uninstall":
            _uninstall_user()
        elif cmd == "update":
            return _do_update(ck)
        elif cmd in ("dev", "ck-clean"):
            # `ck dev clean`, bare `ck dev` (hint), `ck ck-clean`.
            return _run_dev_command(rest[0] if cmd == "dev" and rest else
                                    ("clean" if cmd == "ck-clean" else None))
        elif cmd == "help":
            print(HELP_TEXT)
        else:
            print(f"Unknown command: {cmd!r}. Use `ck --help`.")
            return 2
        return 0
    except KeyError as e:
        print(f"\u274c {e}")
        return 2
    except ValueError as e:
        print(f"\u274c {e}")
        return 2
    except (LockTimeoutError, RegistryCorruptError) as e:
        print(f"\u274c {e}")
        return 3
    except EOFError:
        # Non-interactive stdin (piped / /dev/null / Ctrl-D) on an
        # interactive prompt — abort cleanly instead of a traceback.
        print("\u274c Non-interactive input: command aborted.")
        return 4
    except UnicodeDecodeError as e:
        print(f"\u274c Cannot decode file contents as UTF-8: {e}")
        return 4
    except OSError as e:
        print(f"\u274c I/O error: {e}")
        return 4


# ---------------------------------------------------------------------- #
# Dev-mode sandbox utilities (ck dev clean / ck-clean)
# ---------------------------------------------------------------------- #

def _run_dev_command(action: Optional[str]) -> int:
    """Dispatch ``ck dev <action>`` / ``ck-clean`` subcommands.

    Returns a process exit code. Unknown or missing actions print a
    usage hint to stdout and return 2 (no tracebacks, no side
    effects).
    """
    from .sandbox import clean_sandbox

    if action == "clean":
        return 0 if clean_sandbox() else 1
    if action is None:
        print("Usage: ck dev clean")
        return 2
    print(f"Unknown dev action: {action!r}. Usage: ck dev clean")
    return 2


def clean_sandbox_entrypoint() -> int:
    """Console-script entrypoint for ``ck-clean`` (pyproject)."""
    return _run_dev_command("clean")


# ---------------------------------------------------------------------- #
# Self-update (git pull --ff-only)
# ---------------------------------------------------------------------- #

def _do_update(ck: ContextKeeper) -> int:
    """Run ``ck update`` end-to-end and translate the result to exit code."""
    result = ck.update()
    if result.ok:
        print(f"\u2705 {result.message}")
        return 0
    # Non-ok: the message is the user-facing diagnostic.
    print(f"\u274c {result.message}")
    return 1 if result.action != "abort" else 1


# ---------------------------------------------------------------------- #
# User-level install (no sudo): symlink to ~/.local/bin/ck
# ---------------------------------------------------------------------- #

def _install_user() -> None:
    """Symlink the running entry point into ``~/.local/bin/ck``."""
    source = Path(sys.argv[0]).resolve() if sys.argv else Path(__file__).resolve()
    if not source.exists():
        # Fallback: locate `ck` relative to the package.
        source = (Path(__file__).resolve().parent.parent / "ck").resolve()
    if not source.exists():
        print(f"\u274c Cannot locate source: {source}")
        return
    target_dir = USER_INSTALL_PATH.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    target = USER_INSTALL_PATH
    if target.exists() or target.is_symlink():
        if target.is_symlink() and target.resolve() == source:
            print(f"\u2705 Already installed: {target} \u2192 {source}")
            return
        if not target.is_symlink():
            # Refuse to clobber a REAL file the user placed there
            # (their own wrapper/binary). Never delete user data
            # silently — this matches the uninstall guard.
            print(
                f"\u26a0\ufe0f  {target} exists and is not a symlink. "
                "Refusing to overwrite it."
            )
            print(
                "       Remove it manually first if you want ck to "
                "own this path."
            )
            return
        try:
            target.unlink()
        except OSError as e:
            print(f"\u274c Could not remove existing {target}: {e}")
            return
    try:
        target.symlink_to(source)
    except OSError as e:
        print(f"\u274c Symlink failed: {e}")
        return
    print(f"\u2705 Installed: {target} \u2192 {source}")
    print(f"\U0001f389 Run: ck --help")


def _uninstall_user() -> None:
    target = USER_INSTALL_PATH
    if not target.exists() and not target.is_symlink():
        print(f"\u2139\ufe0f  Not installed at {target}.")
        return
    if not target.is_symlink():
        print(f"\u26a0\ufe0f  {target} is not a symlink. Refusing to delete.")
        return
    try:
        target.unlink()
        print(f"\u2705 Removed symlink: {target}")
    except OSError as e:
        print(f"\u274c Could not remove {target}: {e}")


if __name__ == "__main__":
    sys.exit(main())
