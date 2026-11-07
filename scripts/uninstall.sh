#!/usr/bin/env sh
# Uninstall phoson-cli (standalone binary and/or uv-tool installs).
# Usage: curl -fsSL https://phoson.lat/uninstall | sh
#        ... | sh -s -- --purge     # also delete ~/.phoson (config, sessions)

set -eu

PACKAGE_NAME="phoson-engine-minimal"
INSTALL_DIR="${PHOSON_INSTALL_DIR:-$HOME/.local/share/phoson}"
BIN_DIR="${PHOSON_BIN_DIR:-$HOME/.local/bin}"
PURGE=""
[ "${1:-}" = "--purge" ] && PURGE=1

info() { printf '==> %s\n' "$*"; }

removed=""

if [ -f "$INSTALL_DIR/install-receipt.json" ]; then
    link="$BIN_DIR/phoson-cli"
    if [ -L "$link" ]; then
        case "$(readlink "$link")" in
            "$INSTALL_DIR"/*) rm -f "$link"; info "Removed $link" ;;
        esac
    fi
    rm -rf "$INSTALL_DIR"
    info "Removed $INSTALL_DIR"
    removed=1
fi

if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q "^$PACKAGE_NAME "; then
    uv tool uninstall "$PACKAGE_NAME"
    info "Removed the uv-tool installation"
    removed=1
elif command -v pipx >/dev/null 2>&1 && pipx list 2>/dev/null | grep -q "$PACKAGE_NAME"; then
    pipx uninstall "$PACKAGE_NAME"
    removed=1
fi

[ -n "$removed" ] || info "No phoson-cli installation found."

if [ -d "$HOME/.phoson" ]; then
    if [ -n "$PURGE" ]; then
        rm -rf "$HOME/.phoson"
        info "Removed ~/.phoson"
    elif [ -t 0 ]; then
        printf 'Remove ~/.phoson (config, sessions, plugins)? [y/N]: '
        read -r response
        case "$response" in
            y|Y|yes|YES) rm -rf "$HOME/.phoson"; info "Removed ~/.phoson" ;;
        esac
    else
        info "Kept ~/.phoson (re-run with --purge to delete it)."
    fi
fi

info "Uninstall complete."
