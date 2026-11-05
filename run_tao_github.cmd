@echo off
rem Run the taostats scraper on GitHub Actions (not on this PC), stream the
rem run here, print the scraped table, and download the results.
rem Usage:  run_tao_github.cmd               (all subnets, 10%% threshold)
rem         run_tao_github.cmd 1,8,19 5      (only these netuids, 5%% threshold)
setlocal
cd /d "%~dp0"
set "REPO=Y0UW1SH/undetected-testing"
set "NETUIDS=%~1"
set "THRESH=%~2"
if "%THRESH%"=="" set "THRESH=10"

where gh >nul 2>&1 || (echo Needs the GitHub CLI: winget install GitHub.cli, then gh auth login & exit /b 1)

echo Starting the tao workflow on GitHub ...
set "RUN="
for /f "delims=" %%L in ('gh workflow run tao.yml -R %REPO% --ref master -f threshold^=%THRESH% -f netuids^=%NETUIDS% 2^>^&1') do (
  echo %%L
  for %%U in (%%L) do for /f "tokens=7 delims=/" %%I in ("%%U") do set "RUN=%%I"
)
if "%RUN%"=="" (echo Could not start the run. & exit /b 1)
echo Run: https://github.com/%REPO%/actions/runs/%RUN%
echo Waiting for GitHub to finish (about 2 minutes) ...

gh run watch %RUN% -R %REPO% --exit-status --compact -i 10
set "RC=%errorlevel%"

echo.
echo ===== Scraper output from the GitHub run =====
powershell -NoProfile -Command "$on=$false; gh run view %RUN% -R %REPO% --log | ForEach-Object { $f=$_ -split \"`t\",3; if ($f[1] -like 'Fetch subnets*') { $t=$f[2].Substring(29); if ($t -match '^ *netuid +name') {$on=$true}; if ($on) {$t} } }"

echo.
echo Downloading results to github_out\%RUN%\ ...
gh run download %RUN% -R %REPO% -D "github_out\%RUN%" || exit /b 1
echo Done. CSV: %CD%\github_out\%RUN%\tao-results\data\subnet_stats.csv
echo      Report: %CD%\github_out\%RUN%\tao-results\data\tao_report.html
exit /b %RC%
