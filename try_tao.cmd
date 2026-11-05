@echo off
rem One-click test of the taostats.io scraper (tao CLI).
rem Usage:  try_tao.cmd            (visible Chrome window)
rem         try_tao.cmd --headless (no window)
setlocal
cd /d "%~dp0"
rem UTF-8 output so the tau symbol doesn't crash when output goes to a file.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Creating virtual env in .venv ...
  py -3 -m venv .venv || python -m venv .venv || goto :fail
)
set "PY=.venv\Scripts\python.exe"

echo [2/4] Installing this repo (pip install -e .) ...
"%PY%" -m pip install -q --upgrade pip wheel setuptools || goto :fail
"%PY%" -m pip install -q -e . || goto :fail

echo [3/4] Checking Chrome (seo doctor --browser) ...
".venv\Scripts\seo.exe" doctor --browser || goto :fail

rem Results go to try_tao_out\ so the repo's data\tao.sqlite is left alone.
if not exist try_tao_out mkdir try_tao_out
echo [4/4] Scraping https://taostats.io/subnets ...
".venv\Scripts\tao.exe" --db try_tao_out\tao.sqlite subnets --force %* || goto :fail
".venv\Scripts\tao.exe" --db try_tao_out\tao.sqlite export --dir try_tao_out --format csv

echo.
echo Done. CSV: %CD%\try_tao_out\subnet_stats.csv
exit /b 0

:fail
echo.
echo try_tao failed (exit code %errorlevel%).
exit /b 1
