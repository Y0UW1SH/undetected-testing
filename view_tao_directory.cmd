@echo off
setlocal DisableDelayedExpansion
call "%~dp0run_local.cmd" directory %*
exit /b %errorlevel%
