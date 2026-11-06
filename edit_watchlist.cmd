@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m seo.tao_watchlist --open
) else (
  python -m seo.tao_watchlist --open
)
exit /b %errorlevel%
