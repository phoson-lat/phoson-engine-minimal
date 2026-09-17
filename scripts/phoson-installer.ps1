#requires -Version 5.1
<#
.SYNOPSIS
Install Phoson CLI for the current Windows user using uv and PyPI.
.DESCRIPTION
No administrator rights, Git, or preinstalled Python required. Downloads uv
from https://astral.sh if necessary; uv manages Python 3.12. Requires internet.
Does not change PowerShell profiles or persistent execution policy.
.EXAMPLE
.\phoson-installer.ps1
.EXAMPLE
.\phoson-installer.ps1 -Version 0.40.0 -CI
.EXAMPLE
.\phoson-installer.ps1 -Setup
#>
[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string] $Version,
    [switch] $CI,
    [switch] $SkipSetup,
    [switch] $Setup,
    [switch] $Help
)

function Invoke-PhosonNative {
    param([string] $Executable, [string[]] $Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit $LASTEXITCODE): $Executable $($Arguments -join ' ')"
    }
}

function Find-PhosonUv {
    $command = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    $candidate = Join-Path $HOME '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    return $null
}

function Install-PhosonUv {
    # A child process keeps the upstream script's exit/preferences isolated.
    $tempDirectory = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $tempDirectory -ErrorAction Stop
    $download = Join-Path $tempDirectory 'uv-install.ps1'
    $oldProtocol = [Net.ServicePointManager]::SecurityProtocol
    $oldNoModifyPath = $env:UV_NO_MODIFY_PATH
    $oldInstallDir = $env:UV_INSTALL_DIR
    try {
        [Net.ServicePointManager]::SecurityProtocol = $oldProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile $download -ErrorAction Stop
        $env:UV_NO_MODIFY_PATH = '1'
        $env:UV_INSTALL_DIR = Join-Path $HOME '.local\bin'
        $shell = Join-Path $PSHOME 'powershell.exe'
        if (Test-Path -LiteralPath (Join-Path $PSHOME 'pwsh.exe')) {
            $shell = Join-Path $PSHOME 'pwsh.exe'
        }
        Invoke-PhosonNative $shell @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $download)
    }
    finally {
        [Net.ServicePointManager]::SecurityProtocol = $oldProtocol
        $env:UV_NO_MODIFY_PATH = $oldNoModifyPath
        $env:UV_INSTALL_DIR = $oldInstallDir
        # Remove only this invocation's temporary download directory.
        Remove-Item -LiteralPath $tempDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Merge-PhosonPath {
    param([AllowNull()][string] $Current, [string] $Directory)
    $normalized = $Directory.TrimEnd('\', '/')
    foreach ($entry in ($Current -split ';')) {
        $expanded = [Environment]::ExpandEnvironmentVariables($entry.Trim().Trim('"')).TrimEnd('\', '/')
        if ($expanded -ieq $normalized) { return $Current }
    }
    if ([string]::IsNullOrEmpty($Current)) { return $Directory }
    return $Current.TrimEnd(';') + ';' + $Directory
}

function Add-PhosonUserPath {
    param([string] $Directory)
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $updated = Merge-PhosonPath $current $Directory
    if ($updated -cne $current) {
        [Environment]::SetEnvironmentVariable('Path', $updated, 'User')
    }
    $env:PATH = Merge-PhosonPath $env:PATH $Directory
}

function Assert-PhosonWindows {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'This installer requires Windows. On Linux/macOS use scripts/phoson-installer.sh.'
    }
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'A 64-bit Windows installation is required.'
    }
}

function Install-PhosonCli {
    param([string] $PackageVersion, [switch] $Configure)
    Assert-PhosonWindows
    $uv = Find-PhosonUv
    if (-not $uv) {
        Write-Host 'Installing uv for the current user...'
        Install-PhosonUv
        $uv = Find-PhosonUv
        if (-not $uv) { throw 'uv installation finished but uv.exe was not found.' }
    }
    Invoke-PhosonNative $uv @('--version')
    $package = 'phoson-engine-minimal'
    if ($PackageVersion) { $package += "==$PackageVersion" }
    Write-Host "Installing $package (uv manages Python 3.12)..."
    Invoke-PhosonNative $uv @('tool', 'install', '--python', '3.12', '--upgrade', $package)

    $binOutput = @(Invoke-PhosonNative $uv @('tool', 'dir', '--bin'))
    if ($binOutput.Count -ne 1) { throw 'uv did not return a single tool binary directory.' }
    $bin = ([string] $binOutput[0]).Trim()
    if (-not [IO.Path]::IsPathRooted($bin)) { throw 'uv returned a non-absolute tool directory.' }
    $cli = Join-Path $bin 'phoson-cli.exe'
    if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) {
        throw "Installation verification failed: $cli does not exist."
    }
    # Invoke the installed file directly, not an older executable on PATH.
    $reported = (Invoke-PhosonNative $cli @('--version') | Out-String).Trim()
    if ($reported -notmatch '^phoson-cli \d+\.\d+\.\d+') {
        throw "Unexpected CLI version output: $reported"
    }
    if ($PackageVersion -and $reported -ne "phoson-cli $PackageVersion") {
        throw "Expected phoson-cli $PackageVersion, received: $reported"
    }
    Add-PhosonUserPath (Split-Path -Parent $uv)
    Add-PhosonUserPath $bin
    Write-Host "$reported installed at $cli"
    Write-Host 'Open a new terminal, then run: phoson-cli'
    Write-Host 'Configure later: phoson-cli --setup'
    Write-Host 'Update: uv tool upgrade phoson-engine-minimal'
    Write-Host 'Uninstall: uv tool uninstall phoson-engine-minimal (settings are retained)'
    $resolved = Get-Command phoson-cli.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($resolved -and $resolved.Source -ine $cli) {
        Write-Warning "Another CLI takes precedence on PATH: $($resolved.Source). Use $cli or adjust PATH."
    }
    if ($Configure) { Invoke-PhosonNative $cli @('--setup') }
}

# Dot-sourcing defines helpers for tests without installing anything.
if ($MyInvocation.InvocationName -ne '.') {
    if ($Help) {
        Write-Output 'Usage: .\phoson-installer.ps1 [-Version X.Y.Z] [-Setup] [-CI] [-SkipSetup] [-Help]'
        Write-Output 'Installs for the current Windows user via uv and PyPI. Setup is opt-in.'
        Write-Output '-CI and -SkipSetup suppress setup even when -Setup is specified.'
    }
    else {
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Stop'
        try {
            Install-PhosonCli -PackageVersion $Version -Configure:($Setup -and -not $CI -and -not $SkipSetup)
        }
        finally { $ErrorActionPreference = $previousPreference }
    }
}
