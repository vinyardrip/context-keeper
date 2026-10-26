"""Shell completion script generation for Bash and Zsh.

``ck completion <shell>`` prints a self-contained completion script to
STDOUT so it can be sourced directly::

    eval "$(ck completion bash)"    # or: zsh

The generated scripts are deliberately small and navigation-focused:
the top-level completion offers the core commands plus the registered
project names, and the commands that accept a project target
(``ck st <project>`` / ``ck list <project>`` / ``ck unregister
<project>``) complete project names.

PROJECT NAME SOURCE: the scripts never parse JSON themselves. They
call ``ck completion --list-projects``, which reads the global registry
(``~/.config/ck/projects.json``) through :mod:`cklib.registry` — the
same :func:`cklib.config.file_lock`-guarded read every other registry
consumer uses, so a concurrent writer can never hand the completion
a torn file. A missing, empty, unreadable or corrupt registry simply
yields no names; the helper never raises and never prints a traceback
(the shell side also discards stderr, so a broken registry can never
spray diagnostics into the prompt).
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
        from .registry import has_registry, list_projects

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

    names: List[str] = []
    seen = set()
    for entry in entries:
        try:
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
# --list-projects`. A missing, empty or unreadable registry simply
# yields no project suggestions; stderr is discarded so a broken
# registry can never spray diagnostics into the prompt.

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
    done < <(command ck completion --list-projects 2>/dev/null)

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
            if [[ "$cur" == -* ]]; then
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
    esac
    return 0
}

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
# --list-projects`. A missing, empty or unreadable registry simply
# yields no project suggestions; stderr is discarded so a broken
# registry can never spray diagnostics into the prompt.

_ck_projects() {
    local -a projects
    projects=(${(f)"$(command ck completion --list-projects 2>/dev/null)"})
    (( ${#projects} )) || return 0
    _wanted projects expl 'registered project' compadd -a projects
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
            _arguments \
                '(-l --list)'{-l,--list}'[print the task list]' \
                '(-e --edit)'{-e,--edit}'[open PLAN.md in the editor]' \
                '(-g --global)'{-g,--global}'[show the global dashboard]' \
                '--all[include the full PLAN.md]' \
                '1:registered project:_ck_projects'
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
            fi
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
    "script_for",
    "zsh_script",
]
