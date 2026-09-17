# Dependency-free installer contract tests. Runs with PowerShell 5.1 and 7.
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

Assert-Equal (Merge-PhosonPath 'C:\Existing' 'C:\User Bin') 'C:\Existing;C:\User Bin' 'preserves PATH'
Assert-Equal (Merge-PhosonPath 'C:\USER BIN\;C:\Existing' 'c:\user bin') 'C:\USER BIN\;C:\Existing' 'case-insensitive deduplication'
Assert-Equal (Merge-PhosonPath '' 'C:\User Bin') 'C:\User Bin' 'empty PATH'
Assert-Equal (Merge-PhosonPath '"C:\User Bin";C:\Existing' 'C:\User Bin') '"C:\User Bin";C:\Existing' 'quoted entry'

# Native failures must not be mistaken for success (including PS 5.1).
$shellName = 'pwsh'
if ($PSVersionTable.PSEdition -eq 'Desktop') { $shellName = 'powershell.exe' }
elseif ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) { $shellName = 'pwsh.exe' }
$shell = Join-Path $PSHOME $shellName
Assert-Fails { Invoke-PhosonNative $shell @('-NoProfile', '-Command', 'exit 7') } 'exit 7'
Assert-Equal (Invoke-PhosonNative $shell @('-NoProfile', '-Command', "Write-Output 'native-ok'")) 'native-ok' 'native output'
Assert-Fails { & $installer -Version 'bad;value' -Help } 'pattern'

$temp = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $temp
try {
    $script:testBin = Join-Path $temp 'tool bin with spaces'
    $null = New-Item -ItemType Directory -Path $script:testBin
    $null = New-Item -ItemType File -Path (Join-Path $script:testBin 'phoson-cli.exe')
    $script:testUv = Join-Path $temp 'uv.exe'
    $script:calls = @()
    $script:paths = @()
    $script:missingUv = $false
    $script:installedUv = $false
    $script:failInstall = $false
    $script:reportedVersion = 'phoson-cli 0.40.0'
    function Assert-PhosonWindows { }
    function Find-PhosonUv {
        if ($script:missingUv -and -not $script:installedUv) { return $null }
        return $script:testUv
    }
    function Install-PhosonUv { $script:installedUv = $true }
    function Add-PhosonUserPath([string] $Directory) { $script:paths += $Directory }
    function Invoke-PhosonNative([string] $Executable, [string[]] $Arguments) {
        $script:calls += ,@($Executable, ($Arguments -join '|'))
        if ($Arguments[0] -eq 'tool' -and $Arguments[1] -eq 'dir') { return $script:testBin }
        if ($Arguments[0] -eq 'tool' -and $Arguments[1] -eq 'install' -and $script:failInstall) {
            throw 'simulated install failure'
        }
        if ($Arguments[0] -eq '--version') {
            if ($Executable -eq $script:testUv) { return 'uv test' }
            return $script:reportedVersion
        }
    }

    Install-PhosonCli -PackageVersion '0.40.0' | Out-Null
    Assert-Equal $script:calls[1][1] 'tool|install|--python|3.12|--upgrade|phoson-engine-minimal==0.40.0' 'pinned install'
    Assert-Equal $script:calls[3][0] (Join-Path $script:testBin 'phoson-cli.exe') 'verify exact installed exe'
    Assert-Equal $script:calls.Count 4 'no setup by default'
    Assert-Equal $script:paths[1] $script:testBin 'uses uv actual bin directory'

    $script:calls = @()
    $script:missingUv = $true
    Install-PhosonCli -Configure | Out-Null
    Assert-Equal $script:installedUv $true 'bootstrap when missing'
    Assert-Equal $script:calls[1][1] 'tool|install|--python|3.12|--upgrade|phoson-engine-minimal' 'latest stable'
    Assert-Equal $script:calls[-1][1] '--setup' 'explicit setup'

    $script:calls = @()
    $script:paths = @()
    $script:failInstall = $true
    Assert-Fails { Install-PhosonCli -Configure | Out-Null } 'simulated install failure'
    Assert-Equal $script:calls.Count 2 'stop on failed uv install'
    Assert-Equal $script:paths.Count 0 'no PATH changes on install failure'
    $script:failInstall = $false
    $script:reportedVersion = 'phoson-cli 0.39.0'
    Assert-Fails { Install-PhosonCli -PackageVersion '0.40.0' | Out-Null } 'Expected phoson-cli 0.40.0'
}
finally { Remove-Item -LiteralPath $temp -Recurse -Force }
Write-Output 'PASS: Windows installer contract tests'
