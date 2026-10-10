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
from .models import CompletedTaskError
from .registry import RegistryCorruptError
from .sandbox import (
    SANDBOX_ACTIVE_ENV,
    active_sandbox_project,
    dev_context_active,
    emit_interceptor_banner_if_needed,
    enter_sandbox,
    exit_sandbox,
    is_dev_entrypoint,
    is_dev_mode,
    reject_dev_exit,
)


HELP_TEXT = f"""Context Keeper CLI [v{VERSION}]

Usage: ck <command> [args]

Local (current project):
  st [-l|-e|-g]              Status of current project (-l: list tasks,
                             -e: edit plan, -g: global dashboard)
  st <project_name>          Status of a REGISTERED project, from any
                             directory (same targeting rules as
                             `ck list <project_name>`); an unknown
                             name exits 1. With no argument OUTSIDE
                             every project, `ck st` never guesses:
                             it renders the single nested project or
                             lists the available ones
  st --all                   Also print the full PLAN.md
  list                       Print task list to STDOUT
  list <project_name>        Print the task list of a REGISTERED
                             project from any directory; an unknown
                             name exits 1. With no argument OUTSIDE
                             every project, `ck list` never guesses:
                             it names the single nested project in the
                             header or lists the available ones
  init                       Initialize .ck/ locally (use --register to register globally)
  start <ID>                 Mark task ID as focused ([>]); 0 resets focus.
                             A noted task that loses focus moves to
                             Unfocused / Paused Context (its note travels
                             with it); a noteless one prompts for a note
                             (TTY) or gets a soft hint. Flags: --no-input
                             (never prompt), -y (assume yes, ask text only)
  done <ID|Range>            Mark task(s) as done ([x]). Bare `ck done`
                             completes the CURRENT FOCUS; without a
                             focus it prints the usage line instead
  move <ID> <pos|top|bottom> Move a PENDING/ACTIVE task ([ ] or [>])
                             to position N among the active tasks, or
                             to the first/last slot. Completed ([x])
                             tasks are never reordered (exit 1)
  swap <ID1> <ID2>           Exchange the positions of two active tasks
  reorder <ID1> <ID2> ...    Set the relative order of active tasks;
                             listed IDs move ahead of unlisted ones,
                             which keep their relative order. Prints
                             a multi-line summary of the applied
                             change, then the refreshed sprint view
                             (the `ck list` equivalent); `move` /
                             `swap` stay single-line
  add <text>                 Insert a new open task before ## Completed
  note <text>                Attach/update a process note on the active task
                             (shown in `ck st`, cleared by `ck done`)
  notes                      List all active process notes: the focused
                             task's note plus every paused task's bound note
                             ([i] No active process notes found. when none)
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

Space Management (manage global spaces):
  space list                 List every global space plus the
                             active [PROJECT] context; [PROJECT]
                             is where un-prefixed commands
                             (ck st, ck list) resolve
  space create <name>        Create a new space from the default
                             template
  space rename <old> <new>   Rename a space, carrying its notes
                             along (alias: mv)
  space delete <name>        Permanently delete a space and its
                             process notes; the target is checked
                             first, then `-y` skips the [y/N]
                             confirmation. Built-in `local` /
                             `remote` cannot be deleted

Spaces (out-of-project task lists, ~/.config/ck/spaces/):
  local add <text>          Add a task to the workstation space
  local list                List workstation space tasks
  local st                  Detailed status view for the
                             workstation space (same layout
                             as `ck st`: header, progress,
                             current focus, notes, context);
                             `status` is an exact alias
  local st -l|--list        Space task listing (as `ck st -l`)
  local st -e|--edit        Open the space file in $EDITOR
                             (as `ck st -e`)
  local st -g|--global      Global dashboard (as `ck st -g`);
                             same as `ck local dashboard`
  local done <ID|range>     Mark workstation task(s) done ([x]);
                             bare `ck local done` completes the
                             CURRENT FOCUS of the space
  local focus <ID>          Focus a workstation task ([>]);
                             0 resets focus
  local start <ID>          Alias for `ck local focus <ID>`
  local note <ID> <text>    Attach a process note to a
                             workstation task
  local notes               List every active process note
                             (focused task + paused/unfocused
                             tasks)
  local edit                Open the workstation space file
                             (~/.config/ck/spaces/local.md) in
                             your editor
  local dashboard           Alias for `ck dashboard`; `-v` adds
                             the detailed PREV/FOCUS/NEXT blocks
                             (spaces are listed there too)
  remote ...                Same commands for the infrastructure
                             space (~/.config/ck/spaces/remote.md)
  <space> ...               ANY space file placed in
                             ~/.config/ck/spaces/<name>.md is
                             automatically routable with the same
                             commands (dynamic spaces); spaces
                             also render a detailed PREV/FOCUS/NEXT
                             block under `ck dashboard -v`

Editor resolution (ck edit / ck log):
  1. "editor" key in .ck.json at the project root
  2. $VISUAL
  3. $EDITOR
  4. nano (if installed), else vi

Color overrides ("colors" keys in .ck.json, optional):
  text, muted, border, accent -> "none" (native), "cyan", "bold blue",
  "bright-black", or raw SGR ("38;5;208"); unset keys inherit your
  terminal's native text color. The `border` slot paints the thin
  table/card rules and defaults to a quiet "bright-black" hairline
  (use "none" to inherit the native color).

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
  ck dev emulate             Interactive history-rotation stress emulator:
                             3 generate/rotate cycles in
                             .sandbox/projects/sandbox_stress with tiny
                             limits (.md.gz archives, FIFO purge), then
                             prints how to inspect via `ck log --all`
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
    "init", "start", "done", "add", "note", "notes", "save", "edit",
    "log",
})
# `ck space …` is GLOBAL (it operates purely on ~/.config/ck/spaces/),
# so it is deliberately NOT in _LOCAL_ONLY_COMMANDS — the management
# layer must work from any directory, including a dangling one.

# User-facing message shown when a local command runs from a working
# directory whose descriptor is gone (rootless keeper).
# Plain TEXT (no literal badge): rendered through ``notice()`` so the
# ``[!]`` token is styled and NO_COLOR still suppresses it. Built LAZILY
# because the colour decision must be taken at call time — the
# environment can change between import and dispatch.
_NO_ROOT_TEXT = (
    "Not in a valid project directory. "
    "cd into your project (or use a global command: ck st -g)."
)


def _no_root_message() -> str:
    """The rootless-cwd guidance line, with a styled ``[!]`` badge."""
    return ui.notice(ui.WARN, _NO_ROOT_TEXT)


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
    p_st.add_argument(
        "project_name", nargs="?", default=None,
        help="Registered project to show (default: the current "
             "project; outside every project `ck st` names the "
             "single nested project or lists the choices)")

    p_dash = sub.add_parser(
        "dashboard",
        help="Cross-project dashboard (compact table; -v for block view)",
    )
    p_dash.add_argument("-v", "--verbose", dest="verbose", action="store_true",
                        help="Block view: full triad context per project")

    p_list = sub.add_parser(
        "list", help="Print the task list to STDOUT")
    p_list.add_argument(
        "project_name", nargs="?", default=None,
        help="Registered project to list (default: the current project)")

    p_start = sub.add_parser("start", help="Focus a task (0 resets focus)")
    p_start.add_argument(
        "task_id", type=int,
        help="Task ID to focus; 0 resets focus (demoted task is reported)")
    p_start.add_argument(
        "--no-input", dest="no_input", action="store_true",
        help="Never prompt for a note on focus loss (non-interactive)")
    p_start.add_argument(
        "-y", "--yes", dest="assume_yes", action="store_true",
        help="Assume 'yes' at the note prompt (asks only for the text)")

    p_done = sub.add_parser(
        "done",
        help="Mark task(s) done (bare `ck done` completes the "
             "current focus)")
    p_done.add_argument(
        "spec", nargs="?", default="",
        help="Task ID, range, or list (e.g. 3, 2-4); omitted = the "
             "currently focused task")

    p_add = sub.add_parser("add", help="Add a new task")
    p_add.add_argument("text", nargs="+", help="Task text")

    sub.add_parser(
        "move",
        help="Move a pending/active task to a position (or top/bottom)")
    sub.add_parser(
        "swap",
        help="Exchange the positions of two pending/active tasks")
    sub.add_parser(
        "reorder",
        help="Set the relative order of pending/active tasks; prints "
             "the new order plus the refreshed sprint view")

    p_note = sub.add_parser(
        "note", help="Attach a process note to the active task")
    p_note.add_argument("text", nargs="+", help="Note text")

    sub.add_parser(
        "notes", help="List all active process notes (focus + paused)")

    sub.add_parser("save", help="Two-step save + local commit")
    sub.add_parser("edit", help="Open PLAN.md in $EDITOR")
    p_log = sub.add_parser(
        "log", help="Open HISTORY.md in $EDITOR (--all: view the "
        "full archive history)"
    )
    p_log.add_argument(
        "--all", action="store_true",
        help="View the full history: every rotation archive (.md.gz "
             "decompressed / legacy .md.bak, oldest first) followed by "
             "the current HISTORY.md",
    )
    sub.add_parser("info", help="Show installation diagnostics")

    p_space = sub.add_parser(
        "space", help="Manage global spaces (list/create/rename/delete)")
    space_sub = p_space.add_subparsers(dest="space_action")
    space_sub.add_parser("list", help="List spaces + the active project")
    p_screate = space_sub.add_parser(
        "create", help="Create a new space")
    p_screate.add_argument("name", help="Space name (no spaces/slashes)")
    p_srename = space_sub.add_parser("rename", help="Rename a space")
    p_srename.add_argument("old_name", help="Existing space name")
    p_srename.add_argument("new_name", help="New space name")
    p_sdelete = space_sub.add_parser(
        "delete", help="Permanently delete a space and its notes")
    p_sdelete.add_argument("name", help="Space name")
    p_sdelete.add_argument(
        "-y", "--yes", "--force", dest="assume_yes", action="store_true",
        help="Skip the confirmation prompt (for automation)")

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
    dev_sub.add_parser(
        "emulate",
        help="Interactive history-rotation stress emulator "
             "(sandbox only)",
    )

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
    "init", "st", "dashboard", "start", "done", "add", "note", "notes",
    "save", "edit", "log", "install", "uninstall", "update", "help",
    "list", "register", "unregister", "prune", "info",
    "move", "swap", "reorder",
    "local", "remote", "space",
    "dev", "sandbox", "ck-clean",
)

# Space-routed actions: `ck <space> <action> [...]` dispatches to
# the space's Markdown file for ANY space — the built-in defaults
# (local / remote) or any file discovered in
# ~/.config/ck/spaces/ (dynamic spaces).
_SPACE_ACTIONS = frozenset({
    "add", "list", "done", "focus", "start", "note", "notes",
    "st", "status", "edit", "dashboard",
})

# Flags each SPACE ACTION accepts (keyed by the action, not by the
# space name — `local`, `remote` and every dynamic space share one
# table). Mirrors the project flag sets in ``_LEGACY_FLAGS``:
# ``st``/``status`` carry the same three view shorthands as
# ``ck st`` (documented for projects in :data:`_STATUS_SHORTCUTS`),
# and ``dashboard`` carries the verbose switch. Every other space
# action takes no flags at all, so a stray ``-x`` is reported as an
# error instead of being swallowed into task text.
_SPACE_ACTION_FLAGS: dict[str, frozenset] = {
    "st": frozenset({"-l", "--list", "-e", "--edit", "-g", "--global"}),
    "status": frozenset({"-l", "--list", "-e", "--edit",
                         "-g", "--global"}),
    "dashboard": frozenset({"-v", "--verbose"}),
}

# Flags each SPACE MANAGEMENT action accepts (``ck space …``).
# Only the irreversible ``delete`` takes a bypass flag; the others
# reject every flag so a typo can never be swallowed silently.
_SPACE_MGMT_FLAGS: dict[str, frozenset] = {
    "delete": frozenset({"-y", "--yes", "--force"}),
}

# Head tokens that are real commands. The built-in space names are
# EXCLUDED: `ck local …` / `ck remote …` route through the space
# dispatcher (uniformly with every dynamic space) instead of the
# generic command dispatch.
_COMMAND_HEADS = frozenset(_LEGACY_CHOICES) - {"local", "remote"}

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
    "start": frozenset({"--no-input", "-y", "--yes"}),
    "done": frozenset(),
    "add": frozenset(),
    "move": frozenset(),
    "swap": frozenset(),
    "reorder": frozenset(),
    "note": frozenset(),
    "notes": frozenset(),
    "save": frozenset(),
    "edit": frozenset(),
    "log": frozenset({"--all"}),
    "info": frozenset(),
    "register": frozenset({"-n", "--name", "--path"}),
    "unregister": frozenset({"--path"}),
    "prune": frozenset(),
    "local": frozenset(),
    "remote": frozenset(),
    "install": frozenset({"--dev"}),
    "uninstall": frozenset(),
    "update": frozenset(),
    "help": frozenset(),
    "dev": frozenset(),
    "sandbox": frozenset(),
    "ck-clean": frozenset(),
}


def _reject_unknown_flags(cmd: str, rest: List[str],
                          *,
                          allowed: Optional[frozenset] = None
                          ) -> Optional[List[str]]:
    """Validate legacy flags for ``cmd``.

    Returns the argument list with the ``--`` escape marker removed,
    or None (after printing an error) when an unknown flag appears.
    A literal ``--`` marks everything after it as positional text —
    e.g. ``ck add -- --not-a-flag``.

    ``allowed`` overrides the ``_LEGACY_FLAGS`` lookup for callers
    whose flag set is decided per INVOCATION rather than per
    command name — the space dispatcher validates against the
    ACTION's table, since ``ck local st -l`` is valid while
    ``ck local add -l`` is not. When it is None the command's own
    entry is used, exactly as before.
    """
    if allowed is None:
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


def _dispatch_space(space: str, rest: List[str]) -> int:
    """Run a space command with the shared CLI error translation.

    Space commands are dispatched from two sites (the dynamic
    routing in :func:`_dispatch` and the legacy positional path in
    :func:`_legacy_dispatch`); this wrapper gives both the same
    fail-closed error handling as every other command: domain
    errors (unknown task ID, bad spec, missing space) print a clean
    ``ERROR:`` line and exit 2 instead of raising a traceback.
    """
    try:
        return _run_space_command(space, rest)
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
        # Non-UTF-8 space file reads.
        _print_error(f"ERROR: Cannot decode file contents as UTF-8: {e}")
        return 4
    except OSError as e:
        # Unreadable files, permission errors, …
        _print_error(f"ERROR: I/O error: {e}")
        return 4


def _space_routed_command(raw: List[str]) -> Optional[str]:
    """Space name when ``raw`` is a ``ck <space> <action>`` invocation.

    DYNAMIC SPACE ROUTING: ``ck <space> <action>`` routes to the
    space's Markdown file whenever the head token is NOT a known
    command (or is a built-in space name) and the second token is a
    space action (``add`` / ``list`` / ``done`` / ``focus`` /
    ``note``). The space itself may be a built-in default
    (``local`` / ``remote``), any file discovered in
    ``~/.config/ck/spaces/``, or a brand-new name whose file the
    ``add`` action creates lazily on first write (read/mutate
    actions on a missing space file fail with a clean
    ``Space '<name>' not found`` error).

    Returns None for every non-space invocation so command dispatch
    is completely unaffected.
    """
    if len(raw) < 2 or raw[1] not in _SPACE_ACTIONS:
        return None
    head = raw[0]
    if head.startswith("-") or head in _COMMAND_HEADS:
        return None
    from . import spaces
    return spaces.routable_space_name(head)


def _prompt_note_before_switch(ck: ContextKeeper, *, no_input: bool = False,
                               assume_yes: bool = False) -> None:
    """Interactive note prompt before a focus switch demotes a task.

    When the currently focused task has NO process note and stdin
    AND stdout are interactive TTYs, ask:

        Task #<OLD_ID> lost focus. Add a process note? [y/N]:

    - 'y'/'Y' -> ask ``Note text: `` and save it onto the old task
      (the subsequent switch then carries it into the paused
      registry instead of losing it).
    - 'N'/Enter/EOF -> proceed silently; the post-switch soft hint
      is emitted by :func:`_print_focus_result`.
    - Non-interactive (CI/pipe/script) -> never prompt; the soft
      hint after the switch is the only notice.

    ``no_input`` skips the prompt unconditionally; ``assume_yes``
    (``-y``) skips the y/N confirmation and asks only for the text.
    """
    if no_input:
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    info = ck.pending_focus_loss()
    if info is None or info["has_note"]:
        return
    try:
        if not assume_yes:
            answer = input(
                f"Task #{info['id']} lost focus. "
                "Add a process note? [y/N]: ")
            if answer.strip().lower() not in ("y", "yes"):
                return
        text = input("Note text: ")
    except (EOFError, KeyboardInterrupt):
        return
    text = text.strip()
    if not text:
        return
    saved = ck.set_note(text)
    print(f"* Note saved for [{saved['id']}]: {saved['note']}")


def _print_focus_result(result) -> None:
    """Print ``ck start`` output: the new focus, plus the Unfocused /
    Paused Context handling for the task that lost it.

    - The demoted task carries a note -> its scratchpad was archived
      to HISTORY.md and a paused-context block is printed.
    - The demoted task had NO note -> a soft hint nudges the user to
      attach one before switching (the task stays safely open).
    """
    if result.task_id > 0:
        print(f"-> Focused [{result.task_id}]: {result.title}")
    else:
        _notice(ui.OK, "Focus reset.")
    if result.demoted_id is not None:
        if result.had_note:
            _notice(ui.INFO,
                    f"Task [{result.demoted_id}] {result.demoted_title} "
                    "lost focus — moved to Unfocused / Paused Context "
                    "(note preserved; archived by `ck done`).")
        else:
            _notice(ui.WARN,
                    f"Task #{result.demoted_id} lost focus without a "
                    "note. Attach one via `ck note <text>`.")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns a process exit code.

    WORKING-DIRECTORY GUARANTEE: the caller's working directory is
    captured before dispatch and strictly restored afterwards — on
    normal return, on error, and after a sandbox subshell exits. No
    ``ck`` command may leak a directory change into the shell that
    invoked it.
    """
    try:
        original_cwd: Optional[str] = os.getcwd()
    except OSError:
        original_cwd = None
    try:
        return _dispatch(argv)
    finally:
        if original_cwd is not None:
            try:
                current = os.getcwd()
            except OSError:
                current = None
            if current != original_cwd:
                try:
                    os.chdir(original_cwd)
                except OSError:
                    pass


