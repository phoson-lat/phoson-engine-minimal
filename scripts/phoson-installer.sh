#!/usr/bin/env sh
# phoson-cli installer for Linux and macOS.
#
#   curl -fsSL https://phoson.lat/install | sh
#   curl -fsSL https://phoson.lat/install | sh -s -- --version 0.58.0
#
# Default method: the prebuilt standalone binary from the GitHub release
# (no Python, uv or root needed). The archive is verified against the
# release's SHA256SUMS, unpacked into a versioned directory and linked into
# ~/.local/bin:
#
#   ~/.local/share/phoson/
#   ├── install-receipt.json
#   ├── current -> versions/<version>
#   └── versions/<version>/phoson-cli/   (launcher + _internal/)
#   ~/.local/bin/phoson-cli -> ~/.local/share/phoson/current/phoson-cli/phoson-cli
#
# `phoson-cli --self-update` updates this layout in place and
# `phoson-cli --uninstall` removes it.
#
# Fallback method (`--method uv`, or automatically when no binary fits this
# system, e.g. musl/Alpine or an old glibc): the released package from PyPI
# with `uv tool install` (uv is installed for the user if missing).
#
# Overrides (mainly for testing and mirrors):
#   PHOSON_RELEASE_BASE  default https://github.com/phoson-lat/phoson-engine-minimal/releases
#   PHOSON_INSTALL_DIR   default ~/.local/share/phoson
#   PHOSON_BIN_DIR       default ~/.local/bin

set -eu

PACKAGE_NAME="phoson-engine-minimal"
PHOSON_CLI="phoson-cli"
RELEASE_BASE="${PHOSON_RELEASE_BASE:-https://github.com/phoson-lat/phoson-engine-minimal/releases}"
INSTALL_DIR="${PHOSON_INSTALL_DIR:-$HOME/.local/share/phoson}"
BIN_DIR="${PHOSON_BIN_DIR:-$HOME/.local/bin}"

if [ -t 1 ]; then
    RED=$(printf '\033[0;31m'); GREEN=$(printf '\033[0;32m')
    YELLOW=$(printf '\033[1;33m'); BOLD=$(printf '\033[1m'); RESET=$(printf '\033[0m')
else
    RED=""; GREEN=""; YELLOW=""; BOLD=""; RESET=""
fi

info() { printf '%s==>%s %s\n' "$GREEN" "$RESET" "$*" >&2; }
warn() { printf '%s==>%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
error() { printf '%sERROR:%s %s\n' "$RED" "$RESET" "$*" >&2; }
die() { error "$*"; exit 1; }

usage() {
    cat <<EOF
Usage: phoson-installer.sh [OPTIONS]

Options:
  --version X.Y.Z     Install this version (default: latest release)
  --method M          binary (default) or uv (PyPI package via uv)
  --skip-setup        Do not run the setup wizard
  --ci                Non-interactive: implies --skip-setup
  -h, --help          Show this help
EOF
}

# ── Helpers ──────────────────────────────────────────────────────────────────

fetch() { # fetch URL DEST
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 2 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        die "Neither curl nor wget is available."
    fi
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        return 1
    fi
}

platform_tag() {
    case "$(uname -s)" in
        Linux) os=linux ;;
        Darwin) os=darwin ;;
        *) return 1 ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) arch=x86_64 ;;
        arm64|aarch64) arch=arm64 ;;
        *) return 1 ;;
    esac
    # An x86_64 shell under Rosetta on Apple Silicon: use the native build.
    if [ "$os" = darwin ] && [ "$arch" = x86_64 ] &&
        [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = 1 ]; then
        arch=arm64
    fi
    # Release binaries are built against glibc; musl (Alpine) cannot run them.
    if [ "$os" = linux ] && (ldd --version 2>&1 || true) | grep -qi musl; then
        return 1
    fi
    printf '%s-%s\n' "$os" "$arch"
}

download_base() {
    if [ -n "$VERSION" ]; then
        printf '%s/download/v%s\n' "$RELEASE_BASE" "$VERSION"
    else
        printf '%s/latest/download\n' "$RELEASE_BASE"
    fi
}

# ── Binary method ────────────────────────────────────────────────────────────

