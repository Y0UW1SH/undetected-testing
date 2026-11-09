"""Launcher contracts and offline checks; no Task Scheduler registration."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def powershell():
    executable = (os.environ.get('POWERSHELL_EXE')
                  or shutil.which('pwsh') or shutil.which('powershell'))
    if not executable:
        pytest.skip('PowerShell is unavailable; syntax was not verified')
    return executable


def run_powershell(executable, *args, env=None):
    return subprocess.run([executable, '-NoLogo', '-NoProfile', *args],
                          capture_output=True, text=True, timeout=60, env=env)


def test_smoke_syntax_mode_parses_without_running_scheduler(powershell):
    result = run_powershell(powershell, '-File',
                            str(ROOT / 'tools/test_windows_local.ps1'),
                            '-SyntaxOnly')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PowerShell syntax checks passed' in result.stdout


def test_smoke_syntax_mode_rejects_broken_scheduler(powershell, tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    script = tools / 'test_windows_local.ps1'
    shutil.copyfile(ROOT / 'tools/test_windows_local.ps1', script)
    (tools / 'schedule_local.ps1').write_text('param([string]$value\n')
    result = run_powershell(powershell, '-File', str(script), '-SyntaxOnly')
    assert result.returncode != 0
    assert 'schedule_local.ps1' in result.stdout + result.stderr


@pytest.mark.skipif(os.name == 'nt', reason='Non-Windows guard')
def test_smoke_refuses_native_checks_off_windows(powershell, tmp_path):
    root = tmp_path / 'must remain absent'
    env = dict(os.environ, TAO_LOCAL_HOME=str(root))
    result = run_powershell(powershell, '-File',
                            str(ROOT / 'tools/test_windows_local.ps1'),
                            env=env)
    assert result.returncode != 0
    assert 'requires Windows' in result.stdout + result.stderr
    assert not root.exists()


def test_scheduler_quotes_windows_paths_without_evaluating_them(powershell):
    # Extract only the real quoting function. Never execute the scheduler.
    path = str(ROOT / 'tools/schedule_local.ps1').replace("'", "''")
    command = r'''
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '%s', [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Scheduler syntax is invalid.' }
$fn = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] `
        -and $node.Name -eq 'Quote-Argument'
}, $true)
. ([scriptblock]::Create($fn.Extent.Text))
@((Quote-Argument 'D:\Private data ! (test)'),
  (Quote-Argument 'C:\'),
  (Quote-Argument '\\server\share\')) | ConvertTo-Json -Compress
''' % path
    result = run_powershell(powershell, '-Command', command)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == [
        '"D:\\Private data ! (test)"', '"C:\\\\"',
        '"\\\\server\\share\\\\"']


@pytest.mark.skipif(os.name != 'nt', reason='Requires native cmd.exe')
def test_native_launcher_preserves_root_and_nonzero_exit(tmp_path):
    python = ROOT / '.venv-local/Scripts/python.exe'
    if not python.is_file():
        pytest.skip('Run setup_local.cmd before testing the native launcher')
    root = tmp_path / 'Private data ! (test)'
    launcher = str(ROOT / 'run_local.cmd')

    def launch(*args):
        return subprocess.run([launcher, '--root', str(root), *args],
                              cwd=tmp_path, capture_output=True, text=True,
                              timeout=60)

    result = launch('init')
    assert result.returncode == 0, result.stdout + result.stderr
    config = root / 'config/local.json'
    assert json.loads(config.read_text())['collect']
    before = config.read_bytes()
    assert launch('status').returncode == 0
    assert launch('not-a-real-command').returncode == 2
    assert config.read_bytes() == before


def read(name):
    path = ROOT / name
    assert path.is_file(), f"Missing Windows entry point: {name}"
    return path.read_text(encoding="utf-8")


def test_bootstrap_uses_explicit_python_and_private_environment():
    script = read("setup_local.cmd")
    assert "py -3.13" in script
    assert "-m venv" in script
    assert '.venv-local\\Scripts\\python.exe' in script
    assert "requirements-local.txt" in script
    assert "ExecutionPolicy" not in script
    assert "http" not in script
    assert "substrate-interface==1.8.1" in read("requirements-local.txt")


def test_launcher_preserves_arguments_and_exit_code():
    script = read("run_local.cmd")
    assert 'pushd "%~dp0"' in script
    command = '"%~dp0.venv-local\\Scripts\\python.exe" -m seo.local_runner %*'
    assert command in script
    assert 'set "RESULT=%ERRORLEVEL%"' in script
    assert "exit /b %RESULT%" in script


def test_scheduler_is_explicit_logged_on_and_non_overlapping():
    script = read("tools/schedule_local.ps1")
    assert "[ValidateSet('install', 'remove', 'status')]" in script
    assert "[Parameter(Mandatory = $true, Position = 0)]" in script
    assert "-LogonType Interactive" in script
    assert "-RunLevel Limited" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-StartWhenAvailable" in script
    assert "-WorkingDirectory $Repo" in script
    assert "-Password" not in script
    assert "Set-ExecutionPolicy" not in script
    assert "-RunLevel Highest" not in script


def test_scheduler_scopes_names_and_requires_explicit_replacement():
    script = read("tools/schedule_local.ps1")
    assert "SHA256" in script
    assert '"$Repo|$Root|$User"' in script
    assert "[switch]$Replace" in script
    assert "if ($existing -and -not $Replace)" in script
    assert "Refusing to overwrite" in script
    assert "-Force:$Replace" in script


def test_scheduler_preserves_root_and_opt_in_jobs():
    script = read("tools/schedule_local.ps1")
    assert "$env:TAO_LOCAL_HOME" in script
    assert "$env:LOCALAPPDATA" in script
    assert "@('collect', 'subnets')" in script
    assert "New-TimeSpan -Hours 4" in script
    assert "New-TimeSpan -Minutes 5" in script
    assert "--once" in script
    assert "Quote-Argument $Root" in script
    assert "'census', 'seo', 'research', 'watch'" in script


def test_scheduled_collectors_wait_for_other_jobs():
    assert "--wait 21600" in read("tools/schedule_local.ps1")


def test_launchers_disable_inherited_delayed_expansion():
    # With cmd /V:ON, exclamation marks in paths or arguments otherwise
    # expand as variables even inside double quotes.
    for name in ('setup_local.cmd', 'run_local.cmd', 'collect_tao.cmd',
                 'edit_watchlist.cmd', 'run_tao_github.cmd',
                 'view_tao_directory.cmd', 'view_wallets_github.cmd',
                 'watch_wallets.cmd'):
        script = read(name)
        assert 'setlocal DisableDelayedExpansion' in script
        assert script.index('DisableDelayedExpansion') < script.index('%~dp0')


def test_scheduler_normalizes_directory_spelling_before_instance_hash():
    # Trailing separators must not change the install/status/remove task.
    # Preserve drive/UNC roots rather than turning C:\\ into drive-relative C:.
    script = read('tools/schedule_local.ps1')
    assert 'function Normalize-Directory' in script
    assert '[IO.Path]::GetPathRoot($full)' in script
    assert '$full.Length -gt $volume.Length' in script
    assert "$full.TrimEnd('\\')" in script
    assert ("$Repo = Normalize-Directory (Join-Path $PSScriptRoot '..')"
            in script)
    assert '$Root = Normalize-Directory $Root' in script
    assert script.index('$Root = Normalize-Directory $Root') < script.index(
        '$sha =')
