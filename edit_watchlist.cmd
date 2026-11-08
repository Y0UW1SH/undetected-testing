@echo off
setlocal
call "%~dp0run_local.cmd" edit %*
exit /b %errorlevel%
