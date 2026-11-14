<#
Offline Windows readiness check, run after setup_local.cmd:
  .\tools\test_windows_local.ps1

Checks the installed environment and offline tests, then exercises the CMD
launcher with disposable private state. No browser, live collection, credentials,
Task Scheduler registration, or execution-policy changes are performed.
The syntax-only check also works with PowerShell on Linux/macOS:
  pwsh -NoProfile -File tools/test_windows_local.ps1 -SyntaxOnly
#>
[CmdletBinding()]
param([switch]$SyntaxOnly)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Parse, never invoke, the scheduling script. Parsing catches errors that source
# text assertions cannot detect and has no Task Scheduler side effects.
foreach ($name in @('schedule_local.ps1', 'test_windows_local.ps1')) {
    $path = Join-Path $PSScriptRoot $name
    $tokens = $null
    $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile(
        $path, [ref]$tokens, [ref]$errors)
    if ($errors.Count) {
        throw "PowerShell syntax check failed for ${path}: $($errors -join '; ')"
    }
}
Write-Output 'PowerShell syntax checks passed. No scheduled tasks were changed.'
if ($SyntaxOnly) { return }
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'The native smoke test requires Windows. Use -SyntaxOnly on other operating systems.'
}

$Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Python = Join-Path $Repo '.venv-local\Scripts\python.exe'
$Launcher = Join-Path $Repo 'run_local.cmd'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Run setup_local.cmd first to create the local Python environment.'
}

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Offline check failed with exit code ${LASTEXITCODE}: $Program"
    }
}

Write-Output 'Checking Python 3.13 x64 and installed dependencies.'
# Avoid embedded double quotes: Windows PowerShell 5.1 uses legacy native
# argument passing, which can remove them before Python receives the command.
Invoke-Checked $Python @('-c',
    'import sys; sys.exit(int(sys.version_info[:2] != (3, 13) or sys.maxsize <= 2**32))')
Invoke-Checked $Python @('-m', 'pip', 'check')

$Scratch = Join-Path ([IO.Path]::GetTempPath()) (
    'Tao local smoke ! (test) ' + [Guid]::NewGuid().ToString('N'))
$PrivateRoot = Join-Path $Scratch 'private state'
$RestoredRoot = Join-Path $Scratch 'restored state'
$OriginalHome = [Environment]::GetEnvironmentVariable('TAO_LOCAL_HOME', 'Process')
$null = New-Item -ItemType Directory -Path $Scratch
Push-Location -LiteralPath $Repo
try {
    # Even code that uses the default root is isolated from the user's data.
    $env:TAO_LOCAL_HOME = $PrivateRoot
    Invoke-Checked $Python @('-m', 'pytest', 'tests/', '-q')
    Set-Location -LiteralPath $Scratch
    Invoke-Checked $Launcher @('--root', $PrivateRoot, 'init')
    Invoke-Checked $Launcher @('--root', $PrivateRoot, 'status')
    Invoke-Checked $Launcher @('--root', $PrivateRoot, 'backup')
    $Backups = @(Get-ChildItem -LiteralPath (Join-Path $PrivateRoot 'backups') -Directory)
    if ($Backups.Count -ne 1) { throw 'Expected one disposable backup generation.' }
    Invoke-Checked $Launcher @('--root', $PrivateRoot, 'restore',
        $Backups[0].FullName, '--to', $RestoredRoot)
    foreach ($name in @('local.json', 'keywords.json', 'research.json', 'tao.json')) {
        $relative = Join-Path 'config' $name
        $source = Get-FileHash -LiteralPath (Join-Path $PrivateRoot $relative) -Algorithm SHA256
        $restored = Get-FileHash -LiteralPath (Join-Path $RestoredRoot $relative) -Algorithm SHA256
        if ($source.Hash -ne $restored.Hash) { throw "Restore verification failed for $name" }
    }
    Write-Output 'Offline Windows smoke checks passed. Browser, live sources and Task Scheduler execution remain untested.'
} finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable('TAO_LOCAL_HOME', $OriginalHome, 'Process')
    # Only remove the unique scratch directory created above, never a user root.
    Remove-Item -LiteralPath $Scratch -Recurse -Force
}