def _dispatch(argv: Optional[List[str]] = None) -> int:
    """Inner command dispatcher (see :func:`main`)."""
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

    # `ck --all log` (flag-first) is a documented synonym of
    # `ck log --all`; normalize so the legacy dispatcher sees the
    # command first.
    if raw[0] == "--all" and len(raw) >= 2 and raw[1] == "log":
        raw = ["log", "--all", *raw[2:]]

    # DANGLING WORKING DIRECTORY: when the cwd's descriptor is gone
    # (wiped sandbox, external rebuild), a rootless keeper can still
    # serve the GLOBAL registry commands and global-fallback reads —
    # but the commands above have no project to operate on and must
    # fail with a clean, actionable message instead of a traceback.
    if raw[0] in _LOCAL_ONLY_COMMANDS and find_project_root() is None:
        print(_no_root_message())
        return 1

    # DYNAMIC SPACE ROUTING: `ck <space> <action>` routes to the
    # targeted space file for ANY space — the built-in defaults
    # (local / remote) or any file discovered in
    # ~/.config/ck/spaces/. Space commands are global
    # (project-independent), so they work from any working
    # directory, including a dangling one.
    routed_space = _space_routed_command(raw)
    if routed_space is not None:
        return _dispatch_space(routed_space, raw[1:])

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
            # One implementation path for BOTH dispatch styles:
            # translate the namespace to the legacy token list and
            # run the shared ``ck st`` runner (targeting + views).
            rest = []
            if getattr(args, "project_name", None):
                rest.append(args.project_name)
            if args.list_tasks:
                rest.append("-l")
            elif args.edit:
                rest.append("-e")
            elif getattr(args, "global_dash", False):
                rest.append("-g")
            elif args.all:
                rest.append("--all")
            return _run_status(ck, rest)
        elif args.command == "dashboard":
            print(ck.dashboard(verbose=getattr(args, "verbose", False)))
        elif args.command == "list":
            out, rc = ck.list_tasks(getattr(args, "project_name", None))
            if rc:
                _print_error(out)
                return rc
            print(out)
        elif args.command == "info":
            print(ck.info())
        elif args.command == "space":
            # argparse sub-namespace -> the same positional pipeline the
            # legacy dispatcher uses, so `ck space …` has exactly ONE
            # implementation path.
            action = getattr(args, "space_action", None)
            rest = [action] if action else []
            if action == "create":
                rest.append(args.name)
            elif action == "rename":
                rest.extend([args.old_name, args.new_name])
            elif action == "delete":
                rest.append(args.name)
                if getattr(args, "assume_yes", False):
                    rest.append("-y")
            return _run_space_mgmt(rest)
        elif args.command == "start":
            # IDEMPOTENCY: re-focusing the already-focused task is a
            # clean no-op — no note prompt, no plan mutation.
            if args.task_id > 0 and ck.is_already_focused(args.task_id):
                print(f"Task #{args.task_id} is already focused.")
                return 0
            _prompt_note_before_switch(
                ck,
                no_input=getattr(args, "no_input", False),
                assume_yes=getattr(args, "assume_yes", False),
            )
            result = ck.start(args.task_id)
            _print_focus_result(result)
        elif args.command == "done":
            spec = args.spec
            if not spec:
                # IMPLICIT FOCUS TARGET: `ck done` without arguments
                # completes the CURRENT FOCUS. Without a focus the
                # standard usage error applies (non-zero exit).
                focus_id = ck.current_focus_id()
                if focus_id is None:
                    print("Usage: ck done <ID|range|list>")
                    return 2
                spec = str(focus_id)
            ids = ck.done(spec)
            _notice(ui.OK, f"Marked done: {', '.join(map(str, ids))}")
            _maybe_notify(ck)
        elif args.command == "add":
            text = " ".join(args.text)
            new_id = ck.add_task(text)
            _notice(ui.OK, f"Added task (id={new_id}): {text}")
        elif args.command == "note":
            text = " ".join(args.text)
            info = ck.set_note(text)
            print(f"* Note saved for [{info['id']}]: {info['note']}")
        elif args.command == "notes":
            print(ck.notes())
        elif args.command == "save":
            ck.save()
            _maybe_notify(ck)
        elif args.command == "edit":
            ck.edit_plan()
        elif args.command == "log":
            if args.all:
                print(ck.read_full_history())
            else:
                ck.edit_log()
        elif args.command == "register":
            path = Path(args.path).resolve() if args.path else None
            entry = ck.register(path=path, name=args.name)
            _notice(ui.OK, f"Registered: {entry.name} -> {entry.path}")
        elif args.command == "unregister":
            path = Path(args.path).resolve() if args.path else None
            removed = ck.unregister(path=path, name=args.name)
            if removed:
                _notice(ui.OK, "Unregistered.")
            else:
                _notice(ui.INFO, "Not in registry.")
        elif args.command == "prune":
            pruned = ck.prune()
            if pruned:
                print(f"Pruned {len(pruned)} missing entries:")
                for p in pruned:
                    print(f"   * {p}")
                _notice(ui.OK, f"Summary: {len(pruned)} project(s) "
                          "purged from the global registry.")
            else:
                _notice(ui.OK, "Nothing to prune.")
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
    """Print an ERROR line in bold red when colors are enabled.

    The whole line is painted ``\\033[1;31m`` … ``\\033[0m`` (the
    :data:`ui.ERR` style) so failures are unmistakable. Contrast
    policy: errors use the high-visibility bold red treatment, never
    DIM/dark-gray. With colors off (NO_COLOR, pipes) the output is
    byte-identical to a plain ``print``.
    """
    p = ui.get_palette()
    if not p.enabled:
        print(message)
        return
    print(f"{ui.BOLD_RED}{message}{ui.RESET}")


