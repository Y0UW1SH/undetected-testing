@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "WATCH_DIR=try_tao_out\watch-env-313"
set "WATCH_PY=%WATCH_DIR%\Scripts\python.exe"
if defined CI goto :local_only
if defined GITHUB_ACTIONS goto :local_only
if /i "%~1"=="--setup" goto :setup
if /i "%~1"=="--help" goto :help
if not exist "%WATCH_PY%" goto :need_setup
"%WATCH_PY%" -c "import substrateinterface" >nul 2>nul
if errorlevel 1 goto :need_setup
"%WATCH_PY%" -m seo.tao_local_watch --cached-report --prompt %*
exit /b %errorlevel%

:setup
rem Python 3.13 has Windows wheels for the RPC client's crypto dependencies.
rem Keep the bootstrap, interpreter and caches local to this workspace.
set "DWELLIR_API_KEY="
set "DWELLIR_WS_URL="
set "UV_CACHE_DIR=%CD%\try_tao_out\watch-cache"
set "UV_PYTHON_INSTALL_DIR=%CD%\try_tao_out\watch-python"
set "UV_PYTHON_INSTALL_BIN=0"
set "UV_PYTHON_INSTALL_REGISTRY=0"
set "UV_NO_MODIFY_PATH=1"
set "UV_SYSTEM_CERTS=true"
set "UV_NO_CONFIG=1"
set "TOOLS_PY=try_tao_out\watch-tools\Scripts\python.exe"
set "UV=try_tao_out\watch-tools\Scripts\uv.exe"
if exist "%TOOLS_PY%" goto :bootstrap
if exist ".venv\Scripts\python.exe" goto :project_python
py -3 -m venv "try_tao_out\watch-tools"
if errorlevel 1 exit /b 1
goto :bootstrap
:project_python
".venv\Scripts\python.exe" -m venv "try_tao_out\watch-tools"
if errorlevel 1 exit /b 1
:bootstrap
if exist "%UV%" goto :runtime
"%TOOLS_PY%" -m pip --disable-pip-version-check install --only-binary=:all: "uv==0.13.7"
if errorlevel 1 exit /b 1
:runtime
if exist "%WATCH_PY%" goto :install
"%UV%" venv --python 3.13 "%WATCH_DIR%"
if errorlevel 1 exit /b 1
:install
"%UV%" pip install --python "%WATCH_PY%" --only-binary=:all: "substrate-interface==1.8.1"
if errorlevel 1 exit /b 1
"%WATCH_PY%" -c "from substrateinterface import SubstrateInterface"
if errorlevel 1 exit /b 1
echo Local watcher dependency installed. To start: watch_wallets.cmd
exit /b 0

:help
echo watch_wallets.cmd --setup                    Prepare an isolated local Python 3.13 watcher.
echo watch_wallets.cmd                            Watch eligible followed wallets in the cached report.
echo watch_wallets.cmd --address ADDRESS          Also watch a specific coldkey.
echo watch_wallets.cmd --address ADDRESS --once   Run one local poll.
echo The key prompt hides input. Ctrl+C stops the foreground watcher.
exit /b 0
:need_setup
echo Run watch_wallets.cmd --setup to install this optional local dependency.
exit /b 2
:local_only
echo The Dwellir watcher runs only on your local PC.
exit /b 2
