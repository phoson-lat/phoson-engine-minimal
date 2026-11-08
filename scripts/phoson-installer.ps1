#requires -Version 5.1
<#
.SYNOPSIS
Install Phoson CLI for the current Windows user.
.DESCRIPTION
Default (-Method binary): the prebuilt standalone bundle from the GitHub
release, verified against SHA256SUMS and unpacked into
%LOCALAPPDATA%\Phoson\versions\<version>; a directory junction
%LOCALAPPDATA%\Phoson\current points at it and current\phoson-cli is added
to the user PATH. No Python or uv needed. `phoson-cli --self-update` and
`phoson-cli --uninstall` manage this layout.
Fallback (-Method uv, or automatically when no usable binary exists): the
released package from PyPI via uv (downloaded from https://astral.sh if
needed; uv manages Python 3.12).
No administrator rights required. Does not change PowerShell profiles or
persistent execution policy. PHOSON_RELEASE_BASE / PHOSON_INSTALL_DIR
override the release location and install root (testing, mirrors).
.EXAMPLE
.\phoson-installer.ps1
.EXAMPLE
.\phoson-installer.ps1 -Version 0.40.0 -CI
.EXAMPLE
.\phoson-installer.ps1 -Setup
.EXAMPLE
.\phoson-installer.ps1 -Method uv
#>
[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string] $Version,
    [ValidateSet('binary', 'uv')]
    [string] $Method = 'binary',
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

function Get-PhosonReleaseBase {
    param([string] $PackageVersion)
    $root = $env:PHOSON_RELEASE_BASE
    if (-not $root) { $root = 'https://github.com/phoson-lat/phoson-engine-minimal/releases' }
    if ($root -match '^https?://') {
        if ($PackageVersion) { return "$root/download/v$PackageVersion" }
        return "$root/latest/download"
    }
    # A local directory laid out like a release (tests, offline mirrors).
    if ($PackageVersion) { return (Join-Path $root "download\v$PackageVersion") }
    return (Join-Path $root 'latest\download')
}

function Get-PhosonAsset {
    param([string] $Base, [string] $Name, [string] $Destination)
    if ($Base -match '^https?://') {
        $oldProtocol = [Net.ServicePointManager]::SecurityProtocol
        try {
            [Net.ServicePointManager]::SecurityProtocol = $oldProtocol -bor [Net.SecurityProtocolType]::Tls12
            # GitHub's release CDN returns transient 5xx/504s at times: retry
            # with backoff (2+4+8+16 s) before giving up.
            $delay = 2
            for ($attempt = 1; ; $attempt++) {
                try {
                    Invoke-WebRequest -UseBasicParsing -Uri "$Base/$Name" -OutFile $Destination -ErrorAction Stop
                    break
                }
                catch {
                    if ($attempt -ge 5) { throw }
                    Start-Sleep -Seconds $delay
                    $delay *= 2
                }
            }
        }
        finally { [Net.ServicePointManager]::SecurityProtocol = $oldProtocol }
    }
    else {
        Copy-Item -LiteralPath (Join-Path $Base $Name) -Destination $Destination -ErrorAction Stop
    }
}

