#!/usr/bin/env bash
# install.sh — standalone one-line installer for Context Keeper (ck).
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/vinyardrip/context-keeper/main/install.sh | bash
#   ./install.sh              # same result when run from a checkout
#   ./install.sh --help
#
# The source is downloaded as a STREAM (``curl … | tar -xz``) straight
# into ``~/.local/share/context-keeper`` — no temporary ``.tar.gz`` on
# disk and no cloned ``.git`` directory. The top-level archive component
# is stripped so the files unpack directly into the install directory.
#
# A symlink ``~/.local/bin/ck`` is created (or updated) pointing at the
# launcher ``~/.local/share/context-keeper/ck``; the launcher carries a
# ``#!/usr/bin/env python3`` shebang and is marked executable, so the
# ``ck`` command runs it through ``python3``.
#
# No sudo, no root. Works on POSIX environments (Linux, macOS, WSL,
# Git Bash). Re-running is idempotent: the codebase is refreshed in
# place while user runtime data (``.ck/``) is preserved.
#
# Overrides (used by tests and custom installs):
#   CK_INSTALL_DIR  source directory   (default: ~/.local/share/context-keeper)
#   CK_BIN_DIR      symlink directory  (default: ~/.local/bin)
#   CK_TARBALL_URL  source tarball URL (default: GitHub main archive)
#   CK_REPO_OWNER / CK_REPO_BRANCH     GitHub owner / branch

set -euo pipefail

REPO_OWNER="${CK_REPO_OWNER:-vinyardrip}"
REPO_NAME="context-keeper"
REPO_BRANCH="${CK_REPO_BRANCH:-main}"
TARBALL_URL="${CK_TARBALL_URL:-https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${REPO_BRANCH}.tar.gz}"

ok()   { printf '[ok]  %s\n' "$*"; }
warn() { printf '[!]   %s\n' "$*"; }
fail() { printf '[x]   %s\n' "$*" >&2; }
info() { printf '      %s\n' "$*"; }

usage() {
    printf 'Usage: %s [install|uninstall]\n\n' "${0##*/}"
    printf 'Installs Context Keeper into ~/.local (no sudo required):\n'
    printf '  source  -> ~/.local/share/context-keeper\n'
    printf '  symlink -> ~/.local/bin/ck\n'
    printf '\nActions:\n'
    printf '  install      download and install (default)\n'
    printf '  uninstall    remove the source tree and the symlink\n'
    printf '\nOptions:\n'
    printf '  -h, --help   show this help and exit\n'
}

# ---------------------------------------------------------------------- #
# prerequisites                                                          #
# ---------------------------------------------------------------------- #

check_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        fail "python3 not found on PATH."
        info "Context Keeper requires Python 3.8 or newer."
        info "Install it first, then re-run this installer."
        exit 1
    fi
    if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
        fail "Python 3.8 or newer is required (found an older python3)."
        info "Upgrade Python, then re-run this installer."
        exit 1
    fi
}

check_command() {
    # $1 = command name, $2 = human-readable purpose
    if ! command -v "$1" >/dev/null 2>&1; then
        fail "required command '$1' not found on PATH ($2)."
        info "Install '$1' and re-run this installer."
        exit 1
    fi
}

check_prerequisites() {
    check_python
    check_command curl "needed to download the source"
    check_command tar  "needed to unpack the source"
}

# ---------------------------------------------------------------------- #
# install                                                                #
# ---------------------------------------------------------------------- #

prepare_target() {
    mkdir -p "$INSTALL_DIR"
    # Idempotent refresh: drop the previous codebase but keep user
    # runtime data (.ck/) so re-running updates cleanly.
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name '.ck' \
        -exec rm -rf {} + 2>/dev/null || true
}

