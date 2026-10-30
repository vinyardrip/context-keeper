"""Shell completion script generation for Bash and Zsh.

``ck completion <shell>`` prints a self-contained completion script to
STDOUT so it can be sourced directly::

    eval "$(ck completion bash)"    # or: zsh

The generated scripts are deliberately small and navigation-focused:
the top-level completion offers the core commands plus the registered
project names, and the commands that accept a project target
(``ck st <project>`` / ``ck list <project>`` / ``ck unregister
<project>``) complete project names. ``ck st`` accepts exactly one
positional project: after a project name or flag is present, both
shells switch to valid flags/options instead of repeating projects.

PROJECT NAME SOURCE: the scripts never parse JSON themselves. They
call ``ck completion --list-projects``, which reads the global registry
(``~/.config/ck/projects.json``) through :mod:`cklib.registry` — the
same :func:`cklib.config.file_lock`-guarded read every other registry
consumer uses, so a concurrent writer can never hand the completion
a torn file. A missing, empty, unreadable or corrupt registry simply
yields no names; the helper never raises and never prints a traceback
(the shell side also discards stderr, so a broken registry can never
spray diagnostics into the prompt).

SPACE NAME SOURCE: ``ck completion --list-spaces`` emits the current
space sequence from :func:`cklib.spaces.space_order` — the persisted
``spaces_order`` reconciled with the spaces that actually exist. The
virtual ``project`` row and the built-ins are included in that order;
the generated scripts filter protected names for ``rename`` / ``delete``
and exclude the already-chosen space from the second ``swap`` operand.
Like the project source, the helper degrades to an empty list on any
failure and never creates config files.

BINARY RESOLUTION: both scripts resolve the helper through a small
``_ck_bin`` function. A non-empty, executable ``CK_BIN`` wins;
otherwise the function falls back to ``command -v ck``. ``ck-dev``
exports ``CK_BIN`` with the checkout's local launcher, so completion
keeps using the development binary even when a subshell, mise or a
re-exported ``PATH`` would resolve another ``ck``. A stale ``CK_BIN``
(removed checkout) degrades to the real ``ck`` on ``PATH`` instead of
breaking TAB completion.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

# Shells with a supported completion script.
SUPPORTED_SHELLS: Tuple[str, ...] = ("bash", "zsh")

# Commands offered at the FIRST position of `ck <TAB>`.
#
# Deliberately curated (not every internal command): core task
# navigation, the project/space entry points and registry hygiene.
# Order is the display order in Zsh descriptions.
COMPLETION_COMMANDS: Tuple[Tuple[str, str], ...] = (
    ("st", "status of the current project"),
    ("list", "print the task list"),
    ("dashboard", "cross-project dashboard"),
    ("add", "add a new task"),
    ("start", "focus a task"),
    ("done", "mark task(s) done"),
    ("delete", "permanently remove a task (alias: rm)"),
    ("note", "attach a process note"),
    ("notes", "list active process notes"),
    ("edit", "open PLAN.md in the editor"),
    ("log", "open HISTORY.md in the editor"),
    ("save", "save the current context"),
    ("space", "manage global spaces"),
    ("local", "workstation space"),
    ("remote", "infrastructure space"),
    ("register", "register a project"),
    ("unregister", "unregister a project"),
    ("prune", "drop registry entries whose folders are gone"),
    ("completion", "print a shell completion script"),
)

# Actions offered after `ck local <TAB>` / `ck remote <TAB>` / any
# dynamic `ck <space> <TAB>` (all spaces share one action vocabulary).
_SPACE_ACTIONS: Tuple[str, ...] = (
    "add", "list", "st", "status", "done", "focus", "start",
    "note", "notes", "edit", "dashboard",
)


def list_project_names() -> List[str]:
    """Registered project names, newest first — never raises.

    The registry is read through :func:`cklib.registry.list_projects`,
    which takes the cross-process ``*.lock`` (thread-/process-safe
    read). Any failure — missing file, empty registry, unreadable
    permissions, corrupt JSON, lock timeout — degrades to an empty
    list so shell completion stays silent instead of dumping a
    traceback into the user's prompt. Names are de-duplicated and
    newline-free (a name containing a newline would break the
    line-oriented protocol).
    """
    try:
        from .registry import has_registry, is_sandbox_path, list_projects

        # Lock-free existence probe first: a machine that never
        # registered anything must not get a lock file (or an empty
        # ~/.config/ck/) created by a mere TAB press.
        if not has_registry():
            return []
        entries = list_projects()
    except Exception:
        # Completion must degrade, never fail the prompt. Broad on
        # purpose: OSError, LockTimeoutError, RegistryCorruptError,
        # UnicodeDecodeError and malformed entries are all
        # "no suggestions".
        return []

    # ROUTING LEAK GUARD: registries polluted before the registration
    # check existed may still carry ``.sandbox/`` fixture entries.
    # Never suggest those in production — a TAB press must not route
    # the user into disposable mock data. A dev session keeps them:
    # that is the registry it is meant to read.
    production = True
    try:
        from .sandbox import dev_context_active
        production = not dev_context_active()
    except Exception:
        pass

    names: List[str] = []
    seen = set()
    for entry in entries:
        try:
            if production and is_sandbox_path(entry.path):
                continue
            raw = (entry.name or "").strip()
            if not raw:
                # Hand-edited registry with no display name: fall
                # back to the folder name so the entry stays usable.
                raw = Path(entry.path).name.strip()
        except Exception:
            continue
        if not raw or "\n" in raw or "\r" in raw or raw in seen:
            continue
        seen.add(raw)
        names.append(raw)
    return names


def list_space_names() -> List[str]:
    """Space names in the current sequence — never raises.

    Reads :func:`cklib.spaces.space_order` with the virtual
    ``project`` row included, so the result follows the persisted
    ``spaces_order`` (``~/.config/ck/state.json``) when present and
    falls back to the canonical default otherwise. Built-ins and
    custom spaces are included; the generated scripts apply the
    per-subcommand filtering (``project`` for ``rename``, protected
    built-ins for ``delete``, the first operand for ``swap``).

    A missing, unreadable or corrupt state file, a broken spaces tree
    or any other failure degrades to an empty list. Lock files and
    config directories are never created by a mere TAB press.
    """
    try:
        from .spaces import space_order

        order = space_order(include_project=True)
    except Exception:
        # Completion must degrade, never fail the prompt. Broad on
        # purpose: OSError, malformed state, broken sandbox paths and
        # unexpected registry shapes are all "no suggestions".
        return []

    names: List[str] = []
    seen = set()
    for item in order:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or "\n" in name or "\r" in name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _bash_commands() -> str:
    return " ".join(name for name, _ in COMPLETION_COMMANDS)


def bash_script() -> str:
    """The Bash completion script (source it, or ``eval`` it)."""
    return _BASH_SCRIPT.replace("__CK_COMMANDS__", _bash_commands())


def _zsh_commands() -> str:
    return "\n".join(f"        '{name}:{desc}'"
                     for name, desc in COMPLETION_COMMANDS)


def _zsh_space_actions() -> str:
    descriptions = {
        "add": "add a task",
        "list": "list tasks",
        "st": "detailed status view",
        "status": "detailed status view",
        "done": "complete task(s)",
        "focus": "focus a task",
        "start": "focus a task",
        "note": "attach a process note",
        "notes": "list active process notes",
        "edit": "open the space file in the editor",
        "dashboard": "global dashboard",
    }
    return "\n".join(f"                    '{a}:{descriptions[a]}'"
                     for a in _SPACE_ACTIONS)


def zsh_script() -> str:
    """The Zsh completion script (source it, or ``eval`` it)."""
    return (_ZSH_SCRIPT
            .replace("__CK_COMMAND_ENTRIES__", _zsh_commands())
            .replace("__CK_SPACE_ACTION_ENTRIES__", _zsh_space_actions()))


def script_for(shell: str) -> str:
    """Completion script for ``shell`` (``bash`` / ``zsh``).

    Raises :class:`ValueError` for an unsupported shell name; the CLI
    translates that into a clean usage error.
    """
    key = (shell or "").strip().lower()
    if key == "bash":
        return bash_script()
    if key == "zsh":
        return zsh_script()
    raise ValueError(
        f"Unsupported shell {shell!r}. Supported shells: "
        f"{', '.join(SUPPORTED_SHELLS)}."
    )


# ---------------------------------------------------------------------------
# Bash script
# ---------------------------------------------------------------------------

_BASH_SCRIPT = r"""# ck shell completion for Bash
#
#   eval "$(ck completion bash)"                          # this session
#   echo 'eval "$(ck completion bash)"' >> ~/.bashrc      # persist
#
# Registered project names come from the global registry
# (~/.config/ck/projects.json), read through `ck completion
# --list-projects`. Space names come from the persisted `spaces_order`
# (~/.config/ck/state.json) via `ck completion --list-spaces`. The
# helper binary is resolved per call by `_ck_bin`: a usable CK_BIN wins
# (a ck-dev session pins the checkout's launcher), otherwise the `ck`
# found on PATH is used. Missing, empty or unreadable sources simply
# yield no suggestions; stderr is discarded so they can never spray
# diagnostics into the prompt.

