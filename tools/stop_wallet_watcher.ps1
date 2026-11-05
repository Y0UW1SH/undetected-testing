# Stops only this checkout's hidden wallet monitor. Foreground monitors use Ctrl+C.
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$stateRoot = Join-Path $projectRoot 'try_tao_out'
$pidFile = Join-Path $stateRoot 'watch_monitor.pid'
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output 'No background watcher recorded for this folder. Use Ctrl+C for a foreground watcher.'
    exit 0
}
$watchId = 0
if (-not [int]::TryParse((Get-Content -Raw -LiteralPath $pidFile).Trim(), [ref]$watchId) -or $watchId -le 0) {
    throw 'The local watcher process ID is invalid.'
}
$watch = Get-CimInstance Win32_Process -Filter "ProcessId=$watchId"
if ($watch) {
    $runtime = Join-Path $stateRoot 'watch-env-313\Scripts\python.exe'
    if ($watch.ExecutablePath -ne $runtime -or $watch.CommandLine -notmatch '(?i)(?:^|\s)-m\s+seo\.tao_local_watch(?:\s|$)') {
        throw 'The recorded ID is no longer this wallet watcher; no process was stopped.'
    }
    # A Windows venv may create a separate Python interpreter child.
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$watchId" |
        Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '(?i)(?:^|\s)-m\s+seo\.tao_local_watch(?:\s|$)' })
    foreach ($child in $children) { Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
    Stop-Process -Id $watchId -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $pidFile
Write-Output 'Background watcher stopped. Its last saved checkpoint is kept for the next run.'
