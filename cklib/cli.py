"""Command-line entry point and dispatcher for Context Keeper."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from . import ui
from .config import (
    LockTimeoutError,
    SNAPSHOT_INSTALL_DIR,
    USER_INSTALL_PATH,
    VERSION,
    find_project_root,
)
from .core import ContextKeeper, UpdateResult
from .registry import RegistryCorruptError
from .sandbox import (
    SANDBOX_ACTIVE_ENV,
    active_sandbox_project,
    dev_context_active,
    emit_interceptor_banner_if_needed,
    enter_sandbox,
    exit_sandbox,
    is_dev_entrypoint,
    reject_dev_exit,
)


HELP_TEXT = f"""Context Keeper CLI [v{VERSION}]

Usage: ck <command> [args]

Local (current project):
  st [-l|-e|-g]              Status of current project (-l: list tasks,
                             -e: edit plan, -g: global dashboard)
  st --all                   Also print the full PLAN.md
  list                       Print task list to STDOUT
  init                       Initialize .ck/ locally (use --register to register globally)
  start <ID>                 Mark task ID as focused ([>])
  done <ID|Range>            Mark task(s) as done ([x])
  add <text>                 Insert a new open task before ## Completed
  note <text>                Attach/update a process note on the active task
                             (shown in `ck st`, cleared by `ck done`)
  save                       Two-step save: optional note, then local commit
  edit                       Open PLAN.md in your editor
  log                        Open HISTORY.md in your editor
  info                       Diagnostic information about this installation

Global (all registered projects):
  dashboard                  Cross-project dashboard (compact table)
  dashboard -v               Detailed block view: full PREV/FOCUS/NEXT
                             triad context per project
  register   [-n NAME] [--path PATH]  Add a project to the global registry
  unregister [--path PATH | NAME]     Remove a project from the registry
  prune                              Drop entries whose folders no longer exist

Editor resolution (ck edit / ck log):
  1. "editor" key in .ck.json at the project root
  2. $VISUAL
  3. $EDITOR
  4. nano (if installed), else vi

Color overrides ("colors" keys in .ck.json, optional):
  text, muted, border, accent -> "none" (native), "cyan", "bold blue",
  "bright-black", or raw SGR ("38;5;208"); unset keys inherit your
  terminal's native text color

System commands:
  install                    Copy the ck launcher to {USER_INSTALL_PATH}
                             (physical executable file, never a symlink) and
                             snapshot cklib/ to {SNAPSHOT_INSTALL_DIR}:
                             production is a static snapshot refreshed only
                             by an explicit `ck install` / `ck update`
  install --dev              Deprecated: no global dev entrypoint is
                             created. Dev mode runs from the checkout via
                             local binary execution: ./ck-dev
  uninstall                  Remove {USER_INSTALL_PATH}, the cklib
                             snapshot and any legacy ~/.local/bin/ck-dev
  update                     git fetch + git pull --ff-only origin main
                             (an existing installation is refreshed after
                             a successful pull)

Dev mode (ck-dev / CK_SANDBOX=1 / --sandbox):
  ./ck-dev                   Enter sandbox mode: sets up .sandbox/ if needed,
                             prints the sandbox warning banner and Global
                             Dashboard, then opens an interactive subshell.
                             To exit: type 'exit' or press Ctrl+D.
  ck dev setup               Build an isolated mock environment in .sandbox/
  ck dev clean               Remove the .sandbox/ directory (alias: ck sandbox clean)
  sandbox setup              Build an isolated mock environment in .sandbox/
                             (registered/unregistered/missing projects, bulk
                             history + archive data)
  sandbox clean              Remove the .sandbox/ directory (same as ck-clean)

Inside a sandbox session, plain `ck` prints the sandbox warning banner
before transparently executing in sandbox mode.

Inside a sandbox session, plain `ck` prints the sandbox warning banner
before transparently executing in sandbox mode.

Options:
  -v, --version              Show version
  -h, --help                 Show this help
  -l, --list                 Shorthand for `ck st -l` (list tasks)
  -e, --edit                 Shorthand for `ck st -e` (edit plan)
  -g, --global               Shorthand for `ck st -g` (global dashboard)
