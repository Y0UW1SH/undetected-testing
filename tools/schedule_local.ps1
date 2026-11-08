<#
Explicit opt-in Windows Task Scheduler management. No elevation or credentials.
Tasks run ONLY while this user is logged on (InteractiveToken), because Chrome
needs an interactive desktop. Keep the repository and private root in place.
Examples from PowerShell in the repository:
  .\tools\schedule_local.ps1 status
  .\tools\schedule_local.ps1 install -Root 'D:\Private Data\Tao'
  .\tools\schedule_local.ps1 install -Jobs collect,subnets,watch -Replace
  .\tools\schedule_local.ps1 remove
No execution policy is changed by this script.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('install', 'remove', 'status')]
    [string]$Action,
    [string]$Root,
    [ValidateSet('collect', 'subnets', 'census', 'seo', 'research', 'watch')]
    [string[]]$Jobs = @('collect', 'subnets'),
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')]
    [string]$DailyAt = '09:00',
    [switch]$Replace
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
if (-not $Root) { $Root = $env:TAO_LOCAL_HOME }
if (-not $Root) {
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable; specify -Root.' }
    $Root = Join-Path $env:LOCALAPPDATA 'StealthWalletMonitor\LocalRunner'
}
$Root = [IO.Path]::GetFullPath($Root)
$Python = Join-Path $Repo '.venv-local\Scripts\python.exe'
$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
# Stable instance identity isolates different checkouts, roots, and users.
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $bytes = [Text.Encoding]::UTF8.GetBytes("$Repo|$Root|$User".ToLowerInvariant())
    $Instance = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').Substring(0, 16)
} finally { $sha.Dispose() }
$Prefix = "StealthWalletMonitor-Local-$Instance"
$AllJobs = @('collect', 'subnets', 'census', 'seo', 'research', 'watch')
if ($Action -ne 'install' -and -not $PSBoundParameters.ContainsKey('Jobs')) {
    $Jobs = $AllJobs
}
$Jobs = @($Jobs | Select-Object -Unique)

function Quote-Argument([string]$Value) {
    # Windows paths cannot contain double quotes. Double trailing backslashes
    # so CommandLineToArgvW does not consume the closing quote.
    if ($Value.Contains('"')) { throw 'A path cannot contain a double quote.' }
    return '"' + ($Value -replace '(\\+)$', '$1$1') + '"'
}

if ($Action -eq 'status') {
    foreach ($job in $Jobs) {
        $name = "$Prefix-$job"
        $task = Get-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue
        if ($task) {
            $info = Get-ScheduledTaskInfo -InputObject $task
            [pscustomobject]@{ Name = $name; State = $task.State; Root = $Root;
                LastRun = $info.LastRunTime; LastResult = $info.LastTaskResult;
                NextRun = $info.NextRunTime }
        } else { [pscustomobject]@{ Name = $name; State = 'Not installed'; Root = $Root } }
    }
    return
}
if ($Action -eq 'remove') {
    foreach ($job in $Jobs) {
        $name = "$Prefix-$job"
        $task = Get-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue
        if ($task) {
            Unregister-ScheduledTask -TaskPath '\' -TaskName $name -Confirm:$false
            Write-Output "Removed $name"
        }
    }
    return
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Run setup_local.cmd before installing scheduled tasks.'
}
if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw 'Initialize the private root with run_local.cmd --root PATH init first.'
}
# Preflight every selected name before any task registration.
foreach ($job in $Jobs) {
    $existing = Get-ScheduledTask -TaskPath '\' -TaskName "$Prefix-$job" -ErrorAction SilentlyContinue
    if ($existing -and -not $Replace) {
        throw "Refusing to overwrite $Prefix-$job. Use -Replace explicitly."
    }
}
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 24)
foreach ($job in $Jobs) {
    $arguments = '-m seo.local_runner --wait 21600 --root ' + (Quote-Argument $Root) + ' ' + $job
    if ($job -eq 'watch') { $arguments += ' --once' }
    $taskAction = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $Repo
    if ($job -eq 'subnets') {
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
            -RepetitionInterval (New-TimeSpan -Hours 4)
    } elseif ($job -eq 'watch') {
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
            -RepetitionInterval (New-TimeSpan -Minutes 5)
    } else {
        $trigger = New-ScheduledTaskTrigger -Daily -At ([DateTime]::Today.Add([TimeSpan]::Parse($DailyAt)))
    }
    $task = New-ScheduledTask -Action $taskAction -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "Local runner $job; root $Root; logged-on user only."
    Register-ScheduledTask -TaskPath '\' -TaskName "$Prefix-$job" -InputObject $task -Force:$Replace | Out-Null
    Write-Output "Installed $Prefix-$job (logged-on user only), root: $Root"
}
