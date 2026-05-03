#!/usr/bin/env sh
# Bootstrap installer for phoson-cli
# Usage: curl -L phoson.lat/install | sh
# Or:    sh -c "$(curl -L phoson.lat/install)"

set -e

INSTALLER_URL="${INSTALLER_URL:-https://github.com/phoson-lat/phoson-engine-minimal/releases/latest/download/phoson-installer.sh}"
INSTALLER_SHA="${INSTALLER_SHA:-}"

main() {
    parse_args "$@"

    if [ -n "$DRY_RUN" ]; then
        echo "[dry-run] Would fetch installer from: $INSTALLER_URL"
        exit 0
    fi

    printf '%s\n' "Fetching phoson installer..."
    INSTALLER=$(mktemp)
    trap 'rm -f "$INSTALLER"' EXIT

    if ! curl -fsSL "$INSTALLER_URL" -o "$INSTALLER"; then
        echo "Error: Failed to fetch installer from $INSTALLER_URL" >&2
        exit 1
    fi

    chmod +x "$INSTALLER"

    if [ -n "$VERBOSE" ]; then
        sh -x "$INSTALLER" "$@"
    else
        sh "$INSTALLER" "$@"
    fi
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
        --installer-url=*)
            INSTALLER_URL="${1#*=}"
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        --verbose|-v)
            VERBOSE=1
            ;;
        --ci)
            CI_MODE=1
            ;;
        esac
        shift
    done
}

main "$@"
