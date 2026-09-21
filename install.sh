#!/usr/bin/env bash
# install.sh — install / check / uninstall Context Keeper (ck).
#
# Usage:
#   ./install.sh              # default: physical-copy ck into ~/.local/bin
#   ./install.sh check        # verify dependencies
#   ./install.sh uninstall    # remove the installation
#
# This script does NOT require sudo. It installs a PHYSICAL COPY of the
# launcher at ${USER_BIN:-~/.local/bin}/ck (a regular executable file,
# NEVER a symlink) plus a static snapshot of the cklib package at
# ~/.local/share/ck/cklib, so the installed command stays decoupled
# from this checkout until the install (or `ck update`) is re-run.
# The uninstall step removes the copy, the snapshot, and any legacy
# ~/.local/bin/ck-dev dev entrypoint.

set -euo pipefail

# Resolve the directory holding this script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${USER_BIN:-${HOME}/.local/bin}"
CK_TARGET="${BIN_DIR}/ck"
CK_DEV_TARGET="${BIN_DIR}/ck-dev"
SNAPSHOT_DIR="${HOME}/.local/share/ck"
CK_BIN_SRC="${SCRIPT_DIR}/ck"
CK_LIB_SRC="${SCRIPT_DIR}/cklib"

log()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m\u2713\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m\u2717\033[0m %s\n' "$*"; }

ensure_local_bin() {
    mkdir -p "${BIN_DIR}"
}

# ---------------------------------------------------------------------- #
# default action: install (physical copy — no symlinks)                  #
# ---------------------------------------------------------------------- #

do_install() {
    ensure_local_bin

    if [[ ! -f "${CK_BIN_SRC}" ]]; then
        fail "Cannot locate the 'ck' entry script under ${SCRIPT_DIR}"
        exit 1
    fi
    if [[ ! -d "${CK_LIB_SRC}" ]]; then
        fail "Cannot locate the 'cklib' package under ${SCRIPT_DIR}"
        exit 1
    fi
    chmod +x "${CK_BIN_SRC}" || true

    # Static package snapshot (production isolation): the installed
    # launcher resolves this copy, never the live checkout.
    rm -rf "${SNAPSHOT_DIR}/cklib"
    mkdir -p "${SNAPSHOT_DIR}"
    cp -R "${CK_LIB_SRC}" "${SNAPSHOT_DIR}/cklib"
    find "${SNAPSHOT_DIR}" -type d -name '__pycache__' -prune \
        -exec rm -rf {} + 2>/dev/null || true

    # Force-remove any existing entry (symlink or file): the target
    # must be a fresh regular file — never a copy THROUGH a symlink.
    rm -f "${CK_TARGET}"
    cp "${CK_BIN_SRC}" "${CK_TARGET}"
    chmod 0755 "${CK_TARGET}"

    ok "Installed (physical copy): ${CK_TARGET}"
    ok "Package snapshot: ${SNAPSHOT_DIR}/cklib"
    warn "Production is a static snapshot: re-run the install (or \`ck update\`) after changing this checkout."

    # PATH hint
    if ! command -v ck >/dev/null 2>&1; then
        if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
            warn "${BIN_DIR} is not on PATH."
            printf 'Add this to your shell rc:\n'
            printf '  export PATH="%s:${PATH}"\n' "${BIN_DIR}"
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

    # bin dir writable
    if [[ -d "${BIN_DIR}" ]]; then
        if [[ -w "${BIN_DIR}" ]]; then
            ok "${BIN_DIR} is writable."
        else
            warn "${BIN_DIR} exists but is not writable."
        fi
    else
        warn "${BIN_DIR} does not exist (will be created on install)."
    fi

    # ck installation
    if [[ -L "${CK_TARGET}" ]]; then
        warn "ck is a SYMLINK (legacy install): ${CK_TARGET} -> $(readlink "${CK_TARGET}")."
        warn "Re-run the install to replace it with a physical copy."
    elif [[ -f "${CK_TARGET}" ]]; then
        ok "ck installed (physical copy): ${CK_TARGET}"
        if [[ -d "${SNAPSHOT_DIR}/cklib" ]]; then
            ok "Package snapshot present: ${SNAPSHOT_DIR}/cklib"
        else
            warn "Package snapshot missing: ${SNAPSHOT_DIR}/cklib (re-run the install)."
        fi
    else
        warn "ck is not installed at ${CK_TARGET}."
    fi

    log "---------------------------------"
    log "Check complete."
}

# ---------------------------------------------------------------------- #
# uninstall action                                                       #
# ---------------------------------------------------------------------- #

do_uninstall() {
    local target
    for target in "${CK_TARGET}" "${CK_DEV_TARGET}"; do
        if [[ ! -e "${target}" && ! -L "${target}" ]]; then
            warn "Not installed at ${target}."
            continue
        fi
        if [[ -d "${target}" && ! -L "${target}" ]]; then
            fail "${target} is a directory. Refusing to delete."
            continue
        fi
        rm -f "${target}"
        ok "Removed: ${target}"
    done

    if [[ -d "${SNAPSHOT_DIR}/cklib" ]]; then
        rm -rf "${SNAPSHOT_DIR}"
        ok "Removed package snapshot: ${SNAPSHOT_DIR}"
    fi
}

# ---------------------------------------------------------------------- #
# dispatch                                                               #
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
