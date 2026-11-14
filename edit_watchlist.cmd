@echo off
setlocal DisableDelayedExpansion
call "%~dp0run_local.cmd" edit %*
exit /b %errorlevel%
