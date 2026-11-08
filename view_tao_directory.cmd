@echo off
setlocal
call "%~dp0run_local.cmd" directory %*
exit /b %errorlevel%
