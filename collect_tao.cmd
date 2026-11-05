@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run try_tao.cmd from this folder to set up the browser.
  exit /b 1
)
set "PYTHONIOENCODING=utf-8"
set "TAO_DB=try_tao_out\wallets.sqlite"
if not exist "try_tao_out" mkdir "try_tao_out"
".venv\Scripts\python.exe" -m seo.tao_cli collect --out "try_tao_out\wallets.json" %*
if errorlevel 1 exit /b %errorlevel%
".venv\Scripts\python.exe" -m seo.tao_cli performance
