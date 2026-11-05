@echo off
setlocal
"%~dp0runtime\python.exe" -B "%~dp0wallet_monitor.py" %*
exit /b %errorlevel%
