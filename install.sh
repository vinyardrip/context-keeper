#!/usr/bin/env bash
# install.sh — install / check / uninstall Context Keeper (ck).
#
# Usage:
#   ./install.sh              # default: symlink ck into ~/.local/bin
#   ./install.sh check        # verify dependencies
#   ./install.sh uninstall    # remove the symlink
#
# This script does NOT require sudo. It targets ~/.local/bin/ck
# so users without root access can install the CLI. The
# corresponding uninstall step removes the symlink only.

set -euo pipefail

# Resolve the directory holding this script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CK_TARGET="${HOME}/.local/bin/ck"
CK_BIN_SRC="${SCRIPT_DIR}/ck"

log()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m\u2713\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m\u2717\033[0m %s\n' "$*"; }

ensure_local_bin() {
    mkdir -p "${HOME}/.local/bin"
}

# ---------------------------------------------------------------------- #
# default action: install                                                #
# ---------------------------------------------------------------------- #

do_install() {
    ensure_local_bin

    if [[ ! -f "${CK_BIN_SRC}" ]]; then
        fail "Cannot locate the 'ck' entry script under ${SCRIPT_DIR}"
        exit 1
    fi
    chmod +x "${CK_BIN_SRC}" || true

    if [[ -L "${CK_TARGET}" ]] || [[ -f "${CK_TARGET}" ]]; then
        if [[ -L "${CK_TARGET}" ]] && \
           [[ "$(readlink "${CK_TARGET}")" == "${CK_BIN_SRC}" ]]; then
            ok "Already installed: ${CK_TARGET} -> ${CK_BIN_SRC}"
            exit 0
        fi
        warn "${CK_TARGET} already exists."
        printf 'Overwrite? [y/N] '
        read -r reply
        if [[ "${reply}" != "y" && "${reply}" != "Y" ]]; then
            fail "Install cancelled."
            exit 1
        fi
        rm -f "${CK_TARGET}"
    fi

    if ln -s "${CK_BIN_SRC}" "${CK_TARGET}"; then
        ok "Installed: ${CK_TARGET} -> ${CK_BIN_SRC}"
    else
        fail "Symlink failed."
        exit 1
    fi

    # PATH hint
    if ! command -v ck >/dev/null 2>&1; then
        if [[ ":${PATH}:" != *":${HOME}/.local/bin:"* ]]; then
            warn "${HOME}/.local/bin is not on PATH."
            printf 'Add this to your shell rc:\n'
            printf '  export PATH="${HOME}/.local/bin:${PATH}"\n'
        fi
    fi
}

# ---------------------------------------------------------------------- #
# check action: environment diagnostics                                  #
# ---------------------------------------------------------------------- #

do_check() {
    log "Context Keeper environment check"
    log "---------------------------------"

    # Python
    if command -v python3 >/dev/null 2>&1; then
        py_version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
        py_major="$(python3 -c 'import sys; print(sys.version_info[0])')"
        py_minor="$(python3 -c 'import sys; print(sys.version_info[1])')"
        if [[ "${py_major}" -ge 3 && "${py_minor}" -ge 11 ]]; then
            ok "Python 3.11+ found: ${py_version}"
        else
            warn "Python ${py_version} found. Python 3.11+ is recommended."
        fi
    else
        fail "python3 not found on PATH."
    fi

    # Git
    if command -v git >/dev/null 2>&1; then
        git_version="$(git --version | awk '{print $3}')"
        ok "git found: ${git_version}"
    else
        fail "git not found on PATH."
    fi

    # $EDITOR
    if [[ -n "${EDITOR:-}" ]]; then
        if command -v "${EDITOR}" >/dev/null 2>&1; then
            ok "\$EDITOR set: ${EDITOR}"
        else
            warn "\$EDITOR is '${EDITOR}' but not found on PATH."
        fi
    else
        warn "\$EDITOR is not set. ck edit / ck log will fall back to micro/nano/vi."
    fi

    # ~/.local/bin writable
    if [[ -d "${HOME}/.local/bin" ]]; then
        if [[ -w "${HOME}/.local/bin" ]]; then
            ok "${HOME}/.local/bin is writable."
        else
            warn "${HOME}/.local/bin exists but is not writable."
        fi
    else
        warn "${HOME}/.local/bin does not exist (will be created on install)."
    fi

    # ck symlink
    if [[ -L "${CK_TARGET}" ]]; then
        ok "ck symlink present: ${CK_TARGET} -> $(readlink "${CK_TARGET}")"
    else
        warn "ck is not installed at ${CK_TARGET}."
    fi

    log "---------------------------------"
    log "Check complete."
}

# ---------------------------------------------------------------------- #
# uninstall action                                                        #
# ---------------------------------------------------------------------- #

do_uninstall() {
    if [[ ! -e "${CK_TARGET}" && ! -L "${CK_TARGET}" ]]; then
        warn "ck is not installed at ${CK_TARGET}."
        exit 0
    fi
    if [[ ! -L "${CK_TARGET}" ]]; then
        fail "${CK_TARGET} exists but is not a symlink. Refusing to delete."
        exit 1
    fi
    if rm -f "${CK_TARGET}"; then
        ok "Removed symlink: ${CK_TARGET}"
    else
        fail "Failed to remove ${CK_TARGET}."
        exit 1
    fi
}

# ---------------------------------------------------------------------- #
# dispatch                                                                #
# ---------------------------------------------------------------------- #

case "${1:-install}" in
    install|"")
        do_install
        ;;
    check)
        do_check
        ;;
    uninstall|remove|rm)
        do_uninstall
        ;;
    -h|--help|help)
        printf 'Usage: %s [install|check|uninstall]\n' "$0"
        ;;
    *)
        printf 'Unknown action: %s\n' "$1" >&2
        printf 'Usage: %s [install|check|uninstall]\n' "$0" >&2
        exit 2
        ;;
esac