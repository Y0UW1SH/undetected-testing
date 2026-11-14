@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
py -3.13 -c "import sys; assert sys.version_info[:2] == (3, 13)" >nul 2>&1
if errorlevel 1 (
    echo Python 3.13 and the Python launcher are required. Install them from the official Python website first.
    popd
    exit /b 1
)
if not exist "%~dp0.venv-local\Scripts\python.exe" (
    py -3.13 -m venv "%~dp0.venv-local"
    if errorlevel 1 goto :failed
)
"%~dp0.venv-local\Scripts\python.exe" -c "import sys; assert sys.version_info[:2] == (3, 13), 'Existing .venv-local must use Python 3.13'"
if errorlevel 1 goto :failed
"%~dp0.venv-local\Scripts\python.exe" -m pip install -e . -r requirements-local.txt
if errorlevel 1 goto :failed
"%~dp0.venv-local\Scripts\python.exe" -m seo.local_runner init
if errorlevel 1 goto :failed
echo Setup complete. Run run_local.cmd check before collecting data.
popd
exit /b 0
:failed
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
