#!/usr/bin/env sh
# Main installer for phoson-cli
# Handles uv detection, package installation, and setup wizard

set -e

# Configuration
PACKAGE_NAME="phoson-engine-minimal"
INSTALL_CMD="uv tool install --python 3.12 $PACKAGE_NAME"
UNINSTALL_CMD="uv tool uninstall $PACKAGE_NAME"
UPGRADE_CMD="uv tool upgrade $PACKAGE_NAME"
PHOSON_CLI="phoson-cli"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
RESET='\033[0m'

info() { printf "${GREEN}==>${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}==>${RESET} %s\n" "$*"; }
error() { printf "${RED}ERROR:${RESET} %s\n" "$*" >&2; }

detect_os() {
    case "$(uname -s)" in
        Linux*)  OS="linux";;
        Darwin*) OS="macos";;
        *)       error "Unsupported OS: $(uname -s)"; exit 1;;
    esac
    info "Detected OS: $OS"
}

detect_shell() {
    if [ -n "$ZSH_VERSION" ]; then
        SHELL="zsh"
    elif [ -n "$BASH_VERSION" ]; then
        SHELL="bash"
    elif [ -n "$FISH_VERSION" ]; then
        SHELL="fish"
    else
        SHELL="sh"
    fi
    info "Detected shell: $SHELL"
}

check_python() {
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        if [ "$PYTHON_VERSION" != "3.12" ] && [ "$PYTHON_VERSION" != "3.13" ]; then
            warn "Python $PYTHON_VERSION detected. uv will install Python 3.12."
        fi
    else
        warn "Python not found. uv will install Python 3.12."
    fi
}

install_dependencies() {
    info "Installing required dependencies (git, unzip)..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq && apt-get install -y -qq git unzip 2>/dev/null || {
            warn "Could not install dependencies via apt-get"
        }
    elif command -v yum >/dev/null 2>&1; then
        yum install -y git unzip 2>/dev/null || {
            warn "Could not install dependencies via yum"
        }
    elif command -v apk >/dev/null 2>&1; then
        apk add git unzip 2>/dev/null || {
            warn "Could not install dependencies via apk"
        }
    elif command -v brew >/dev/null 2>&1; then
        brew install git unzip 2>/dev/null || {
            warn "Could not install dependencies via brew"
        }
    else
        warn "Could not detect a package manager. Please install git and unzip manually."
    fi
}

check_uv() {
    if command -v uv >/dev/null 2>&1; then
        UV_VERSION=$(uv --version | cut -d' ' -f2)
        info "uv $UV_VERSION found"
        return 0
    fi
    return 1
}

install_uv() {
    warn "uv not found. Installing uv..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        error "Neither curl nor wget found. Cannot install uv."
        exit 1
    fi

    # Source the env file to get uv in PATH
    if [ -f "$HOME/.local/bin/env" ]; then
        . "$HOME/.local/bin/env"
    fi

    export PATH="$HOME/.local/bin:$PATH"

    if ! command -v uv >/dev/null 2>&1; then
        error "Failed to install uv"
        exit 1
    fi
    info "uv installed successfully"
}

check_pipx() {
    command -v pipx >/dev/null 2>&1
}

install_with_pipx() {
    warn "uv not found and --use-pipx specified. Using pipx."
    if ! command -v pipx >/dev/null 2>&1; then
        error "pipx not found. Install pipx first: pip install pipx"
        exit 1
    fi
    pipx install "$PACKAGE_NAME"
}

