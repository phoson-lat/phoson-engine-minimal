# Binary-mode contract tests for scripts/phoson-installer.ps1 against a local
# fake release (PHOSON_RELEASE_BASE pointing at a directory). Windows only:
# exercises the real zip extraction, checksum, junction and receipt code.
$ErrorActionPreference = 'Stop'
$installer = Join-Path $PSScriptRoot '../../scripts/phoson-installer.ps1'
. $installer

function Assert-Equal($Actual, $Expected, [string] $Label) {
    if ($Actual -cne $Expected) { throw "${Label}: expected '$Expected', got '$Actual'" }
}
function Assert-Fails([scriptblock] $Action, [string] $Pattern) {
    try { & $Action } catch {
        if ($_.Exception.Message -notmatch $Pattern) { throw }
        return
    }
    throw "Expected failure matching $Pattern"
}

$temp = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $temp
$asset = 'phoson-cli-windows-x86_64.zip'

function Publish-Release([string] $Directory) {
    $bundle = Join-Path $temp 'bundle'
    Remove-Item -LiteralPath $bundle -Recurse -Force -ErrorAction SilentlyContinue
    $null = New-Item -ItemType Directory -Path (Join-Path $bundle 'phoson-cli')
    Set-Content -LiteralPath (Join-Path $bundle 'phoson-cli\phoson-cli.exe') -Value 'fake'
    $null = New-Item -ItemType Directory -Force -Path $Directory
    $zip = Join-Path $Directory $asset
    Remove-Item -LiteralPath $zip -ErrorAction SilentlyContinue
    Compress-Archive -Path (Join-Path $bundle 'phoson-cli') -DestinationPath $zip
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
    Set-Content -LiteralPath (Join-Path $Directory 'SHA256SUMS') -Value "$hash  $asset"
}

try {
    $release = Join-Path $temp 'release'
    $root = Join-Path $temp 'install root'
    $env:PHOSON_RELEASE_BASE = $release
    $env:PHOSON_INSTALL_DIR = $root
    $script:paths = @()
    $script:reported = 'phoson-cli 1.2.3'
    $script:uvFallback = $false
    function Assert-PhosonWindows { }
    function Find-PhosonUv { return $null }
    function Add-PhosonUserPath([string] $Directory) { $script:paths += $Directory }
    function Install-PhosonCli { $script:uvFallback = $true }
    function Invoke-PhosonNative([string] $Executable, [string[]] $Arguments) {
        if ($Arguments[0] -eq '--version') {
            if ($script:reported -eq 'fail') { throw 'cannot start' }
            return $script:reported
        }
    }

    Publish-Release (Join-Path $release 'latest\download')
    Invoke-PhosonInstall -Method 'binary' | Out-Null
    $receiptPath = Join-Path $root 'install-receipt.json'
    $bytes = [IO.File]::ReadAllBytes($receiptPath)
    Assert-Equal ($bytes[0] -eq 0xEF) $false 'receipt has no BOM'
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    Assert-Equal $receipt.method 'binary' 'receipt method'
    Assert-Equal $receipt.version '1.2.3' 'receipt version'
    $current = Get-Item -LiteralPath (Join-Path $root 'current')
    Assert-Equal ([string] $current.LinkType) 'Junction' 'current is a junction'
    Assert-Equal $script:paths[0] (Join-Path (Join-Path $root 'current') 'phoson-cli') 'PATH entry'
    Assert-Equal $receipt.path_entry $script:paths[0] 'receipt PATH entry'

    # A pinned version switches the junction and keeps the previous version.
    Publish-Release (Join-Path $release 'download\v1.2.4')
    $script:reported = 'phoson-cli 1.2.4'
    Invoke-PhosonInstall -Method 'binary' -PackageVersion '1.2.4' | Out-Null
    $target = @((Get-Item -LiteralPath (Join-Path $root 'current')).Target)[0]
    Assert-Equal (Split-Path -Leaf $target) '1.2.4' 'junction switched'
    $kept = @(Get-ChildItem -LiteralPath (Join-Path $root 'versions') -Directory | ForEach-Object Name | Sort-Object)
    Assert-Equal ($kept -join ',') '1.2.3,1.2.4' 'previous version kept'

    # A tampered archive aborts; it never falls back to uv.
    Add-Content -LiteralPath (Join-Path $release "latest\download\$asset") -Value 'x'
    $script:reported = 'phoson-cli 1.2.3'
    Assert-Fails { Invoke-PhosonInstall -Method 'binary' | Out-Null } 'Checksum mismatch'
    Assert-Equal $script:uvFallback $false 'no fallback after checksum failure'

    # A download failure (network/GitHub error) aborts; it never falls back to
    # uv, even with the implicit method.
    $env:PHOSON_RELEASE_BASE = Join-Path $temp 'missing-release'
    Assert-Fails { Invoke-PhosonInstall -Method 'binary' | Out-Null } 'network or GitHub error'
    Assert-Equal $script:uvFallback $false 'no fallback after a download failure'
    $env:PHOSON_RELEASE_BASE = $release

    # No usable binary: implicit method falls back, explicit method fails.
    Publish-Release (Join-Path $release 'latest\download')
    $script:reported = 'fail'
    Invoke-PhosonInstall -Method 'binary' | Out-Null
    Assert-Equal $script:uvFallback $true 'falls back to uv'
    $script:uvFallback = $false
    Assert-Fails { Invoke-PhosonInstall -Method 'binary' -MethodExplicit | Out-Null } 'Binary installation failed'
    Assert-Equal $script:uvFallback $false 'explicit method does not fall back'
}
finally {
    $env:PHOSON_RELEASE_BASE = $null
    $env:PHOSON_INSTALL_DIR = $null
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Output 'PASS: Windows binary installer contract tests'