def _notice(token: str, message: str = "") -> None:
    """Print a styled status line: badge first, plain text after.

    The single funnel for every ``[!]`` / ``[i]`` / ``[ok]`` notice
    the CLI emits, so badge styling can never drift between commands.
    See :func:`cklib.ui.notice` for the reset-immediately-after-the-
    badge contract.
    """
    print(ui.notice(token, message))


# Usage strings for the three reordering commands, keyed by action.
# Shared by the arity check and the "not a number" message so the two
# can never disagree.
_REORDER_USAGE = {
    "move": "ck move <ID> <position|top|bottom>",
    "swap": "ck swap <ID1> <ID2>",
    "reorder": "ck reorder <ID1> <ID2> [<ID3> ...]",
}


def _render_sprint_after_reorder(ck) -> None:
    """Print the target project's sprint view after ``ck reorder``.

    A reorder changes what the user sees, so the fresh ``ck list``
    rendering follows the confirmation immediately instead of making
    them run a second command (v0.8.10). ONLY the multi-id ``reorder``
    does this — the single-task ``move`` / ``swap`` keep their
    one-line confirmation.

    Rendering is best-effort: the reorder is already committed, so a
    view failure can never turn a successful command into a failing
    one (the sprint view is skipped, exit code stays 0).
    """
    try:
        out, rc = ck.list_tasks()
    except Exception:
        return
    if rc or not out:
        return
    print()
    print(out)


