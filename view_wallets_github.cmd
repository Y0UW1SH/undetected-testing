@echo off
setlocal
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
set "RUN=%~1"
if "%RUN%"=="" set "RUN=latest"
"%PY%" -m seo.tao_view --github "%RUN%" || exit /b 1
start "" "%CD%\github_out\wallets.html"
