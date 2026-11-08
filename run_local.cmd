@echo off
setlocal DisableDelayedExpansion
if not exist "%~dp0.venv-local\Scripts\python.exe" (
    echo Run setup_local.cmd first to create the local Python environment.
    exit /b 1
)
pushd "%~dp0" || exit /b 1
"%~dp0.venv-local\Scripts\python.exe" -m seo.local_runner %*
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