def _run_reorder(ck, action: str, tokens: List[str]) -> int:
    """Run ``move`` / ``swap`` / ``reorder`` and translate domain errors.

    All three share one contract, enforced here rather than in three
    near-identical dispatch branches:

    - a non-numeric id → exit 2 (usage error);
    - an UNKNOWN id    → exit 2 with the plain "No task with id N";
    - a COMPLETED id   → exit 1 with the explicit
      ``Cannot move completed task #N. Only pending/active tasks can
      be reordered.`` refusal;
    - success          → exit 0 and a styled ``[ok]`` confirmation.

    ``reorder`` alone (never ``move`` / ``swap``) then re-renders the
    project's sprint view, and its confirmation is the multi-line
    summary produced by :meth:`ContextKeeper.reorder_tasks`.

    Returns the exit code (0 = success).
    """
    usage = _REORDER_USAGE[action]
    # 1. Parse the id tokens FIRST so a typo can never be reported as
    #    a domain error (and vice versa).
    id_count = 2 if action == "swap" else len(tokens)
    if action == "move":
        id_count = 1
    ids: List[int] = []
    for tok in tokens[:id_count]:
        try:
            ids.append(int(tok))
        except ValueError:
            _print_error(f"ERROR: Task ID must be a number: {tok!r}\n"
                         f"   Usage: {usage}")
            return 2

    # 2. Execute against the domain.
    try:
        if action == "move":
            message = ck.move_task(ids[0], tokens[1])
        elif action == "swap":
            message = ck.swap_tasks(ids[0], ids[1])
        else:
            message = ck.reorder_tasks(ids)
    except CompletedTaskError as e:
        # The STRICT rule: completed work is never reordered.
        _notice(ui.ERR,
                f"Cannot move completed task #{e.task_id}. "
                "Only pending/active tasks can be reordered.")
        return 1
    except KeyError as e:
        _print_error(f"ERROR: {e.args[0] if e.args else e}\n"
                     f"   Usage: {usage}")
        return 2
    except ValueError as e:
        # Domain range errors ("Position 9 is out of range (1..3)").
        _print_error(f"ERROR: {e}\n   Usage: {usage}")
        return 2
    _notice(ui.OK, message)
    if action == "reorder":
        # v0.8.10: a successful reorder immediately shows the fresh
        # sprint view (the `ck list` equivalent) for the target
        # project. Deliberately NOT done for single-task `move` /
        # `swap`, whose confirmation stays a single line.
        _render_sprint_after_reorder(ck)
    return 0