# Resolve the helper binary: "${CK_BIN:-ck}" with a stale-path guard.
# A CK_BIN left over from a removed checkout must not break completion.
_ck_bin() {
    if [ -n "${CK_BIN:-}" ] && [ -f "${CK_BIN}" ] && [ -x "${CK_BIN}" ]; then
        printf '%s\n' "${CK_BIN}"
        return 0
    fi
    command -v ck 2>/dev/null || printf '%s\n' ck
}

_ck_completion() {
    local cur prev cmd
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    # Newline-separated project names; kept as array elements so names
    # containing spaces survive intact.
    local __ck_projects=()
    local __ck_line __ck_name
    while IFS= read -r __ck_line; do
        if [ -n "$__ck_line" ]; then
            __ck_projects+=("$__ck_line")
        fi
    done < <("$(_ck_bin)" completion --list-projects 2>/dev/null)

    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "__CK_COMMANDS__" -- "$cur") )
        for __ck_name in "${__ck_projects[@]}"; do
            case "$__ck_name" in
                "$cur"*) COMPREPLY+=("$__ck_name") ;;
            esac
        done
        return 0
    fi

    cmd="${COMP_WORDS[1]}"
    case "$cmd" in
        st)
            # PROJECT NAME IS THE FIRST (AND ONLY) POSITIONAL. Once any
            # argument after `st` exists — a project name or a flag such
            # as -l / -e / -g / --all — there is nothing left to name:
            # switch to the valid options instead of repeating projects.
            if [[ "$cur" == -* ]] || [ "$COMP_CWORD" -gt 2 ]; then
                COMPREPLY=( $(compgen -W "-l --list -e --edit -g --global --all" -- "$cur") )
            else
                for __ck_name in "${__ck_projects[@]}"; do
                    case "$__ck_name" in
                        "$cur"*) COMPREPLY+=("$__ck_name") ;;
                    esac
                done
            fi
            ;;
        list|unregister)
            for __ck_name in "${__ck_projects[@]}"; do
                case "$__ck_name" in
                    "$cur"*) COMPREPLY+=("$__ck_name") ;;
                esac
            done
            ;;
        register)
            case "$prev" in
                --path) COMPREPLY=( $(compgen -d -- "$cur") ) ;;
                *) COMPREPLY=( $(compgen -W "-n --name --path" -- "$cur") ) ;;
            esac
            ;;
        space)
            if [ "$COMP_CWORD" -eq 2 ]; then
                COMPREPLY=( $(compgen -W "list create rename move swap reorder delete" -- "$cur") )
                return 0
            fi
            # SPACE ARGUMENTS: move / rename / delete take a space as
            # their FIRST argument; swap takes two. Names come from the
            # persisted spaces_order and keep that exact sequence.
            local __ck_action="${COMP_WORDS[2]}"
            local __ck_expect=0
            local __ck_skip=""
            case "$__ck_action" in
                move|rename)
                    if [ "$COMP_CWORD" -eq 3 ]; then
                        __ck_expect=1
                    fi
                    ;;
                swap)
                    if [ "$COMP_CWORD" -eq 3 ]; then
                        __ck_expect=1
                    elif [ "$COMP_CWORD" -eq 4 ]; then
                        __ck_expect=1
                        __ck_skip="${COMP_WORDS[3]}"
                    fi
                    ;;
                delete)
                    if [ "$COMP_CWORD" -eq 3 ]; then
                        __ck_expect=1
                    fi
                    ;;
            esac
            if [ "$__ck_expect" -eq 1 ]; then
                while IFS= read -r __ck_line; do
                    if [ -z "$__ck_line" ]; then
                        continue
                    fi
                    # `project` is virtual and cannot be renamed or
                    # deleted; local/remote are protected built-ins.
                    case "$__ck_action:$__ck_line" in
                        rename:project|delete:project|delete:local|delete:remote)
                            continue
                            ;;
                    esac
                    if [ -n "$__ck_skip" ] && [ "$__ck_line" = "$__ck_skip" ]; then
                        continue
                    fi
                    case "$__ck_line" in
                        "$cur"*) COMPREPLY+=("$__ck_line") ;;
                    esac
                done < <("$(_ck_bin)" completion --list-spaces 2>/dev/null)
            fi
            ;;
        local|remote)
            if [ "$COMP_CWORD" -eq 2 ]; then
                COMPREPLY=( $(compgen -W "add list st status done focus start note notes edit dashboard" -- "$cur") )
            fi
            ;;
        completion)
            if [ "$COMP_CWORD" -eq 2 ]; then
                COMPREPLY=( $(compgen -W "bash zsh" -- "$cur") )
            fi
            ;;
        *)
            # Unknown subcommand: explicitly empty. Top-level `ck`
            # completion must never fall back to arbitrary filenames.
            COMPREPLY=()
            ;;
    esac
    return 0
}

