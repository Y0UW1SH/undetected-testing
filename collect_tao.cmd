@echo off
setlocal DisableDelayedExpansion
call "%~dp0run_local.cmd" collect %*
exit /b %errorlevel%