def _run_space_mgmt(rest: List[str]) -> int:
    """Dispatch ``ck space <action> …`` — the space management layer.

    Actions (with their explicit aliases):

    - ``list`` (``ls``)   — every global space plus the ACTIVE
      PROJECT row (``[PROJECT] <name>``) that un-prefixed commands
      resolve against;
    - ``create <name>`` (``new``) — scaffold a new space file;
    - ``rename <old> <new>`` (``mv``) — rename it, carrying the note
      sidecar along;
    - ``delete <name>``   — permanently remove it plus its sidecar.
      NO short alias on purpose: deletion is irreversible, so the verb
      is spelled out and requires an interactive ``[y/N]`` confirmation
      (or an explicit ``-y`` / ``--yes`` / ``--force``).

    Validation lives in :mod:`cklib.spaces` (``validate_new_space_name``
    and friends); this layer only maps failures to clean ``ERROR:`` lines
    and exit codes.
    """
    from . import spaces

    usage = (
        "Usage: ck space "
        "<list|create <name>|rename <old> <new>|delete <name> [-y]>"
    )
    if not rest:
        print(usage)
        return 2

    action, args = rest[0], rest[1:]
    validated = _reject_unknown_flags(
        f"space {action}", rest,
        allowed=_SPACE_MGMT_FLAGS.get(action, frozenset()))
    if validated is None:
        return 2
    args = validated[1:]

    try:
        if action in ("list", "ls"):
            if args:
                _print_error("ERROR: `ck space list` takes no arguments.")
                return 2
            ck = ContextKeeper()
            print(ck.space_manager_list())
            return 0

        if action in ("create", "new"):
            if not args:
                print("Usage: ck space create <name>")
                return 2
            if len(args) > 1:
                _print_error(
                    "ERROR: `ck space create` takes exactly one name "
                    f"(got {len(args)}). Spaces never contain spaces.")
                return 2
            path = spaces.create_space(args[0])
            _notice(ui.OK, f"Created space {args[0]!r}: {path}")
            print(f"     Add a task with `ck {args[0]} add <text>`.")
            return 0

        if action in ("rename", "mv"):
            if len(args) != 2:
                print("Usage: ck space rename <old> <new>")
                return 2
            info = spaces.rename_space(args[0], args[1])
            _notice(ui.OK, f"Renamed space {info['from']!r} -> "
                      f"{info['to']!r}: {info['moved']}")
            if info["notes_moved"]:
                print("     Process notes (sidecar) carried along.")
            return 0

        if action == "delete":
            # Split the confirmation flags off FIRST: `ck space delete
            # work -y` is one name plus a bypass, not two names.
            assume_yes = any(
                tok in ("-y", "--yes", "--force") for tok in args)
            names = [a for a in args
                     if a not in ("-y", "--yes", "--force")]
            if not names:
                print("Usage: ck space delete <name> [-y]")
                return 2
            if len(names) > 1:
                _print_error(
                    "ERROR: `ck space delete` takes exactly one name.")
                return 2
            name = names[0]
            # PRE-FLIGHT: validate the TARGET before asking anything.
            # A space that does not exist (or a protected built-in) can
            # never be deleted, so the honest ERROR wins — the user is
            # never asked to confirm a no-op, and a script piping into
            # `ck space delete` never blocks on a prompt it could only
            # meaningfully answer "yes" to.
            spaces.validate_deletable_space(name)
            # Confirmation gate: `-y` / `--yes` / `--force` bypass the
            # prompt for automation; otherwise ask interactively and
            # REFUSE on a non-TTY (never delete on a bare newline,
            # never delete when stdin is piped).
            if not assume_yes:
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    _print_error(
                        "ERROR: Refusing to delete space "
                        f"{name!r} without confirmation "
                        "(non-interactive terminal). Pass -y to "
                        "confirm.")
                    return 2
                try:
                    answer = input(
                        f"Are you sure you want to permanently delete "
                        f"space '{name}' and its process notes? [y/N]: ")
                except (EOFError, KeyboardInterrupt):
                    _notice(ui.INFO, "Aborted.")
                    return 2
                if answer.strip().lower() not in ("y", "yes"):
                    _notice(ui.INFO, "Aborted.")
                    return 2
            info = spaces.delete_space(name, confirmed=True)
            _notice(ui.OK, f"Deleted space {info['name']!r} "
                      f"({info['removed']}).")
            if info["notes"]:
                print("     Process notes (sidecar) deleted too.")
            return 0

    except spaces.SpaceNotFoundError as e:
        # "Not found" is its own outcome, not a usage error: report it
        # with exit 1 so a script can tell "you asked for something
        # that isn't there" apart from "you asked for something
        # invalid". Handled before the generic ValueError branch.
        _print_error(f"ERROR: {e}")
        return 1
    except ValueError as e:
        _print_error(f"ERROR: {e}")
        return 2
    except OSError as e:
        _print_error(f"ERROR: I/O error: {e}")
        return 4

    _print_error(
        f"ERROR: Unknown `ck space` action: {action!r}\n   {usage}"
    )
    return 2


