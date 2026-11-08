@echo off
rem Compatibility filename only: collect subnets on this PC, without GitHub.
rem Legacy arguments: run_tao_github.cmd [netuids] [threshold]
setlocal
cd /d "%~dp0"
echo This compatibility command now collects subnets locally.
set "THRESH=%~2"
if not defined THRESH set "THRESH=10"
if "%~1"=="" goto :all
call "%~dp0run_local.cmd" subnets --netuids "%~1" --threshold "%THRESH%"
exit /b %errorlevel%
:all
call "%~dp0run_local.cmd" subnets --threshold "%THRESH%"
exit /b %errorlevel%
