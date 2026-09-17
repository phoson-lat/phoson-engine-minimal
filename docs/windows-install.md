# Install Phoson CLI on Windows

The PowerShell installer supports Windows PowerShell 5.1 and PowerShell 7 on
64-bit Windows. Run it as your normal user, not as administrator. Internet
access is required. It installs the published PyPI package using `uv`; no Git
or preinstalled Python is required. uv manages Python 3.12 automatically.

## From a checkout

Review `scripts/phoson-installer.ps1`, then run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\phoson-installer.ps1
```

`-ExecutionPolicy Bypass` applies only to this child process. The installer does
not modify persistent execution policy or your PowerShell profile. If an
organization policy disallows scripts, follow your administrator's policy
rather than trying to override it.

## Download after the installer is merged to main

The following URL is available **only after this script is published on main**:

```powershell
Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/phoson-lat/phoson-engine-minimal/main/scripts/phoson-installer.ps1' -OutFile '.\phoson-installer.ps1'
# Review the downloaded script before executing it.
Get-Content .\phoson-installer.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\phoson-installer.ps1
```

Use `pwsh` instead of `powershell.exe` if you prefer PowerShell 7. This is not
`curl ... | sh`: the Unix endpoint at `phoson.lat/install` serves a shell script,
not PowerShell. Do not pipe it to `Invoke-Expression`.

## Options

```powershell
# Pin a published stable version; omit -Version for the latest stable package.
.\scripts\phoson-installer.ps1 -Version 0.40.0

# Setup is opt-in; ordinary installation never opens a wizard.
.\scripts\phoson-installer.ps1 -Setup

# Both flags suppress setup, even if -Setup is also present.
.\scripts\phoson-installer.ps1 -CI
.\scripts\phoson-installer.ps1 -SkipSetup
.\scripts\phoson-installer.ps1 -Help
```

If uv is absent, the installer downloads Astral's official installer over HTTPS
and runs it in a child PowerShell process. Phoson uses `uv tool install --python
3.12 --upgrade phoson-engine-minimal`, checks the installed executable directly,
and adds uv and `uv tool dir --bin` to the **user** PATH without replacing its
existing contents. Custom `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` are respected.
This installs the CLI package, not every optional provider/plugin extra.

Open a **new terminal** after installation so it inherits the updated PATH:

```powershell
phoson-cli --version
phoson-cli --setup
phoson-cli
```

A script launched in a child process cannot update the parent terminal's PATH.
If an older CLI elsewhere takes precedence, use the full executable path
printed by the installer and inspect `Get-Command phoson-cli.exe -All`.

## Update and uninstall

```powershell
uv tool upgrade phoson-engine-minimal
uv tool uninstall phoson-engine-minimal
```

Uninstalling the tool retains your Phoson configuration and sessions. Neither
installation nor reinstallation deletes your existing Phoson data. Close any
running CLI before upgrading if Windows reports a file-lock error.

## Testing

`tests/scripts/test_windows_installer.ps1` tests native command errors,
PATH merging, pinned/latest installs, verification, setup opt-in, and bootstrap
control flow using local doubles (no downloads or persistent PATH writes).

The `windows-installer` workflow runs the contract tests and an actual PyPI
installation on `windows-latest` under both Windows PowerShell 5.1 and PowerShell
7. It tests paths containing spaces, repeat installs, persisted user PATH, and
setup suppression. Linux PowerShell tests alone do not verify Windows behavior.