def _print_full_plan(ck: ContextKeeper) -> None:
    # AMBIGUOUS directory (several nested projects, none chosen): the
    # discovery notice printed above already says why nothing follows
    # — a plan exists in every listed project, so neither "PLAN.md not
    # found" nor some other project's plan may appear here.
    if ck.status_is_ambiguous():
        return
    print("=" * 45)
    print(" FULL PLAN (PLAN.md)")
    print("=" * 45)
    # Follows the SAME target resolution as the status block above
    # (local → ancestors up to the Git repo root → nested-project
    # discovery → global registry), so the plan printed here always
    # belongs to the project that block named.
    text = ck.full_plan_text()
    if text is None:
        _notice(ui.WARN, "PLAN.md not found.\n")
    else:
        print(text)


def _run_status(ck: ContextKeeper, rest: List[str]) -> int:
    """Run ``ck st`` — view flags plus EXPLICIT project targeting.

    ``ck st <project_name>`` rebinds the whole command to that
    project from ANY working directory (same resolution as
    ``ck list <project_name>``: registry name → folder name → the
    nested-project scan), so an explicitly named target always wins
    over whichever project the user happens to be standing in. An
    unknown name is the same clean exit-1 error ``ck list`` prints —
    never a silent fall back to a different project, never a
    traceback. More than one positional token is a usage error.

    ``-g`` (the GLOBAL dashboard) is a cross-project view, so
    pairing it with a project name is rejected as a usage error
    instead of silently ignoring the target.

    Returns the exit code (0 = rendered, 1 = unknown project,
    2 = usage).
    """
    positionals = [tok for tok in rest if not tok.startswith("-")]
    if len(positionals) > 1:
        _print_error("ERROR: Usage: ck st [<project_name>] "
                     "[-l|-e|-g|--all]")
        return 2
    target = ck
    if positionals:
        name = positionals[0]
        root = ck.resolve_named_project(name)
        if root is None:
            _print_error(f"ERROR: Project '{name}' not found.")
            return 1
        if "-g" in rest or "--global" in rest:
            _print_error("ERROR: `ck st -g` renders the global "
                         "dashboard and takes no project name "
                         f"(got: {name}).")
            return 2
        # EXPLICIT TARGET: the named project becomes THE context, so
        # every view flag below operates on it — never on whatever
        # the current directory happens to resolve to.
        target = ContextKeeper(root=root)
    if "-l" in rest or "--list" in rest:
        # ``ck st -l`` IS ``ck list`` — the documented same view — so
        # it routes through the same command: outside every project
        # the nested-project discovery decides there, and the GLOBAL
        # registry tier never silently picks a task list for you.
        out, rc = target.list_tasks()
        if rc:
            _print_error(out)
            return rc
        print(out)
    elif "-e" in rest or "--edit" in rest:
        target.edit_plan()
    elif "-g" in rest or "--global" in rest:
        print(target.dashboard())
    elif "--all" in rest:
        print(target.status())
        _print_full_plan(target)
    else:
        print(target.status())
    return 0


def _legacy_dispatch(raw: List[str]) -> int:
    """Dispatch legacy positional invocations (preserves old UX)."""
    cmd, rest = raw[0], raw[1:]
    # `ck space …` validates its flags per ACTION inside
    # ``_run_space_mgmt`` (only ``delete`` accepts ``-y``), so the
    # command-level pre-pass is skipped here — otherwise it would
    # reject the flag (or accept it for the wrong action) before the
    # action-aware check ever runs.
    if cmd != "space":
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
            # Optional positional project name (`ck st <project_name>`)
            # + the documented view flags, all handled by the shared
            # runner so the argparse and legacy paths cannot drift.
            return _run_status(ck, rest)
        elif cmd == "dashboard":
            verbose = "-v" in rest or "--verbose" in rest
            print(ck.dashboard(verbose=verbose))
        elif cmd == "list":
            # Optional positional project name: `ck list <project_name>`
            # targets a registered project from ANY directory. With no
            # argument the core layer decides (inside a project ->
            # normal listing; outside -> explicit nested-project
            # discovery, never a silent arbitrary fallback).
            name = rest[0] if rest else None
            out, rc = ck.list_tasks(name)
            if rc:
                _print_error(out)
                return rc
            print(out)
        elif cmd == "info":
            print(ck.info())
        elif cmd == "start":
            no_input = False
            assume_yes = False
            id_tokens: List[str] = []
            for tok in rest:
                if tok == "--no-input":
                    no_input = True
                elif tok in ("-y", "--yes"):
                    assume_yes = True
                else:
                    id_tokens.append(tok)
            if not id_tokens:
                print("Usage: ck start <ID>")
                return 2
            try:
                tid = int(id_tokens[0])
            except ValueError:
                _print_error(
                    f"ERROR: Invalid task ID: {id_tokens[0]!r}")
                return 2
            # IDEMPOTENCY: re-focusing the already-focused task is
            # a clean no-op — no note prompt, no plan mutation.
            if tid > 0 and ck.is_already_focused(tid):
                print(f"Task #{tid} is already focused.")
                return 0
            _prompt_note_before_switch(
                ck, no_input=no_input, assume_yes=assume_yes)
            result = ck.start(tid)
            _print_focus_result(result)
        elif cmd == "done":
            spec = rest[0] if rest else ""
            if not spec:
                # IMPLICIT FOCUS TARGET: `ck done` without arguments
                # completes the CURRENT FOCUS. Without a focus the
                # standard usage error applies (non-zero exit).
                focus_id = ck.current_focus_id()
                if focus_id is None:
                    print("Usage: ck done <ID|range|list>")
                    return 2
                spec = str(focus_id)
            ids = ck.done(spec)
            _notice(ui.OK, f"Marked done: {', '.join(map(str, ids))}")
            if notifiable:
                _maybe_notify(ck)
        elif cmd == "add":
            if not rest:
                print("Usage: ck add <text>")
                return 2
            text = " ".join(rest)
            new_id = ck.add_task(text)
            _notice(ui.OK, f"Added task (id={new_id}): {text}")
        elif cmd == "move":
            if len(rest) < 2:
                print("Usage: ck move <ID> <position|top|bottom>")
                return 2
            rc = _run_reorder(ck, "move", rest)
            if rc:
                return rc
        elif cmd == "swap":
            if len(rest) < 2:
                print("Usage: ck swap <ID1> <ID2>")
                return 2
            rc = _run_reorder(ck, "swap", rest[:2])
            if rc:
                return rc
        elif cmd == "reorder":
            if not rest:
                print("Usage: ck reorder <ID1> <ID2> [<ID3> ...]")
                return 2
            rc = _run_reorder(ck, "reorder", rest)
            if rc:
                return rc
        elif cmd == "note":
            if not rest:
                print("Usage: ck note <text>")
                return 2
            text = " ".join(rest)
            info = ck.set_note(text)
            print(f"* Note saved for [{info['id']}]: {info['note']}")
        elif cmd == "notes":
            print(ck.notes())
        elif cmd == "save":
            ck.save()
            if notifiable:
                _maybe_notify(ck)
        elif cmd == "edit":
            ck.edit_plan()
        elif cmd == "log":
            if "--all" in rest:
                print(ck.read_full_history())
            else:
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
            _notice(ui.OK, f"Registered: {entry.name} -> {entry.path}")
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
                _notice(ui.OK, "Unregistered.")
            else:
                _notice(ui.INFO, "Not in registry.")
        elif cmd == "prune":
            pruned = ck.prune()
            if pruned:
                print(f"Pruned {len(pruned)} missing entries:")
                for p in pruned:
                    print(f"   * {p}")
                _notice(ui.OK, f"Summary: {len(pruned)} project(s) "
                          "purged from the global registry.")
            else:
                _notice(ui.OK, "Nothing to prune.")
        elif cmd in ("local", "remote"):
            return _run_space_command(cmd, rest)
        elif cmd == "space":
            return _run_space_mgmt(rest)
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
# Out-of-project spaces (ck local / ck remote)
# ---------------------------------------------------------------------- #

