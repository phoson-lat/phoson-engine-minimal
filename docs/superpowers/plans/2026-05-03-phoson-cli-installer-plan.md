# Phoson CLI Installer — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a production-grade `curl | sh` installer that installs `phoson-cli` globally and runs the setup wizard.

**Architecture:** Two-script approach — a minimal bootstrap that fetches the full installer from `phoson.lat`. The main installer handles uv detection, package installation, wizard execution, and verification.

**Tech Stack:** POSIX sh, uv, Python 3.12+

---

## Chunk 1: Bootstrap Script

**Files:**
- Create: `scripts/install.sh`

### Steps

- [ ] **Step 1: Write bootstrap script**

```sh
#!/usr/bin/env sh
# Bootstrap installer for phoson-cli
# Usage: curl -L phoson.lat/install | sh
# Or:    sh -c "$(curl -L phoson.lat/install)"

set -e

INSTALLER_URL="${INSTALLER_URL:-https://phoson.lat/scripts/phoson-installer.sh}"
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
```

- [ ] **Step 2: Test dry-run mode**

Run: `sh scripts/install.sh --dry-run`
Expected: Prints fetch URL and exits 0

- [ ] **Step 3: Commit**

```bash
git add scripts/install.sh
git commit -m "feat(installer): add bootstrap install.sh script"
```

---

## Chunk 2: Main Installer Script

**Files:**
- Create: `scripts/phoson-installer.sh`

### Steps

- [ ] **Step 1: Write main installer script**

```sh
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

    if ! uv tool install --python 3.12 "$PACKAGE_NAME" 2>/dev/null; then
        error "Failed to install $PACKAGE_NAME"
        exit 1
    fi

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
```

- [ ] **Step 2: Make script executable**

Run: `chmod +x scripts/phoson-installer.sh`

- [ ] **Step 3: Test dry-run with CI mode**

Run: `sh scripts/phoson-installer.sh --ci --dry-run 2>&1 || echo "(expected: command not found)"`
Expected: --dry-run not implemented yet (will fail with "bad option")

- [ ] **Step 4: Test help**

Run: `sh scripts/phoson-installer.sh --help`
Expected: Shows usage information

- [ ] **Step 5: Commit**

```bash
git add scripts/phoson-installer.sh
git commit -m "feat(installer): add main installer script with uv detection"
```

---

## Chunk 3: Self-Update and Uninstall Commands

**Files:**
- Modify: `phoson_cli/__main__.py:11-20`

### Steps

- [ ] **Step 1: Add self-update and uninstall commands**

```python
"""Entry point for the Phoson CLI application."""

import sys
import asyncio
import subprocess
from pathlib import Path

from phoson_cli.repl import PhosonRepl
from phoson_cli.config import load_config
from phoson_cli.installer import run_install_wizard


async def self_update() -> None:
    """Upgrade phoson-cli to the latest version via uv."""
    print("Updating phoson-cli...")
    result = subprocess.run(
        ["uv", "tool", "upgrade", "phoson-engine-minimal"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("Update complete!")
    else:
        print(f"Update failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)


async def uninstall() -> None:
    """Remove phoson-cli and optionally config."""
    import shutil

    print("Uninstalling phoson-cli...")

    result = subprocess.run(
        ["uv", "tool", "uninstall", "phoson-engine-minimal"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("Package uninstalled.")
    else:
        print(f"Failed to uninstall package: {result.stderr}", file=sys.stderr)

    config_dir = Path.home() / ".phoson"
    if config_dir.exists():
        response = input("Remove ~/.phoson config directory? [y/N] ")
        if response.lower() in {"y", "yes"}:
            shutil.rmtree(config_dir)
            print("Config directory removed.")


def main() -> None:
    """Run the Phoson CLI REPL or setup wizard."""
    args = sys.argv[1:]

    if "--self-update" in args:
        asyncio.run(self_update())
        return

    if "--uninstall" in args:
        asyncio.run(uninstall())
        return

    if any(arg in {"--install", "--setup"} for arg in args):
        config = load_config()
        asyncio.run(run_install_wizard(config))
        return

    config = load_config()
    repl = PhosonRepl(config)
    asyncio.run(repl.run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run typecheck**

Run: `uv run ruff check phoson_cli/__main__.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add phoson_cli/__main__.py
git commit -m "feat(cli): add --self-update and --uninstall commands"
```

---

## Chunk 4: Uninstaller Script

**Files:**
- Create: `scripts/uninstall.sh`

### Steps

- [ ] **Step 1: Write uninstaller script**

```sh
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
```

- [ ] **Step 2: Make executable and commit**

```bash
chmod +x scripts/uninstall.sh
git add scripts/uninstall.sh
git commit -m "feat(installer): add uninstaller script"
```

---

## Chunk 5: End-to-End Test

**Files:**
- Test: `scripts/install.sh` and `scripts/phoson-installer.sh`

### Steps

- [ ] **Step 1: Test on clean environment (if possible)**

Test the following scenarios:
1. Fresh install with uv present
2. Install with uv auto-install
3. CI mode
4. Self-update
5. Uninstall

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "test(installer): add end-to-end test coverage"
```

---

## Summary

| Task | Files | Status |
|------|-------|--------|
| Bootstrap script | `scripts/install.sh` | pending |
| Main installer | `scripts/phoson-installer.sh` | pending |
| CLI commands | `phoson_cli/__main__.py` | pending |
| Uninstaller | `scripts/uninstall.sh` | pending |
| E2E tests | — | pending |
