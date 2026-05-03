# Phoson CLI Installer — Design

**Date:** 2026-05-03
**Status:** Approved

---

## Overview

Design for a production-grade CLI installer (`curl | sh`) that installs `phoson-cli` globally and runs the interactive setup wizard.

## Architecture

```
┌─────────────────────────────────────────────┐
│  curl -L phoson.lat/install | sh            │  ← Bootstrap (200 lines)
└──────────────────┬──────────────────────────┘
                   │ fetch
                   ▼
┌─────────────────────────────────────────────┐
│  phoson-installer                           │  ← Main installer
│  ├── Detects shell and PATH                │
│  ├── Detects/installs uv if missing       │
│  ├── uv tool install phoson-engine-minimal │
│  ├── Runs phoson-cli --setup (wizard)     │
│  └── Verifies installation                  │
└─────────────────────────────────────────────┘
```

## Installation Flow

1. **Bootstrap** — `curl -L phoson.lat/install | sh`
2. **UV Detection** — Check if `uv` is installed; if not, offer to install it via official installer
3. **Package Installation** — `uv tool install --python 3.12 phoson-engine-minimal`
4. **Configuration** — Run `phoson-cli --setup` (interactive wizard)
5. **Verification** — `phoson-cli --version`

## Installer Features

| Feature | Description |
|---------|-------------|
| **Shell detection** | Detects bash/zsh/fish, ensures PATH is configured |
| **uv as primary** | Uses `uv tool install` for fast, isolated installation |
| **pipx fallback** | Falls back to `pipx` if uv is unavailable |
| **uv auto-install** | Installs uv via official script if not found |
| **Update** | `phoson-cli --self-update` via `uv tool upgrade` |
| **Uninstall** | `phoson-cli --uninstall` via `uv tool uninstall` |
| **Verbose mode** | `sh -c "$(curl ...)" -- --verbose` |
| **Dry-run mode** | `--dry-run` to inspect script without executing |
| **CI mode** | `--ci` for non-interactive environments |

## Shell Integration

Installer adds shim to PATH via:

- **Linux/macOS:** Symlink in `~/.local/bin` (standard)
- **fish shell:** Configures completions automatically

## File Structure

```
scripts/
  install.sh           ← Bootstrap script (served from phoson.lat/install)
  uninstall.sh         ← Uninstaller script
```

## Bootstrap Script Responsibilities

1. Fetch main installer script from `https://phoson.lat/scripts/install.sh`
2. Execute it with passed arguments
3. Pass through exit code

## Main Installer Responsibilities

1. Detect operating system (Linux/macOS)
2. Detect shell (bash/zsh/fish)
3. Check for `uv`, install if missing
4. Run `uv tool install --python 3.12 phoson-engine-minimal`
5. Execute `phoson-cli --setup` if not in CI mode
6. Print success message with next steps

## Self-Update

`phoson-cli --self-update`:
- Runs `uv tool upgrade phoson-engine-minimal`
- Restarts the REPL

## Uninstall

`phoson-cli --uninstall`:
- Runs `uv tool uninstall phoson-engine-minimal`
- Optionally removes `~/.phoson` config directory (with confirmation)

## CI Mode

When `--ci` is passed:
- Skip interactive wizard (`--setup` not run)
- Fail if uv is not installed
- Fail if Python version < 3.12
- Print only essential output

## Security Considerations

- Installer script served over HTTPS only
- Installer verifies checksum of downloaded packages
- API keys stored in `~/.phoson/config.toml` (0600 permissions)