def _run_space_command(space: str, rest: List[str]) -> int:
    """Dispatch ``ck <space> <action>`` for ANY space.

    Works identically for the built-in defaults (``local`` /
    ``remote``) and every dynamic space discovered in
    ``~/.config/ck/spaces/``. Accepted actions:

    - ``add <text>``          — append a new open task;
    - ``list``                — pipe-friendly task listing;
    - ``st`` / ``status``     — detailed status view (same
                                 layout as ``ck st``: header,
                                 progress, current focus, notes,
                                 work context). Accepts the same
                                 view flags as ``ck st``: ``-l``
                                 (list), ``-e`` (edit), ``-g``
                                 (global dashboard);
    - ``done <ID|range>``     — mark task(s) done (bare form
                                 completes the space's CURRENT
                                 FOCUS, mirroring ``ck done``);
    - ``focus <ID>``          — focus a task, demoting any other
                                 focus (``0`` resets focus);
    - ``start <ID>``          — alias for ``focus <ID>``;
    - ``note <ID> <text>``    — attach a process note to a task;
    - ``notes``               — list every active process note
                                 (focused task + paused/unfocused
                                 tasks);
    - ``edit``                — open the space's Markdown file in
                                 the user's editor (resolved with
                                 the project's standard editor
                                 resolution logic);
    - ``dashboard``           — alias for ``ck dashboard``
                                 (``-v`` adds the detailed
                                 PREV/FOCUS/NEXT blocks).

    All mutations go through the centralized
    :class:`cklib.spaces.SpaceManager`, which re-uses the core
    Task/Plan parsing and editing engine on the space's Markdown
    file. Strictly explicit: there are no aliases (``-s`` / ``sm``
    / ``host`` / ``-m`` / ``virtual`` are deliberately unsupported
    unless a matching space file exists) and an unknown action is
    an error.
    """
    from . import spaces

    action = rest[0] if rest else ""
    # Flag validity is decided by the ACTION (see
    # ``_SPACE_ACTION_FLAGS``): `ck local st -l` is valid,
    # `ck local add -l` is not. Errors name the full invocation
    # (`ck local st`) so the fix is obvious.
    validated = _reject_unknown_flags(
        f"{space} {action}" if action else space, rest,
        allowed=_SPACE_ACTION_FLAGS.get(action, frozenset()))
    if validated is None:
        return 2
    rest = validated

    usage = (
        f"Usage: ck {space} "
        "<add <text>|list|st|status [-l|-e|-g]|done <ID|range>|"
        "focus <ID>|start <ID>|note <ID> <text>|notes|edit|"
        "dashboard [-v]>"
    )
    if not rest:
        print(usage)
        return 2
    action, args = rest[0], rest[1:]

    # `create` lets the `add` action materialize a brand-new
    # dynamic space lazily (the built-ins behave the same way);
    # every other action on a missing space file is a clean error.
    try:
        mgr = spaces.SpaceManager(space, create=(action == "add"))
    except ValueError as e:
        _print_error(f"ERROR: {e}")
        return 2

    if action == "add":
        if not args:
            print(f"Usage: ck {space} add <text>")
            return 2
        text = " ".join(args)
        new_id = mgr.add_task(text)
        _notice(ui.OK, f"Added task to {space} space (id={new_id}): {text}")
        return 0

    if action == "list":
        if args:
            _print_error(f"ERROR: `ck {space} list` takes no arguments.")
            return 2
        print(mgr.list_tasks())
        return 0

    if action in ("st", "status"):
        # VIEW FLAGS: `ck <space> st -l/-e/-g` are exact synonyms of
        # `ck <space> list/edit/dashboard`, checked in the same order
        # as the project `ck st` dispatcher.
        if "-l" in args or "--list" in args:
            print(mgr.list_tasks())
            return 0
        if "-e" in args or "--edit" in args:
            mgr.edit()
            return 0
        if "-g" in args or "--global" in args:
            print(ContextKeeper().dashboard())
            return 0
        if args:
            _print_error(f"ERROR: `ck {space} st` takes no arguments.")
            return 2
        print(mgr.status())
        return 0

    if action == "dashboard":
        # `ck <space> dashboard` is a direct alias of the global
        # dashboard (the dashboard is global state, never
        # space-scoped — `-v` adds the detailed blocks).
        if [a for a in args if a not in ("-v", "--verbose")]:
            _print_error(
                f"ERROR: `ck {space} dashboard` takes no arguments.")
            return 2
        verbose = "-v" in args or "--verbose" in args
        print(ContextKeeper().dashboard(verbose=verbose))
        return 0

    if action == "notes":
        if args:
            _print_error(
                f"ERROR: `ck {space} notes` takes no arguments.")
            return 2
        print(mgr.notes())
        return 0

    if action == "edit":
        if args:
            _print_error(f"ERROR: `ck {space} edit` takes no arguments.")
            return 2
        mgr.edit()
        return 0

    if action == "done":
        spec = args[0] if args else ""
        if not spec:
            # IMPLICIT FOCUS TARGET: bare `ck <space> done` completes
            # the CURRENT FOCUS of the space (usage error without one).
            focus_id = mgr.current_focus_id()
            if focus_id is None:
                print(f"Usage: ck {space} done <ID|range|list>")
                return 2
            spec = str(focus_id)
        ids = mgr.done(spec)
        _notice(ui.OK, f"Marked done: {', '.join(map(str, ids))}")
        return 0

    if action in ("focus", "start"):
        # `start` is an exact alias of `focus` (command parity
        # with the project-level `ck start <ID>`).
        if not args:
            print(f"Usage: ck {space} {action} <ID>")
            return 2
        try:
            tid = int(args[0])
        except ValueError:
            _print_error(f"ERROR: Invalid task ID: {args[0]!r}")
            return 2
        # IDEMPOTENCY: re-focusing the already-focused task is a
        # clean no-op — no plan mutation.
        if tid > 0 and mgr.already_focused(tid):
            print(f"Task #{tid} is already focused.")
            return 0
        result = mgr.focus(tid)
        if result["task_id"] > 0:
            print(f"-> Focused [{result['task_id']}]: {result['title']}")
        else:
            _notice(ui.OK, "Focus reset.")
        if result.get("demoted_id") is not None:
            _notice(ui.INFO, f"Task #{result['demoted_id']} "
                             f"{result['demoted_title']} lost focus "
                             "(demoted to open).")
        return 0

    if action == "note":
        if len(args) < 2:
            print(f"Usage: ck {space} note <ID> <text>")
            return 2
        try:
            tid = int(args[0])
        except ValueError:
            _print_error(f"ERROR: Invalid task ID: {args[0]!r}")
            return 2
        text = " ".join(args[1:])
        info = mgr.set_note(tid, text)
        print(f"* Note saved for [{info['id']}]: {info['note']}")
        return 0

    _print_error(
        f"ERROR: Unknown `ck {space}` action: {action!r}\n   {usage}"
    )
    return 2


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
    if action == "emulate":
        return _run_emulate_command()
    if action is None:
        print("Usage: ck dev <setup|clean|emulate>")
        return 2
    print(
        f"Unknown dev action: {action!r}. "
        "Usage: ck dev <setup|clean|emulate>"
    )
    return 2


