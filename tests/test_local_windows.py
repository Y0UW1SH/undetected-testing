"""Static contracts; Task Scheduler still needs a Windows smoke test."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