install_package() {
    info "Installing $PACKAGE_NAME..."

    # Create a temporary directory for the installation
    TEMP_DIR=$(mktemp -d)
    info "Downloading phoson-engine-minimal from GitHub..."
    cd "$TEMP_DIR"

    # Download the package from GitHub releases or repository
    # Using the main branch as source
    if command -v git >/dev/null 2>&1; then
        git clone --depth 1 https://github.com/phoson-lat/phoson-engine-minimal.git "$TEMP_DIR/phoson-engine-minimal" 2>/dev/null || {
            # Fallback: download as zip
            curl -sL https://github.com/phoson-lat/phoson-engine-minimal/archive/refs/heads/main.zip -o "$TEMP_DIR/package.zip"
            unzip -q "$TEMP_DIR/package.zip" -d "$TEMP_DIR"
            mv "$TEMP_DIR/phoson-engine-minimal-main" "$TEMP_DIR/phoson-engine-minimal"
        }
    else
        # Fallback: download as zip
        curl -sL https://github.com/phoson-lat/phoson-engine-minimal/archive/refs/heads/main.zip -o "$TEMP_DIR/package.zip"
        unzip -q "$TEMP_DIR/package.zip" -d "$TEMP_DIR"
        mv "$TEMP_DIR/phoson-engine-minimal-main" "$TEMP_DIR/phoson-engine-minimal"
    fi

    cd "$TEMP_DIR/phoson-engine-minimal"

    info "Running: uv tool install --python 3.12 ."
    if ! uv tool install --python 3.12 .; then
        error "Failed to install $PACKAGE_NAME"
        # Show the actual error
        uv tool install --python 3.12 . 2>&1 || true
        rm -rf "$TEMP_DIR"
        exit 1
    fi

    # Cleanup
    rm -rf "$TEMP_DIR"

    info "Package installed successfully"
}

verify_installation() {
    if command -v "$PHOSON_CLI" >/dev/null 2>&1; then
        VERSION=$("$PHOSON_CLI" --version 2>/dev/null || echo "unknown")
        info "Verification: $PHOSON_CLI $VERSION"
        return 0
    fi

    error "Installation verification failed: $PHOSON_CLI not found in PATH"
    error "You may need to restart your shell or add ~/.local/bin to your PATH"
    return 1
}

run_setup_wizard() {
    if [ -n "$CI_MODE" ]; then
        info "CI mode: skipping setup wizard"
        return 0
    fi

    # Check if we have a terminal (non-interactive mode)
    if [ ! -t 0 ]; then
        info "Non-interactive mode detected. Skipping setup wizard."
        info "Run 'phoson-cli --setup' later to configure."
        return 0
    fi

    info "Running setup wizard..."
    if command -v "$PHOSON_CLI" >/dev/null 2>&1; then
        "$PHOSON_CLI" --setup
    else
        error "Cannot run setup wizard: $PHOSON_CLI not found"
        return 1
    fi
}

print_next_steps() {
    echo ""
    info "${BOLD}Installation complete!${RESET}"
    echo ""
    echo "Next steps:"
    echo "  1. Restart your shell or run: source ~/.bashrc  (or equivalent)"
    echo "  2. Run: phoson-cli"
    echo ""
    echo "Update:   phoson-cli --self-update"
    echo "Uninstall: phoson-cli --uninstall"
    echo ""
}

main() {
    # Parse arguments
    CI_MODE=""
    USE_PIPX=""
    SKIP_SETUP=""

    while [ $# -gt 0 ]; do
        case "$1" in
        --ci)
            CI_MODE=1
            ;;
        --use-pipx)
            USE_PIPX=1
            ;;
        --skip-setup)
            SKIP_SETUP=1
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --ci          CI mode (skip setup wizard)"
            echo "  --use-pipx    Use pipx instead of uv"
            echo "  --skip-setup  Skip setup wizard"
            echo "  --help, -h    Show this help"
            exit 0
            ;;
        esac
        shift
    done

    info "${BOLD}phoson-cli installer${RESET}"

    detect_os
    detect_shell
    check_python
    install_dependencies

    # Install uv if needed
    if [ -n "$USE_PIPX" ]; then
        install_with_pipx
    else
        if check_uv; then
            : # uv already installed
        else
            install_uv
        fi
        install_package
    fi

    verify_installation || exit 1

    if [ -z "$SKIP_SETUP" ]; then
        run_setup_wizard
    fi

    print_next_steps
}

main "$@"