# Sets INSTALLED_VERSION on success. Returns non-zero (with a reason on
# stderr) when this system has no usable binary, so the caller can fall back;
# integrity failures (checksum, bad archive) abort the whole installer.
# Runs in the main shell (not in $(...)) so that `die` really exits.
install_binary() {
    tag=$(platform_tag) || { warn "No prebuilt binary for $(uname -s)/$(uname -m)."; return 1; }
    asset="phoson-cli-$tag.tar.gz"
    base=$(download_base)
    work=$(mktemp -d)
    WORK_DIRS="$WORK_DIRS $work"

    info "Downloading $asset"
    if ! fetch "$base/$asset" "$work/$asset" || ! fetch "$base/SHA256SUMS" "$work/SHA256SUMS"; then
        warn "Could not download $asset from $base."
        return 1
    fi
    expected=$(awk -v name="$asset" '{ f=$2; sub(/^\*/, "", f) } f == name { print $1 }' "$work/SHA256SUMS")
    [ -n "$expected" ] || die "$asset is not listed in SHA256SUMS; refusing to install."
    actual=$(sha256_of "$work/$asset") || die "Install sha256sum or shasum to verify the download."
    [ "$actual" = "$expected" ] || die "Checksum mismatch for $asset (expected $expected, got $actual)."
    info "Checksum verified"

    mkdir -p "$INSTALL_DIR/versions"
    staging=$(mktemp -d "$INSTALL_DIR/versions/.staging.XXXXXX")
    tar -xzf "$work/$asset" -C "$staging"
    launcher="$staging/phoson-cli/phoson-cli"
    [ -x "$launcher" ] || { rm -rf "$staging"; die "The archive does not contain a phoson-cli bundle."; }
    if [ "$(uname -s)" = Darwin ]; then
        xattr -dr com.apple.quarantine "$staging" 2>/dev/null || true
    fi

    # Smoke test before switching: a binary that cannot start here (e.g. an
    # older glibc than the build machine) must not replace a working install.
    if ! reported=$("$launcher" --version 2>/dev/null); then
        rm -rf "$staging"
        warn "The prebuilt binary does not run on this system."
        return 1
    fi
    version=$(printf '%s\n' "$reported" | awk '{ print $2 }')
    [ -n "$version" ] || { rm -rf "$staging"; die "Unexpected version output: $reported"; }

    rm -rf "$INSTALL_DIR/versions/$version"
    mv "$staging" "$INSTALL_DIR/versions/$version"
    rm -f "$INSTALL_DIR/current"
    ln -s "versions/$version" "$INSTALL_DIR/current"

    mkdir -p "$BIN_DIR"
    link="$BIN_DIR/$PHOSON_CLI"
    if [ -e "$link" ] && [ ! -L "$link" ]; then
        die "$link exists and is not a link; move it away and re-run the installer."
    fi
    rm -f "$link"
    ln -s "$INSTALL_DIR/current/phoson-cli/phoson-cli" "$link"

    cat >"$INSTALL_DIR/install-receipt.json" <<EOF
{
  "method": "binary",
  "version": "$version",
  "platform": "$tag",
  "bin_link": "$link"
}
EOF

    # Keep the new version and the one it replaced; drop older ones.
    # shellcheck disable=SC2012
    ls -1t "$INSTALL_DIR/versions" | tail -n +3 | while IFS= read -r old; do
        case "$old" in .staging.*) continue ;; esac
        rm -rf "$INSTALL_DIR/versions/$old"
    done

    INSTALLED_VERSION="$version"
}

# ── uv method ────────────────────────────────────────────────────────────────

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi
    warn "uv not found; installing uv for the current user (https://astral.sh/uv)."
    work=$(mktemp -d)
    fetch "https://astral.sh/uv/install.sh" "$work/uv-install.sh"
    sh "$work/uv-install.sh"
    rm -rf "$work"
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv installation failed."
}

install_uv() {
    ensure_uv
    requirement="$PACKAGE_NAME"
    [ -z "$VERSION" ] || requirement="$PACKAGE_NAME==$VERSION"
    info "Installing $requirement from PyPI with uv (uv manages Python 3.12)"
    uv tool install --upgrade --python 3.12 "$requirement"
}

uv_tool_installed() {
    command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q "^$PACKAGE_NAME "
}

# ── Main ─────────────────────────────────────────────────────────────────────

WORK_DIRS=""
INSTALLED_VERSION=""
# shellcheck disable=SC2086
trap 'rm -rf $WORK_DIRS' EXIT

VERSION=""
METHOD="binary"
METHOD_EXPLICIT=""
SKIP_SETUP=""

while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="${2:-}"; shift ;;
        --version=*) VERSION="${1#*=}" ;;
        --method) METHOD="${2:-}"; METHOD_EXPLICIT=1; shift ;;
        --method=*) METHOD="${1#*=}"; METHOD_EXPLICIT=1 ;;
        --use-pipx) die "--use-pipx is no longer supported; use --method uv." ;;
        --ci|--skip-setup) SKIP_SETUP=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown option: $1 (see --help)" ;;
    esac
    shift
done
VERSION="${VERSION#v}"
case "$METHOD" in binary|uv) ;; *) die "--method must be binary or uv" ;; esac

info "${BOLD}phoson-cli installer${RESET}"

if [ "$METHOD" = binary ]; then
    if install_binary; then
        info "Installed phoson-cli $INSTALLED_VERSION (standalone binary)"
        if uv_tool_installed; then
            warn "Removing the previous uv-tool installation of $PACKAGE_NAME (your ~/.phoson config is kept)."
            uv tool uninstall "$PACKAGE_NAME" >/dev/null 2>&1 || true
        fi
    elif [ -n "$METHOD_EXPLICIT" ]; then
        die "Binary installation failed; re-run with --method uv to install from PyPI."
    else
        warn "Falling back to the PyPI package (uv)."
        METHOD=uv
        install_uv
    fi
else
    install_uv
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) on_path=1 ;;
    *) on_path="" ;;
esac
if [ -z "$on_path" ] && [ "$METHOD" = binary ]; then
    warn "$BIN_DIR is not on your PATH. Add it, for example:"
    printf '    echo '\''export PATH="%s:$PATH"'\'' >> ~/.%src\n' "$BIN_DIR" "$(basename "${SHELL:-sh}")"
    CLI="$BIN_DIR/$PHOSON_CLI"
else
    CLI="$PHOSON_CLI"
fi

if [ -z "$SKIP_SETUP" ] && [ -t 0 ] && [ -t 1 ]; then
    info "Running the setup wizard (re-run any time with: phoson-cli --setup)"
    "$CLI" --setup || warn "Setup did not finish; run 'phoson-cli --setup' later."
elif [ -z "$SKIP_SETUP" ]; then
    info "Non-interactive shell: run 'phoson-cli --setup' to configure."
fi

printf '\n'
info "${BOLD}Done.${RESET} Start with: phoson-cli"
printf '    Update:    phoson-cli --self-update\n'
printf '    Uninstall: phoson-cli --uninstall\n'