# No -o default / -o bashdefault: `ck <TAB>` is command + project
# completion only; arbitrary files from the cwd must never appear.
complete -F _ck_completion ck
"""


# ---------------------------------------------------------------------------
# Zsh script
# ---------------------------------------------------------------------------

_ZSH_SCRIPT = r"""#compdef ck
# ck shell completion for Zsh
#
#   autoload -Uz compinit && compinit     # once per shell (if needed)
#   eval "$(ck completion zsh)"           # this session
#   echo 'eval "$(ck completion zsh)"' >> ~/.zshrc   # persist
#
# Registered project names come from the global registry
# (~/.config/ck/projects.json), read through `ck completion
# --list-projects`. Space names come from the persisted `spaces_order`
# (~/.config/ck/state.json) via `ck completion --list-spaces`. The
# helper binary is resolved per call by `_ck_bin`: a usable CK_BIN wins
# (a ck-dev session pins the checkout's launcher), otherwise the `ck`
# found on PATH is used. Missing, empty or unreadable sources simply
# yield no suggestions; stderr is discarded so they can never spray
# diagnostics into the prompt.

# Resolve the helper binary: "${CK_BIN:-ck}" with a stale-path guard.
# A CK_BIN left over from a removed checkout must not break completion.
_ck_bin() {
    if [[ -n "${CK_BIN:-}" && -f "${CK_BIN}" && -x "${CK_BIN}" ]]; then
        print -r -- "${CK_BIN}"
        return 0
    fi
    command -v ck 2>/dev/null || print -r -- ck
}

