@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\stop_wallet_watcher.ps1"
exit /b %errorlevel%
