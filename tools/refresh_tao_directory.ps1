param([switch]$NoOpen, [switch]$Offline, [switch]$Follow,
      [ValidateRange(1, 144)][int]$MaxChecks = 144)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$cache = Join-Path $projectRoot 'github_out'
$checkout = Join-Path $cache 'taostats-directory-checkpoint'
$db = Join-Path $cache 'taostats-directory.sqlite'
$view = Join-Path $cache 'accounts.html'
$repo = 'https://github.com/Y0UW1SH/undetected-testing.git'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) { $python = 'python' }
Push-Location -LiteralPath $projectRoot
try {
    if ($Follow) {
        if ($Offline) { throw 'Follow needs access to the public checkpoint.' }
        # This only downloads changes. It never dispatches Actions runs.
        # Stop after completion, 90 minutes without a checkpoint, or 12 hours.
        $lastChange = [datetime]::UtcNow
        $previous = ''
        for ($i=0; $i -lt $MaxChecks; $i++) {
            $remote = git ls-remote --exit-code $repo refs/heads/tao-account-registry
            if ($LASTEXITCODE -eq 0 -and $remote -match '^([0-9a-f]{40})\s') {
                $revision = $Matches[1]
                if ($revision -ne $previous -or !(Test-Path -LiteralPath $view)) {
                    & $PSCommandPath -NoOpen
                    $previous = $revision
                    $lastChange = [datetime]::UtcNow
                    $check = & $python -c 'import sys, sqlite3; from seo.sources.tao_census import _next_page; c=sqlite3.connect(sys.argv[1]); print(_next_page(c,*c.execute(sys.argv[2]).fetchone()))' $db 'SELECT page_size,latest_total FROM census_state'
                    if ($LASTEXITCODE -ne 0) { throw 'Could not verify the local page cursor.' }
                    Write-Output ('Next directory page: ' + $check)
                    if ($check -eq 'None') { return }
                }
            }
            if (([datetime]::UtcNow - $lastChange).TotalMinutes -ge 90) { return }
            if ($i+1 -lt $MaxChecks) { Start-Sleep -Seconds 300 }
        }
        return
    }
    if (!$Offline) {
        [IO.Directory]::CreateDirectory($cache) | Out-Null
        if (Test-Path -LiteralPath $checkout) {
            $origin = git -C $checkout remote get-url origin
            if ($LASTEXITCODE -ne 0 -or $origin -ne $repo) { throw 'Unexpected census checkout. Leave existing files intact.' }
            git -C $checkout fetch --depth=1 --no-tags origin refs/heads/tao-account-registry
            if ($LASTEXITCODE -ne 0) { throw 'Could not fetch the public checkpoint. Use -Offline to view the previous download.' }
            git -C $checkout checkout --detach FETCH_HEAD
            if ($LASTEXITCODE -ne 0) { throw 'Could not switch the local census checkpoint.' }
        } else {
            git clone --depth=1 --single-branch --branch tao-account-registry --no-tags $repo $checkout
            if ($LASTEXITCODE -ne 0) { throw 'Could not download the public checkpoint.' }
        }
        $stage = Join-Path $cache ('census-' + [guid]::NewGuid().ToString('N') + '.sqlite')
        try {
            & $python -m tools.tao_census_workflow restore --state $checkout --db $stage
            if ($LASTEXITCODE -ne 0) { throw 'Could not restore the checkpoint. The previous local database is unchanged.' }
            & $python -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' $stage $db
            if ($LASTEXITCODE -ne 0) { throw 'Could not install the downloaded directory locally.' }
        } finally {
            if ([IO.File]::Exists($stage)) { [IO.File]::Delete($stage) }
        }
    }
    if (!(Test-Path -LiteralPath $db)) { throw 'No local directory yet. Run once without -Offline.' }
    & $python -m seo.tao_directory_view --census $db --cache $cache --out $view
    if ($LASTEXITCODE -ne 0) { throw 'Could not render the account directory.' }
    if (!$NoOpen) { Start-Process -FilePath $view }
} finally {
    Pop-Location
}