_ck_projects() {
    local -a projects
    projects=(${(f)"$("$(_ck_bin)" completion --list-projects 2>/dev/null)"})
    (( ${#projects} )) || return 0
    _wanted projects expl 'registered project' compadd -a projects
}

_ck_spaces() {
    local -a spaces kept
    local candidate skip_name
    local -i keep
    spaces=(${(f)"$("$(_ck_bin)" completion --list-spaces 2>/dev/null)"})
    for candidate in "${spaces[@]}"; do
        keep=1
        for skip_name in "$@"; do
            if [[ "$candidate" == "$skip_name" ]]; then
                keep=0
                break
            fi
        done
        if (( keep )); then
            kept+=("$candidate")
        fi
    done
    (( ${#kept} )) || return 0
    _wanted spaces expl 'space' compadd -a kept
}

_ck_commands() {
    local -a commands
    commands=(
__CK_COMMAND_ENTRIES__
    )
    _describe -t commands 'ck command' commands
}

_ck() {
    local -a actions
    if (( CURRENT == 2 )); then
        _ck_commands
        _ck_projects
        return
    fi

    case $words[2] in
        st)
            local -a specs
            specs=(
                '(-l --list)'{-l,--list}'[print the task list]'
                '(-e --edit)'{-e,--edit}'[open PLAN.md in the editor]'
                '(-g --global)'{-g,--global}'[show the global dashboard]'
                '--all[include the full PLAN.md]'
            )
            # `_arguments` positions count COMMAND arguments, not
            # `words`: `st` is argument 1, so the project slot is
            # argument 2 even though it is completed at CURRENT == 3.
            # After a project name or a flag is present, offer options
            # only — never duplicate the project list.
            if (( CURRENT == 3 )); then
                specs+=('2:registered project:_ck_projects')
            fi
            _arguments "${specs[@]}"
            ;;
        list|unregister)
            _ck_projects
            ;;
        register)
            _arguments \
                '(-n --name)'{-n,--name}'[display name]:display name:' \
                '--path[project path]:project path:_files -/' \
                '1:registered project:_ck_projects'
            ;;
        space)
            if (( CURRENT == 3 )); then
                actions=(
                    'list:list spaces plus the active project'
                    'create:create a new space'
                    'rename:rename a space'
                    'move:move a space to a position'
                    'swap:swap two spaces'
                    'reorder:normalize the space order'
                    'delete:delete a space and its notes'
                )
                _describe -t actions 'space action' actions
                return
            fi
            # SPACE ARGUMENTS follow the persisted spaces_order. The
            # virtual `project` cannot be renamed/deleted, local/remote
            # are protected built-ins, and swap excludes its first pick.
            case $words[3] in
                move)
                    if (( CURRENT == 4 )); then
                        _ck_spaces
                    fi
                    ;;
                rename)
                    if (( CURRENT == 4 )); then
                        _ck_spaces project
                    fi
                    ;;
                swap)
                    if (( CURRENT == 4 )); then
                        _ck_spaces
                    elif (( CURRENT == 5 )); then
                        _ck_spaces "$words[4]"
                    fi
                    ;;
                delete)
                    if (( CURRENT == 4 )); then
                        _ck_spaces project local remote
                    fi
                    ;;
            esac
            ;;
        local|remote)
            if (( CURRENT == 3 )); then
                actions=(
__CK_SPACE_ACTION_ENTRIES__
                )
                _describe -t actions 'space action' actions
            fi
            ;;
        completion)
            if (( CURRENT == 3 )); then
                _values 'shell' bash zsh
            fi
            ;;
        *)
            # Unknown subcommand: no completion, and no fallback to
            # arbitrary files from the working directory.
            return 0
            ;;
    esac
}

if (( $+functions[compdef] )); then
    compdef _ck ck
fi
"""


__all__ = [
    "COMPLETION_COMMANDS",
    "SUPPORTED_SHELLS",
    "bash_script",
    "list_project_names",
    "list_space_names",
    "script_for",
    "zsh_script",
]
