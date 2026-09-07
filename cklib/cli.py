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

Core commands:
  init                       Initialize .ck/ in the current project
  st [--all|--global]        Status of the current project (--global: dashboard)
  dashboard                  Cross-project dashboard table
  list [-g]                  List registered projects (-g same as dashboard)
  start <ID>                 Mark task ID as focused ([>])
  done <ID|Range>            Mark task(s) as done ([x])
  add <text>                 Insert a new open task before ## Completed
  save                       Two-step save: optional note, then local commit
  edit                       Open PLAN.md in $EDITOR
  log                        Open HISTORY.md in $EDITOR
  info                       Diagnostic information about this installation

Global registry:
  register   [-n NAME] [--path PATH]  Add a project to the global registry
  unregister [--path PATH | NAME]     Remove a project from the registry
  prune                              Drop entries whose folders no longer exist

System commands:
  install                    Symlink ck to {USER_INSTALL_PATH}
  uninstall                  Remove the {USER_INSTALL_PATH} symlink
  update                     git fetch + git pull --ff-only origin main

Options:
  -v, --version              Show version
  -h, --help                 Show this help
"""


# Commands that may print a non-blocking update notice to stderr.
_NOTIFIER_COMMANDS = frozenset({
    "st", "status", "dashboard", "list", "info", "prune",
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

    sub.add_parser("init", help="Initialize .ck/ in the current project")

    p_st = sub.add_parser("st", help="Project status")
    p_st.add_argument("--all", action="store_true", help="Include full PLAN.md")
    p_st.add_argument("--global", dest="global_dash", action="store_true",
                      help="Show global dashboard instead")

    sub.add_parser("dashboard", help="Cross-project dashboard table")

    p_list = sub.add_parser("list", help="List registered projects")
    p_list.add_argument("-g", "--global", dest="global_dash", action="store_true",
                        help="Render as a table (alias for dashboard)")

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

    return parser


# Legacy positional parser (for backward compatibility with old ck
# invocations like `ck add foo bar`).
_LEGACY_CHOICES = (
    "init", "st", "dashboard", "start", "done", "add", "save",
    "edit", "log", "install", "uninstall", "update", "help",
    "list", "register", "unregister", "prune", "info",
)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns a process exit code."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or "-h" in raw or "--help" in raw or (raw and raw[0] == "help"):
        print(HELP_TEXT)
        return 0
    if "-v" in raw or "--version" in raw:
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
            ck.init()
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
            print(ck.dashboard())
        elif args.command == "list":
            _maybe_notify(ck)
            print(ck.dashboard())
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
    ck = ContextKeeper()
    notifiable = cmd in _NOTIFIER_COMMANDS
    try:
        if cmd == "init":
            ck.init()
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
        elif cmd == "dashboard" or (cmd == "list" and "-g" in rest):
            if notifiable:
                _maybe_notify(ck)
            print(ck.dashboard())
        elif cmd == "list":
            if notifiable:
                _maybe_notify(ck)
            print(ck.dashboard())
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