def _run_emulate_command() -> int:
    """Guarded entry for ``ck dev emulate`` (history stress emulator).

    SAFETY GUARD: the emulator writes ONLY under ``sandbox_root()``;
    it is refused unless a sandbox context is actually present —
    either an active dev session (``CK_SANDBOX``/``CK_DEV``) or an
    existing ``.sandbox/`` directory. Outside a sandbox the command
    terminates with a warning and exit code 1, creating nothing.
    """
    from .emulate import run_emulation
    from .sandbox import is_dev_mode, sandbox_root

    if not is_dev_mode() and not sandbox_root().is_dir():
        _notice(ui.INFO, "Not in sandbox mode — emulator aborted.")
        return 1
    return run_emulation()


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
        _notice(ui.OK, f"{result.message}")
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
        _notice(ui.WARN, "An installed copy exists but no launcher "
                   "source was found; run `ck install` from the "
                   "checkout to refresh it.")
        return
    _notice(ui.INFO, "Refreshing installed production copy ...")
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


def _refuse_global_mutation_in_dev_mode(action: str) -> bool:
    """Print a clean refusal when dev mode targets the GLOBAL install.

    DEV-MODE GUARDRAIL (spec): dev-mode execution/testing must never
    touch, overwrite or mutate global system binaries
    (``~/.local/bin/ck``) or global state (``~/.config/ck/``).
    Returns True when a refusal was printed (caller must bail).
    """
    if not is_dev_mode():
        return False
    _notice(ui.WARN, f"Dev mode: `{action}` would mutate the global "
               f"installation ({USER_INSTALL_PATH}, "
               f"{SNAPSHOT_INSTALL_DIR}).")
    print(
        "    Sandbox sessions never touch global state — run this "
        "command outside dev mode (plain `ck` without CK_SANDBOX)."
    )
    return True


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
        _notice(ui.WARN, "Deprecated: `ck install --dev` no longer "
                   "creates ~/.local/bin/ck-dev.")
        print(
            "    Dev mode runs via local binary execution from the "
            "checkout: ./ck-dev <command>"
        )
        print("    (no global dev entrypoint is installed).")
        print("    Production install (physical copy): ./ck install")
        return
    if _refuse_global_mutation_in_dev_mode("install"):
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

    _notice(ui.OK, f"Installed (physical copy): {target}")
    _notice(ui.OK, f"Package snapshot: {snapshot / 'cklib'}")
    print(
        "Production is a static snapshot: re-run `ck install` (or "
        "`ck update`) after changing the checkout."
    )
    print("Run: ck --help")


def _uninstall_user() -> None:
    """Remove the user-level production install and dev leftovers.

    ``ck uninstall`` tears down the whole user installation:

    DEV-MODE GUARDRAIL: refuses (clean message, exit-code-free no-op)
    when dev mode is active — a sandbox session must never remove the
    global binary or the package snapshot.

    - ``$USER_BIN/ck`` — the physical copy written by ``ck install``
      (regular file), or a legacy symlink from older versions;
    - ``$USER_BIN/ck-dev`` — the legacy dev entrypoint created by the
      removed ``ck install --dev`` (purged when present);
    - the ``cklib`` package snapshot directory.

    Directories at the bin paths are refused (never deleted); missing
    paths are reported gracefully.
    DEV-MODE GUARDRAIL: refuses with a clean message (no mutation)
    when dev mode is active — a sandbox session must never remove the
    global binary or the package snapshot.
    """
    if _refuse_global_mutation_in_dev_mode("uninstall"):
        return
    bin_dir = _user_bin_dir()
    targets = (bin_dir / "ck", bin_dir / _DEV_SCRIPT_NAME)
    for target in targets:
        if not target.exists() and not target.is_symlink():
            _notice(ui.INFO, f"Not installed at {target}.")
            continue
        if target.is_dir() and not target.is_symlink():
            _notice(ui.WARN, f"{target} is a directory. Refusing to delete.")
            continue
        try:
            target.unlink()
            _notice(ui.OK, f"Removed: {target}")
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
        _notice(ui.OK, f"Removed package snapshot: {snapshot}")
    except OSError as e:
        _print_error(f"ERROR: Could not remove {snapshot}: {e}")


if __name__ == "__main__":
    sys.exit(main())
