@echo off
rem Compatibility filename only: view this PC's local reports, without GitHub.
setlocal
cd /d "%~dp0"
echo This compatibility command now opens local wallet reports.
if /i "%~1"=="latest" goto :latest
call "%~dp0run_local.cmd" view %*
exit /b %errorlevel%
:latest
call "%~dp0run_local.cmd" view
exit /b %errorlevel%
