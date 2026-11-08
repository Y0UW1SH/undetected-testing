@echo off
setlocal
call "%~dp0run_local.cmd" collect %*
exit /b %errorlevel%