download_and_extract() {
    # Preserve an existing .ck/ tree: exclude it from extraction so the
    # archive cannot overwrite user runtime data on re-install.
    local excludes=""
    if [ -d "${INSTALL_DIR}/.ck" ]; then
        excludes="--exclude=*/.ck --exclude=*/.ck/*"
    fi

    info "Downloading ${TARBALL_URL}"
    # shellcheck disable=SC2086  # intentional word splitting of $excludes
    if ! curl -fsSL "$TARBALL_URL" \
            | tar -xz -C "$INSTALL_DIR" --strip-components=1 $excludes; then
        fail "download or extraction failed."
        info "Check your network connection and try again."
        exit 1
    fi
}

link_launcher() {
    if [ ! -f "${INSTALL_DIR}/ck" ]; then
        fail "extraction did not produce ${INSTALL_DIR}/ck."
        exit 1
    fi

    mkdir -p "$BIN_DIR"
    if [ -d "$CK_LINK" ] && [ ! -L "$CK_LINK" ]; then
        fail "${CK_LINK} is a directory; refusing to replace it."
        exit 1
    fi

    chmod +x "${INSTALL_DIR}/ck"
    # Symlink to the launcher: the launcher resolves its own path, so
    # the adjacent cklib/ package under INSTALL_DIR is what executes.
    ln -sf "${INSTALL_DIR}/ck" "$CK_LINK"
    ok "Linked ${CK_LINK} -> ${INSTALL_DIR}/ck"
}

do_uninstall() {
    if [ -L "$CK_LINK" ]; then
        rm -f "$CK_LINK"
        ok "Removed symlink ${CK_LINK}"
    elif [ -e "$CK_LINK" ]; then
        warn "${CK_LINK} is not a symlink; leaving it untouched."
    else
        warn "Nothing installed at ${CK_LINK}."
    fi

    if [ -d "$INSTALL_DIR" ]; then
        rm -rf "$INSTALL_DIR"
        ok "Removed ${INSTALL_DIR}"
    fi
}

# ---------------------------------------------------------------------- #
# PATH guidance                                                          #
# ---------------------------------------------------------------------- #

shell_name() {
    local s="${SHELL:-${0:-}}"
    [ -n "$s" ] && printf '%s' "${s##*/}"
}

path_hint() {
    case ":${PATH:-}:" in
        *":${BIN_DIR}:"*)
            ok "${BIN_DIR} is already on your PATH."
            return 0
            ;;
    esac

    warn "${BIN_DIR} is not on your PATH."
    case "$(shell_name)" in
        fish)
            info "Add it to your fish PATH with:"
            printf '        fish_add_path "%s"\n' "$BIN_DIR"
            ;;
        zsh)
            info "Add it to ~/.zshrc with:"
            printf '        echo '\''export PATH="%s:$PATH"'\'' >> %s/.zshrc\n' \
                "$BIN_DIR" "$HOME_DIR"
            ;;
        *)
            info "Add it to ~/.bashrc with:"
            printf '        echo '\''export PATH="%s:$PATH"'\'' >> %s/.bashrc\n' \
                "$BIN_DIR" "$HOME_DIR"
            ;;
    esac
    info "Then reload it (e.g. 'source ~/.bashrc') or open a new terminal."
}

# ---------------------------------------------------------------------- #
# main                                                                   #
# ---------------------------------------------------------------------- #

main() {
    case "${1:-}" in
        -h|--help|help)
            usage
            exit 0
            ;;
    esac

    if [ -z "${HOME:-}" ]; then
        fail "\$HOME is not set; cannot determine install paths."
        exit 1
    fi

    HOME_DIR="${HOME}"
    INSTALL_DIR="${CK_INSTALL_DIR:-${HOME_DIR}/.local/share/context-keeper}"
    BIN_DIR="${CK_BIN_DIR:-${HOME_DIR}/.local/bin}"
    CK_LINK="${BIN_DIR}/ck"

    case "${1:-}" in
        ""|install)
            ;;
        uninstall|remove|rm)
            do_uninstall
            return 0
            ;;
        *)
            fail "unknown argument: $1"
            usage >&2
            exit 2
            ;;
    esac

    check_prerequisites
    prepare_target
    download_and_extract
    link_launcher
    path_hint

    printf '\n'
    ok "Context Keeper installed."
    info "Run 'ck -v' to verify the installation."
}

main "$@"
