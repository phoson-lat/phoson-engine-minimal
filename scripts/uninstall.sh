#!/usr/bin/env sh
# Uninstall phoson-cli
# Usage: curl -L phoson.lat/uninstall | sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
RESET='\033[0m'

info() { printf "${GREEN}==>${RESET} %s\n" "$*"; }
error() { printf "${RED}ERROR:${RESET} %s\n" "$*" >&2; }

main() {
    info "Uninstalling phoson-cli..."

    if command -v uv >/dev/null 2>&1; then
        uv tool uninstall phoson-engine-minimal
    elif command -v pipx >/dev/null 2>&1; then
        pipx uninstall phoson-engine-minimal
    else
        error "Neither uv nor pipx found. Please uninstall manually."
        exit 1
    fi

    info "Package uninstalled."

    if [ -d "$HOME/.phoson" ]; then
        printf "Remove ~/.phoson config? [y/N]: "
        read -r response
        if [ "$response" = "y" ] || [ "$response" = "yes" ]; then
            rm -rf "$HOME/.phoson"
            info "Config directory removed."
        fi
    fi

    info "Uninstall complete."
}

main "$@"