"""


# Commands that may print a non-blocking update notice to stderr.
# MUTATIVE flows only (explicit user-initiated state changes):
# read-only / diagnostic commands (st, list, dashboard, info, log,
# help, version) must answer instantly — no network probes, no
# git subprocesses on their execution path.
_NOTIFIER_COMMANDS = frozenset({"done", "save"})

# Commands that REQUIRE a resolvable local project root: all mutative
# flows plus the editor launches. Global commands (dashboard / register
# / unregister / prune) operate purely on registry state, and the read
# commands (st / list) fall back to the global registry tier by design
# — all three groups stay usable even when the working directory is
# dangling (the keeper is rootless there, not broken).
_LOCAL_ONLY_COMMANDS = frozenset({
    "init", "start", "done", "add", "note", "save", "edit", "log",
})

# User-facing message shown when a local command runs from a working
# directory whose descriptor is gone (rootless keeper).
_NO_ROOT_MESSAGE = (
    "[!] Not in a valid project directory. "
    "cd into your project (or use a global command: ck st -g)."
)


def build_parser() -> "argparse.ArgumentParser":
    """Build the top-level argument parser with subcommands.

    ``argparse`` is imported lazily: the legacy positional dispatch
    (the hot path for every known command) never needs it, keeping
    read-only invocations startup-light.
    """
    import argparse

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

    p_st = sub.add_parser(
        "st",
        help="Status of current project (-l: list tasks, -e: edit plan, "
             "-g: global dashboard)",
    )
    p_st.add_argument("-l", "--list", dest="list_tasks", action="store_true",
                      help="Print the task list (same as `ck list`)")
    p_st.add_argument("-e", "--edit", dest="edit", action="store_true",
                      help="Open PLAN.md in your editor (same as `ck edit`)")
    p_st.add_argument("-g", "--global", dest="global_dash", action="store_true",
                      help="Show the global dashboard (same as `ck dashboard`)")
    p_st.add_argument("--all", action="store_true", help="Include full PLAN.md")

    p_dash = sub.add_parser(
        "dashboard",
        help="Cross-project dashboard (compact table; -v for block view)",
    )
    p_dash.add_argument("-v", "--verbose", dest="verbose", action="store_true",
                        help="Block view: full triad context per project")

    sub.add_parser("list", help="Print the task list to STDOUT")

    p_start = sub.add_parser("start", help="Focus a task")
    p_start.add_argument("task_id", type=int, help="Task ID to focus")

    p_done = sub.add_parser("done", help="Mark task(s) done")
    p_done.add_argument("spec", help="Task ID, range, or list (e.g. 3, 2-4)")

    p_add = sub.add_parser("add", help="Add a new task")
    p_add.add_argument("text", nargs="+", help="Task text")

    p_note = sub.add_parser(
        "note", help="Attach a process note to the active task")
    p_note.add_argument("text", nargs="+", help="Note text")

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
    p_install = sub.add_parser(
        "install",
        help=f"Copy ck to {USER_INSTALL_PATH} "
             "(physical copy, no symlink)")
    p_install.add_argument(
        "--dev", action="store_true",
        help="Deprecated: dev mode runs via ./ck-dev from the checkout "
             "(no global dev entrypoint is installed)",
    )
    sub.add_parser(
        "uninstall",
        help="Remove the installed ck copy and legacy dev entrypoint")
    sub.add_parser("update", help="Self-update via git pull --ff-only")
    sub.add_parser("help", help="Show this help")
    sub.add_parser(
        "exit",
        help="Dev entrypoint only: warns that a top-level exit cannot "
             "close an active subshell (exit code 1)")

    p_dev = sub.add_parser("dev", help="Dev-mode sandbox utilities")
    dev_sub = p_dev.add_subparsers(dest="dev_command", metavar="<action>")
    dev_sub.add_parser(
        "setup",
        help="Build an isolated mock environment in .sandbox/ "
             "(bulk test data generation)",
    )
    dev_sub.add_parser("clean", help="Remove the .sandbox/ directory")

    p_sandbox = sub.add_parser(
        "sandbox",
        help="Sandbox environment utilities (setup/clean)",
    )
    sandbox_sub = p_sandbox.add_subparsers(
        dest="sandbox_action", metavar="<action>")
    sandbox_sub.add_parser(
        "setup",
        help="Build an isolated mock environment in .sandbox/ "
             "(bulk test data generation)",
    )
    sandbox_sub.add_parser(
        "clean", help="Remove the .sandbox/ directory",
    )

    return parser


# Legacy positional parser (for backward compatibility with old ck
# invocations like `ck add foo bar`).
_LEGACY_CHOICES = (
    "init", "st", "dashboard", "start", "done", "add", "note", "save",
    "edit", "log", "install", "uninstall", "update", "help",
    "list", "register", "unregister", "prune", "info",
    "dev", "sandbox", "ck-clean",
)

# Top-level shorthands for the ``st`` view flags: ``ck -l`` is an
# exact synonym for ``ck st -l``, ``ck -e`` for ``ck st -e`` and
# ``ck -g`` for ``ck st -g``. They are TRANSLATED to the canonical
# form (never dispatched separately) so both spellings always share
# one code path and cannot drift apart.
_STATUS_SHORTCUTS = frozenset({"-l", "--list", "-e", "--edit",
                               "-g", "--global"})

# Flags each legacy command accepts. Anything else starting with "-"
# is reported as an error instead of being silently swallowed into
# task text / ignored.
_LEGACY_FLAGS: dict[str, frozenset] = {
    "init": frozenset({"--register"}),
    "st": frozenset({"-l", "--list", "-e", "--edit", "-g", "--global",
                     "--all"}),
    "dashboard": frozenset({"-v", "--verbose"}),
    "list": frozenset(),
    "start": frozenset(),
    "done": frozenset(),
    "add": frozenset(),
    "note": frozenset(),
    "save": frozenset(),
    "edit": frozenset(),
    "log": frozenset(),
    "info": frozenset(),
    "register": frozenset({"-n", "--name", "--path"}),
    "unregister": frozenset({"--path"}),
    "prune": frozenset(),
    "install": frozenset({"--dev"}),
    "uninstall": frozenset(),
    "update": frozenset(),
    "help": frozenset(),
    "dev": frozenset(),
    "sandbox": frozenset(),
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
                _print_error(
                    f"ERROR: Unknown flag for `ck {cmd}`: {tok}\n"
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
    argv0 = sys.argv[0] if sys.argv else ""
    is_dev_call = Path(argv0).name.lower() == "ck-dev"
    # Bare `ck-dev`: explicit sandbox ENTRY (help stays plain-`ck`).
    if not raw:
        if is_dev_call:
            return enter_sandbox()
        print(HELP_TEXT)
        return 0
    if raw[0] == "exit" and is_dev_call:
        # SAFEGUARD: `ck-dev exit` cannot close an active subshell.
        # Warn (with the real entrypoint name) and exit 1 without
        # launching a new subshell; the real exit is typing `exit` /
        # Ctrl+D inside the session subshell.
        return reject_dev_exit([argv0, *raw])
    # Sandbox interceptor: plain `ck` inside a dev-mode session warns
    # with the sandbox banner first, then executes transparently.
    # Help/version flows stay banner-free.
    if "-h" in raw or "--help" in raw or raw[0] == "help":
        print(HELP_TEXT)
        return 0
    if raw[0] == "-v" or raw[0] == "--version":
        print(f"ck version {VERSION}")
        return 0
    if not is_dev_call and dev_context_active():
        emit_interceptor_banner_if_needed(
            argv=[Path(argv0).name if argv0 else "ck", *raw])
    if raw[0] == "exit":
        # Plain `ck exit` is not a command; the dev entrypoint was
        # handled above.
        print("Unknown command: 'exit'. Use `ck --help`.")
        return 2

    # TOP-LEVEL STATUS SHORTCUTS: `ck -l` / `-e` / `-g` are exact
    # synonyms for the documented `ck st -l` / `-e` / `-g`. Rewrite
    # to the canonical form so legacy dispatch (and its flag
    # validation) handles them identically.
    if raw[0] in _STATUS_SHORTCUTS:
        raw = ["st", *raw]

    # DANGLING WORKING DIRECTORY: when the cwd's descriptor is gone
    # (wiped sandbox, external rebuild), a rootless keeper can still
    # serve the GLOBAL registry commands and global-fallback reads —
    # but the commands above have no project to operate on and must
    # fail with a clean, actionable message instead of a traceback.
    if raw[0] in _LOCAL_ONLY_COMMANDS and find_project_root() is None:
        print(_NO_ROOT_MESSAGE)
        return 1

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
            if args.list_tasks:
                print(ck.tasks())
            elif args.edit:
                ck.edit_plan()
            elif getattr(args, "global_dash", False):
                print(ck.dashboard())
            elif args.all:
                print(ck.status())
                _print_full_plan(ck)
            else:
                print(ck.status())
        elif args.command == "dashboard":
            print(ck.dashboard(verbose=getattr(args, "verbose", False)))
        elif args.command == "list":
            print(ck.tasks())
        elif args.command == "info":
            print(ck.info())
        elif args.command == "start":
            result = ck.start(args.task_id)
            print(f"-> Focused [{result.task_id}]: {result.title}")
        elif args.command == "done":
            ids = ck.done(args.spec)
            print(f"[ok] Marked done: {', '.join(map(str, ids))}")
            _maybe_notify(ck)
        elif args.command == "add":
            text = " ".join(args.text)
            new_id = ck.add_task(text)
            print(f"[ok] Added task (id={new_id}): {text}")
        elif args.command == "note":
            text = " ".join(args.text)
            info = ck.set_note(text)
            print(f"* Note saved for [{info['id']}]: {info['note']}")
        elif args.command == "save":
            ck.save()
            _maybe_notify(ck)
        elif args.command == "edit":
            ck.edit_plan()
        elif args.command == "log":
            ck.edit_log()
        elif args.command == "register":
            path = Path(args.path).resolve() if args.path else None
            entry = ck.register(path=path, name=args.name)
            print(f"[ok] Registered: {entry.name} -> {entry.path}")
        elif args.command == "unregister":
            path = Path(args.path).resolve() if args.path else None
            removed = ck.unregister(path=path, name=args.name)
            if removed:
                print("[ok] Unregistered.")
            else:
                print("[i] Not in registry.")
        elif args.command == "prune":
            pruned = ck.prune()
            if pruned:
                print(f"Pruned {len(pruned)} missing entries:")
                for p in pruned:
                    print(f"   * {p}")
                print(
                    f"[ok] Summary: {len(pruned)} project(s) purged "
                    "from the global registry."
                )
            else:
                print("[ok] Nothing to prune.")
        elif args.command == "install":
            _install_user(
                dev=getattr(args, "dev", False) or is_dev_entrypoint())
        elif args.command == "uninstall":
            _uninstall_user()
        elif args.command == "update":
            return _do_update(ck)
        elif args.command == "dev":
            return _run_dev_command(getattr(args, "dev_command", None))
        elif args.command == "sandbox":
            return _run_sandbox_command(
                getattr(args, "sandbox_action", None))
        return 0
    except KeyError as e:
        _print_error(f"ERROR: {e}")
        return 2
    except ValueError as e:
        _print_error(f"ERROR: {e}")
        return 2
    except (LockTimeoutError, RegistryCorruptError) as e:
        # Fail-closed concurrency / data-integrity guards: surface a
        # clean diagnostic instead of an unlocked write or a wipe.
        _print_error(f"ERROR: {e}")
        return 3
    except EOFError:
        # Non-interactive stdin (piped / /dev/null / Ctrl-D) on an
        # interactive prompt — abort cleanly instead of a traceback.
        _print_error("ERROR: Non-interactive input: command aborted.")
        return 4
    except UnicodeDecodeError as e:
        # Non-UTF-8 PLAN.md / HISTORY.md / registry reads.
        _print_error(f"ERROR: Cannot decode file contents as UTF-8: {e}")
        return 4
    except OSError as e:
        # Unreadable files, permission errors, missing editors, …
        _print_error(f"ERROR: I/O error: {e}")
        return 4


def _maybe_notify(ck: ContextKeeper) -> None:
    """Run the non-blocking update notifier; swallow all errors.

    Only invoked after explicit mutative flows (``done``, ``save``) —
    never on read-only commands, which must stay latency-free.
    """
    try:
        ck.maybe_notify_update()
    except Exception:
        pass


def _print_error(message: str) -> None:
    """Print an ERROR line, in standard red when colors are enabled.

    Contrast policy: errors use the high-visibility ANSI RED
    badge, never DIM/dark-gray. With colors off (NO_COLOR, pipes)
    the output is byte-identical to a plain ``print``.
    """
    print(ui.get_palette().red(message))


def _print_full_plan(ck: ContextKeeper) -> None:
    print("=" * 45)
    print(" FULL PLAN (PLAN.md)")
    print("=" * 45)
    # Follows the same context resolution as the status block above:
    # local → ancestors (up to the Git repo root) → global registry.
    text = ck.full_plan_text()
    if text is None:
        print("[!] PLAN.md not found.\n")
    else:
        print(text)


def _legacy_dispatch(raw: List[str]) -> int:
    """Dispatch legacy positional invocations (preserves old UX)."""
    cmd, rest = raw[0], raw[1:]
    validated = _reject_unknown_flags(cmd, rest)
    if validated is None:
        return 2
    rest = validated
    # Inside a sandbox session, plain `ck` re-exports the active
    # project for every delegated subcommand (nested `ck` calls and
    # subprocesses banner against the same project without touching
    # the state file).
    if dev_context_active():
        active = active_sandbox_project()
        if active is not None:
            os.environ[SANDBOX_ACTIVE_ENV] = str(active)
    ck = ContextKeeper()
    notifiable = cmd in _NOTIFIER_COMMANDS
    try:
        if cmd == "init":
            ck.init(register="--register" in rest)
        elif cmd == "st":
            if "-l" in rest or "--list" in rest:
                print(ck.tasks())
            elif "-e" in rest or "--edit" in rest:
                ck.edit_plan()
            elif "-g" in rest or "--global" in rest:
                print(ck.dashboard())
            elif "--all" in rest:
                print(ck.status())
                _print_full_plan(ck)
            else:
                print(ck.status())
        elif cmd == "dashboard":
            verbose = "-v" in rest or "--verbose" in rest
            print(ck.dashboard(verbose=verbose))
        elif cmd == "list":
            print(ck.tasks())
        elif cmd == "info":
            print(ck.info())
        elif cmd == "start":
            if not rest:
                print("Usage: ck start <ID>")
                return 2
            try:
                tid = int(rest[0])
            except ValueError:
                _print_error(f"ERROR: Invalid task ID: {rest[0]!r}")
                return 2
            result = ck.start(tid)
            print(f"-> Focused [{result.task_id}]: {result.title}")
        elif cmd == "done":
            if not rest:
                print("Usage: ck done <ID|range|list>")
                return 2
            ids = ck.done(rest[0])
            print(f"[ok] Marked done: {', '.join(map(str, ids))}")
            if notifiable:
                _maybe_notify(ck)
        elif cmd == "add":
            if not rest:
                print("Usage: ck add <text>")
                return 2
            text = " ".join(rest)
            new_id = ck.add_task(text)
            print(f"[ok] Added task (id={new_id}): {text}")
        elif cmd == "note":
            if not rest:
                print("Usage: ck note <text>")
                return 2
            text = " ".join(rest)
            info = ck.set_note(text)
            print(f"* Note saved for [{info['id']}]: {info['note']}")
        elif cmd == "save":
            ck.save()
            if notifiable:
                _maybe_notify(ck)
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
            print(f"[ok] Registered: {entry.name} -> {entry.path}")
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
                print("[ok] Unregistered.")
            else:
                print("[i] Not in registry.")
        elif cmd == "prune":
            pruned = ck.prune()
            if pruned:
                print(f"Pruned {len(pruned)} missing entries:")
                for p in pruned:
                    print(f"   * {p}")
                print(
                    f"[ok] Summary: {len(pruned)} project(s) purged "
                    "from the global registry."
                )
            else:
                print("[ok] Nothing to prune.")
        elif cmd == "install":
            _install_user(dev="--dev" in rest or is_dev_entrypoint())
        elif cmd == "uninstall":
            _uninstall_user()
        elif cmd == "update":
            return _do_update(ck)
        elif cmd in ("dev", "ck-clean"):
            # `ck dev clean`, bare `ck dev` (hint), `ck ck-clean`.
            return _run_dev_command(rest[0] if cmd == "dev" and rest else
                                    ("clean" if cmd == "ck-clean" else None))
        elif cmd == "sandbox":
            # `ck sandbox setup` / `ck sandbox clean` (ck-dev entry).
            return _run_sandbox_command(rest[0] if rest else None)
        elif cmd == "exit":
            # Only the dev entrypoint owns `exit`; plain `ck exit` is
            # rejected earlier in main() before dispatch.
            return exit_sandbox()
        elif cmd == "help":
            print(HELP_TEXT)
        else:
            print(f"Unknown command: {cmd!r}. Use `ck --help`.")
            return 2
        return 0
    except KeyError as e:
        _print_error(f"ERROR: {e}")
        return 2
    except ValueError as e:
        _print_error(f"ERROR: {e}")
        return 2
    except (LockTimeoutError, RegistryCorruptError) as e:
        _print_error(f"ERROR: {e}")
        return 3
    except EOFError:
        # Non-interactive stdin (piped / /dev/null / Ctrl-D) on an
        # interactive prompt — abort cleanly instead of a traceback.
        _print_error("ERROR: Non-interactive input: command aborted.")
        return 4
    except UnicodeDecodeError as e:
        _print_error(f"ERROR: Cannot decode file contents as UTF-8: {e}")
        return 4
    except OSError as e:
        _print_error(f"ERROR: I/O error: {e}")
        return 4


# ---------------------------------------------------------------------- #
# Dev-mode sandbox utilities (ck dev clean / ck-clean)
# ---------------------------------------------------------------------- #

def _run_dev_command(action: Optional[str]) -> int:
    """Dispatch ``ck dev <action>`` / ``ck-clean`` subcommands.

    - ``setup``: build the isolated mock environment in ``.sandbox/``
      (same as ``ck sandbox setup``).
    - ``clean``: safely remove ``.sandbox/`` (same as ``ck-clean``).

    Returns a process exit code. Unknown or missing actions print a
    usage hint to stdout and return 2 (no tracebacks, no side
    effects).
    """
    from .sandbox import clean_sandbox
    from .sandbox_setup import setup_sandbox

    if action == "setup":
        return 0 if setup_sandbox() else 1
    if action == "clean":
        return 0 if clean_sandbox() else 1
    if action is None:
        print("Usage: ck dev <setup|clean>")
        return 2
    print(
        f"Unknown dev action: {action!r}. Usage: ck dev <setup|clean>"
    )
    return 2


def clean_sandbox_entrypoint() -> int:
    """Console-script entrypoint for ``ck-clean`` (pyproject)."""
    return _run_dev_command("clean")


# ---------------------------------------------------------------------- #
# Sandbox environment utilities (ck sandbox setup / clean)
# ---------------------------------------------------------------------- #

def _run_sandbox_command(action: Optional[str]) -> int:
    """Dispatch ``ck sandbox <action>`` subcommands.

    - ``setup``: build the isolated mock environment in ``.sandbox/``
      (bulk test data generation — registered/unregistered/missing
      projects, 100+ history entries, mock archives).
    - ``clean``: safely remove ``.sandbox/`` (same as ``ck-clean``).

    Returns a process exit code. Unknown or missing actions print a
    usage hint to stdout and return 2 (no tracebacks, no side
    effects — mirrors ``ck dev``).
    """
    from .sandbox import clean_sandbox
    from .sandbox_setup import setup_sandbox

    if action == "setup":
        return 0 if setup_sandbox() else 1
    if action == "clean":
        return 0 if clean_sandbox() else 1
    if action is None:
        print("Usage: ck sandbox <setup|clean>")
        return 2
    print(
        f"Unknown sandbox action: {action!r}. "
        "Usage: ck sandbox <setup|clean>"
    )
    return 2


# ---------------------------------------------------------------------- #
# Self-update (git pull --ff-only)
# ---------------------------------------------------------------------- #

def _do_update(ck: ContextKeeper) -> int:
    """Run ``ck update`` end-to-end and translate the result to exit code."""
    result = ck.update()
    if result.ok:
        print(f"[ok] {result.message}")
        _refresh_installed_copy()
        return 0
    # Non-ok: the message is the user-facing diagnostic.
    _print_error(f"ERROR: {result.message}")
    return 1 if result.action != "abort" else 1


def _refresh_installed_copy() -> None:
    """Re-snapshot an existing production install after ``ck update``.

    Static-snapshot contract: production changes ONLY on an explicit
    ``ck install`` / ``ck update``. When nothing is installed the
    update stays a pure checkout pull — no surprise installation.
    Best-effort: a refresh failure keeps the update's success status.
    """
    target = _user_bin_dir() / "ck"
    if not (target.exists() or target.is_symlink()):
        return
    source = _resolve_install_source("ck")
    if source is None:
        print(
            "[!] An installed copy exists but no launcher source was "
            "found; run `ck install` from the checkout to refresh it."
        )
        return
    print("[i] Refreshing installed production copy ...")
    _install_physical_copy(source, target)


# ---------------------------------------------------------------------- #
# User-level install (no sudo): physical binary copy in ~/.local/bin   #
# ---------------------------------------------------------------------- #

_DEV_SCRIPT_NAME = "ck-dev"


def _user_bin_dir() -> Path:
    """The user bin directory that owns the production ``ck`` copy.

    ``$USER_BIN`` overrides the default (``~/.local/bin``).
    """
    env = os.environ.get("USER_BIN", "").strip()
    if env:
        return Path(env).expanduser()
    return USER_INSTALL_PATH.parent


def _resolve_install_source(script_name: str) -> Optional[Path]:
    """Locate the repo script (``ck``) to physically install.

    Resolution order:

    1. ``$0`` when its basename matches ``script_name`` — handles
       ``./ck install`` and invocations through an existing symlink
       or the installed copy itself (``resolve()`` follows the link
       back to the real file).
    2. The script bundled next to the ``cklib`` package
       (``$REPO_ROOT/<script_name>``).

    Returns None when neither exists (e.g. a pip installation
    without a checkout): the caller reports a clean error.
    """
    if sys.argv:
        candidate = Path(sys.argv[0]).resolve()
        if candidate.name == script_name and candidate.exists():
            return candidate
    bundled = Path(__file__).resolve().parent.parent / script_name
    if bundled.exists():
        return bundled.resolve()
    return None


def _install_user(dev: bool = False) -> None:
    """Install production ``ck`` as a PHYSICAL copy (never a symlink).

    ``dev=False`` (default) performs the production install: the
    launcher is copied byte-for-byte to ``$USER_BIN/ck`` (0755) and
    the running ``cklib`` package is snapshotted to
    ``SNAPSHOT_INSTALL_DIR`` so the installed binary is a STANDALONE
    static snapshot — editing the checkout never changes its behavior
    until ``ck install`` / ``ck update`` explicitly re-runs.

    ``dev=True`` (``ck install --dev`` or the ``ck-dev`` entrypoint)
    is DEPRECATED: global dev entrypoints are purged. Dev mode runs
    strictly via local binary execution from the checkout (or sandbox
    environment): ``./ck-dev``. Nothing is installed.
    """
    if dev:
        print(
            "[!] Deprecated: `ck install --dev` no longer creates "
            "~/.local/bin/ck-dev."
        )
        print(
            "    Dev mode runs via local binary execution from the "
            "checkout: ./ck-dev <command>"
        )
        print("    (no global dev entrypoint is installed).")
        print("    Production install (physical copy): ./ck install")
        return
    source = _resolve_install_source("ck")
    if source is None:
        _print_error("ERROR: Cannot locate source: ck")
        return
    _install_physical_copy(source, _user_bin_dir() / "ck")


def _install_physical_copy(source: Path, target: Path) -> None:
    """Physically copy the launcher and snapshot ``cklib`` (production).

    Production isolation contract:

    - ``target`` becomes a REGULAR executable file (0755) — never a
      symlink — so production is a static snapshot decoupled from the
      checkout.
    - ANY existing entry at ``target`` (a symlink left by an older
      install, a stale copy) is force-removed first (``rm -f`` /
      ``unlink``): copying through a surviving symlink would write
      THROUGH it into the checkout and silently re-couple production
      to development sources.
    - The running ``cklib`` package is snapshotted to
      ``SNAPSHOT_INSTALL_DIR/cklib`` (bytecode caches excluded); the
      copied launcher resolves that snapshot when no ``cklib`` sits
      next to it (installed-snapshot fallback in the ``ck`` launcher).
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    # Read the launcher payload BEFORE touching the target: the source
    # may BE the target (self-reinstall through the installed copy),
    # so the copy must not race its own removal.
    try:
        payload = source.read_bytes()
    except OSError as e:
        _print_error(f"ERROR: Cannot read launcher {source}: {e}")
        return

    # 1. Snapshot the running cklib package (static production copy).
    snapshot = SNAPSHOT_INSTALL_DIR
    try:
        if snapshot.is_dir():
            shutil.rmtree(snapshot)
        elif snapshot.exists() or snapshot.is_symlink():
            snapshot.unlink()
        snapshot.mkdir(parents=True)
        shutil.copytree(
            Path(__file__).resolve().parent,  # the running cklib/
            snapshot / "cklib",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copy2(source, snapshot / source.name)
    except OSError as e:
        _print_error(
            f"ERROR: Could not write package snapshot {snapshot}: {e}"
        )
        return

    # 2. Force-remove any existing entry (symlink or file) at the
    #    target — never copy through a link.
    try:
        if target.is_symlink() or target.exists():
            target.unlink()
    except OSError as e:
        _print_error(f"ERROR: Could not remove existing {target}: {e}")
        return

    # 3. Write the physical copy (staged + atomic replace) as 0755.
    staged = target.with_name(target.name + ".new")
    try:
        staged.write_bytes(payload)
        staged.chmod(0o755)
        os.replace(staged, target)
    except OSError as e:
        _print_error(f"ERROR: Could not write {target}: {e}")
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        return

    print(f"[ok] Installed (physical copy): {target}")
    print(f"[ok] Package snapshot: {snapshot / 'cklib'}")
    print(
        "Production is a static snapshot: re-run `ck install` (or "
        "`ck update`) after changing the checkout."
    )
    print("Run: ck --help")


def _uninstall_user() -> None:
    """Remove the user-level production install and dev leftovers.

    ``ck uninstall`` tears down the whole user installation:

    - ``$USER_BIN/ck`` — the physical copy written by ``ck install``
      (regular file), or a legacy symlink from older versions;
    - ``$USER_BIN/ck-dev`` — the legacy dev entrypoint created by the
      removed ``ck install --dev`` (purged when present);
    - the ``cklib`` package snapshot directory.

    Directories at the bin paths are refused (never deleted); missing
    paths are reported gracefully.
    """
    bin_dir = _user_bin_dir()
    targets = (bin_dir / "ck", bin_dir / _DEV_SCRIPT_NAME)
    for target in targets:
        if not target.exists() and not target.is_symlink():
            print(f"[i] Not installed at {target}.")
            continue
        if target.is_dir() and not target.is_symlink():
            print(f"[!] {target} is a directory. Refusing to delete.")
            continue
        try:
            target.unlink()
            print(f"[ok] Removed: {target}")
        except OSError as e:
            _print_error(f"ERROR: Could not remove {target}: {e}")
    _remove_package_snapshot()


def _remove_package_snapshot() -> None:
    """Best-effort removal of the ``cklib`` snapshot directory."""
    snapshot = SNAPSHOT_INSTALL_DIR
    if not (snapshot / "cklib" / "__init__.py").is_file():
        return
    try:
        shutil.rmtree(snapshot)
        print(f"[ok] Removed package snapshot: {snapshot}")
    except OSError as e:
        _print_error(f"ERROR: Could not remove {snapshot}: {e}")


if __name__ == "__main__":
    sys.exit(main())
