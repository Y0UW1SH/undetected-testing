@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if /i "%~1"=="--setup" goto :setup
call "%~dp0run_local.cmd" watch %*
exit /b %errorlevel%
:setup
call "%~dp0setup_local.cmd"
exit /b %errorlevel%