function Install-PhosonBinary {
    # Returns $true when installed, $false when no usable binary exists (the
    # caller may fall back to uv). Integrity failures throw: never fall back
    # silently from a checksum mismatch.
    param([string] $PackageVersion, [switch] $Configure)
    Assert-PhosonWindows
    $asset = 'phoson-cli-windows-x86_64.zip'
    $root = $env:PHOSON_INSTALL_DIR
    if (-not $root) { $root = Join-Path $env:LOCALAPPDATA 'Phoson' }
    $base = Get-PhosonReleaseBase $PackageVersion
    $work = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $work -ErrorAction Stop
    try {
        $zip = Join-Path $work $asset
        $sums = Join-Path $work 'SHA256SUMS'
        try {
            Get-PhosonAsset $base $asset $zip
            Get-PhosonAsset $base 'SHA256SUMS' $sums
        }
        catch {
            # A download failure is a network/GitHub problem, not a missing
            # binary: never fall back to PyPI over it.
            throw "Could not download $asset from ${base} (network or GitHub error): $($_.Exception.Message). Try again in a minute, or re-run with -Method uv to install from PyPI instead."
        }
        $expected = $null
        foreach ($line in Get-Content -LiteralPath $sums) {
            $parts = -split $line
            if ($parts.Count -eq 2 -and $parts[1].TrimStart('*') -eq $asset) {
                $expected = $parts[0].ToLowerInvariant()
            }
        }
        if (-not $expected) { throw "$asset is not listed in SHA256SUMS; refusing to install." }
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            throw "Checksum mismatch for $asset (expected $expected, got $actual)."
        }
        Write-Host 'Checksum verified'

        $versions = Join-Path $root 'versions'
        $null = New-Item -ItemType Directory -Force -Path $versions
        $staging = Join-Path $versions ('.staging-' + [Guid]::NewGuid().ToString('N'))
        Expand-Archive -LiteralPath $zip -DestinationPath $staging -Force
        $stagedExe = Join-Path $staging 'phoson-cli\phoson-cli.exe'
        if (-not (Test-Path -LiteralPath $stagedExe -PathType Leaf)) {
            Remove-Item -LiteralPath $staging -Recurse -Force
            throw 'The archive does not contain a phoson-cli bundle.'
        }
        # Smoke test before switching: a bundle that cannot start here must not
        # replace a working install.
        try { $reported = (Invoke-PhosonNative $stagedExe @('--version') | Out-String).Trim() }
        catch {
            Remove-Item -LiteralPath $staging -Recurse -Force
            Write-Warning 'The prebuilt binary does not run on this system.'
            return $false
        }
        if ($reported -notmatch '^phoson-cli (\d+\.\d+\.\d+)') {
            Remove-Item -LiteralPath $staging -Recurse -Force
            throw "Unexpected CLI version output: $reported"
        }
        $installed = $Matches[1]
        if ($PackageVersion -and $installed -ne $PackageVersion) {
            Remove-Item -LiteralPath $staging -Recurse -Force
            throw "Expected phoson-cli $PackageVersion, received: $reported"
        }

        $final = Join-Path $versions $installed
        if (Test-Path -LiteralPath $final) { Remove-Item -LiteralPath $final -Recurse -Force }
        Move-Item -LiteralPath $staging -Destination $final
        $current = Join-Path $root 'current'
        # Directory.Delete on a junction removes the link, not its target.
        if (Test-Path -LiteralPath $current) { [IO.Directory]::Delete($current) }
        $null = New-Item -ItemType Junction -Path $current -Target $final
        $pathEntry = Join-Path $current 'phoson-cli'
        Add-PhosonUserPath $pathEntry

        $receipt = [ordered]@{
            method     = 'binary'
            version    = $installed
            platform   = 'windows-x86_64'
            path_entry = $pathEntry
        } | ConvertTo-Json
        # No BOM: PowerShell 5.1's -Encoding UTF8 would add one.
        [IO.File]::WriteAllText((Join-Path $root 'install-receipt.json'), $receipt, (New-Object Text.UTF8Encoding $false))

        # Keep the new version and the one it replaced.
        Get-ChildItem -LiteralPath $versions -Directory |
            Where-Object { $_.Name -notlike '.staging-*' -and $_.FullName -ne $final } |
            Sort-Object LastWriteTime -Descending | Select-Object -Skip 1 |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

        $uv = Find-PhosonUv
        if ($uv) {
            $tools = @(Invoke-PhosonNative $uv @('tool', 'list')) -join "`n"
            if ($tools -match '(?m)^phoson-engine-minimal ') {
                Write-Warning 'Removing the previous uv-tool installation (settings are kept).'
                $null = Invoke-PhosonNative $uv @('tool', 'uninstall', 'phoson-engine-minimal')
            }
        }

        $cli = Join-Path $final 'phoson-cli\phoson-cli.exe'
        Write-Host "phoson-cli $installed installed (standalone binary) at $root"
        Write-Host 'Open a new terminal, then run: phoson-cli'
        Write-Host 'Configure later: phoson-cli --setup'
        Write-Host 'Update: phoson-cli --self-update'
        Write-Host 'Uninstall: phoson-cli --uninstall (settings are retained)'
        if ($Configure) { Invoke-PhosonNative $cli @('--setup') | Out-Host }
        return $true
    }
    finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-PhosonInstall {
    param([string] $Method, [switch] $MethodExplicit, [string] $PackageVersion, [switch] $Configure)
    if ($Method -eq 'uv') {
        Install-PhosonCli -PackageVersion $PackageVersion -Configure:$Configure
        return
    }
    # Only the last pipeline value is the function's verdict.
    $ok = @(Install-PhosonBinary -PackageVersion $PackageVersion -Configure:$Configure)[-1]
    if ($ok -eq $true) { return }
    if ($MethodExplicit) {
        throw 'Binary installation failed; re-run with -Method uv to install from PyPI.'
    }
    Write-Warning 'Falling back to the PyPI package (uv).'
    Install-PhosonCli -PackageVersion $PackageVersion -Configure:$Configure
}

# Dot-sourcing defines helpers for tests without installing anything.
if ($MyInvocation.InvocationName -ne '.') {
    if ($Help) {
        Write-Output 'Usage: .\phoson-installer.ps1 [-Version X.Y.Z] [-Method binary|uv] [-Setup] [-CI] [-SkipSetup] [-Help]'
        Write-Output 'Installs for the current Windows user: the standalone binary by default, or via uv and PyPI. Setup is opt-in.'
        Write-Output '-CI and -SkipSetup suppress setup even when -Setup is specified.'
    }
    else {
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Stop'
        try {
            Invoke-PhosonInstall -Method $Method -MethodExplicit:($PSBoundParameters.ContainsKey('Method')) `
                -PackageVersion $Version -Configure:($Setup -and -not $CI -and -not $SkipSetup)
        }
        finally { $ErrorActionPreference = $previousPreference }
    }
}
