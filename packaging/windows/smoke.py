"""Verify setup and uninstall in an isolated Windows user directory."""
import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import winreg
import zipfile


def test_env(folder):
    keys = {"WINDIR", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keys}
    profile = folder / "Fresh Profile"
    env.update(USERPROFILE=str(profile), HOME=str(profile),
               LOCALAPPDATA=str(profile / "Local"),
               APPDATA=str(profile / "Roaming"),
               PATH=str(Path(env["WINDIR"]) / "System32"),
               STEALTH_WALLET_DATA=str(folder / "Wallet Data"))
    keys = ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "STEALTH_WALLET_DATA")
    for key in keys:
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    return env


def hidden():
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return info


def run(args, env, *, check=True):
    proc = subprocess.run(args, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=150, startupinfo=hidden(), text=True,
                          errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"Exit {proc.returncode}: {args}\n{proc.stdout}")
    return proc


def registration():
    name = r"Software\StealthWalletMonitor"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name) as key:
            return winreg.QueryValueEx(key, "InstallDir")[0]
    except FileNotFoundError:
        return None


def seed(root):
    for run_id in (10, 11):
        stamp = f"2026-11-{run_id}T00:00:00+00:00"
        folder = root / "github_out" / str(run_id)
        report = folder / "tao-wallet-report/tao_collect.json"
        report.parent.mkdir(parents=True)
        data = dict(checked_at=stamp, following=[], coverage={}, transfers=[],
                    performance=dict(wallets=[], rankings={}))
        report.write_text(json.dumps(data))
        state = folder / "tao-wallet-state/wallets.sqlite"
        state.parent.mkdir(parents=True)
        with closing(sqlite3.connect(state)) as db:
            db.execute("CREATE TABLE wallet_events (id text)")
            db.execute("CREATE TABLE wallet_performance_runs "
                       "(checked_at text)")
            db.execute("INSERT INTO wallet_performance_runs VALUES (?)",
                       (stamp,))
            db.commit()
    private = root / "try_tao_out" / "local_wallet_activity.json"
    private.parent.mkdir()
    private.write_text(json.dumps(dict(
        source="local-finney-events-v1", addresses=[], events=[],
        last_scanned_block=12345, rpc_attempts=2,
        rpc_retry={"stage": "block_hash", "type": "connection"})))


def stop_standby(python, test, root, env):
    # Test the real named stop signal in an isolated console; the standby
    # callback sleeps and never opens a connection or dispatches a workflow.
    script = root / "standby.py"
    script.write_text("from wallet_monitor import data_root, worker\n"
                      "import time\n"
                      "try:\n    worker(data_root(), lambda: time.sleep(90))\n"
                      "except KeyboardInterrupt:\n    pass\n")
    proc = subprocess.Popen([str(python), "-B", str(script)], env=env,
                            creationflags=subprocess.CREATE_NEW_CONSOLE,
                            startupinfo=hidden())
    try:
        deadline = time.monotonic() + 15
        while "running" not in test("status"):
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Isolated standby worker failed to start")
            time.sleep(.2)
        assert "already running" in test("start")
        assert "Stopped the local monitor" in test("stop")
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def desktop(exe, env):
    """Start and close the installed window without selecting an action."""
    ui = ctypes.WinDLL("user32", use_last_error=True)
    visit_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    ui.EnumWindows.argtypes = (visit_type, wintypes.LPARAM)
    ui.EnumChildWindows.argtypes = (wintypes.HWND, visit_type, wintypes.LPARAM)
    ui.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    ui.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    ui.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM)
    proc = subprocess.Popen([str(exe)], env=env, startupinfo=hidden())
    found = []

    @visit_type
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        ui.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == proc.pid:
            title = ctypes.create_unicode_buffer(256)
            ui.GetWindowTextW(hwnd, title, len(title))
            if title.value == "Stealth Wallet Monitor":
                found.append(hwnd)
        return True

    try:
        deadline = time.monotonic() + 15
        while not found and time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            ui.EnumWindows(visit, 0)
            time.sleep(.1)
        if not found:
            raise RuntimeError("Desktop launcher did not show its main window")
        controls = []

        @visit_type
        def child(hwnd, _):
            caption = ctypes.create_unicode_buffer(128)
            ui.GetWindowTextW(hwnd, caption, len(caption))
            controls.append(caption.value)
            return True

        ui.EnumChildWindows(found[0], child, 0)
        assert "Edit watchlist" in controls, "Installed editor button missing"
        ui.PostMessageW(found[0], 0x0010, 0, 0)  # WM_CLOSE
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def smoke(setup, folder, real_backup=None, real_explorer=None):
    before = registration()
    env = test_env(folder)
    root = Path(env["STEALTH_WALLET_DATA"])
    install = folder / "Programs With Spaces" / "Wallet Monitor"
    unrelated = folder / "Unrelated App"
    unrelated.mkdir()
    (unrelated / "ignore.txt").write_text("untouched")
    # NSIS requires a final, unquoted /D argument even for spaces.
    run(f'"{setup}" /S /NOINTEGRATION /D={unrelated}', env, check=False)
    assert list(unrelated.iterdir()) == [unrelated / "ignore.txt"]
    run(f'"{setup}" /S /NOINTEGRATION /D={install}', env)
    assert (install / "no-shell-integration").is_file()
    assert registration() == before
    app = install / "app"
    python = app / "runtime/python.exe"
    launcher = app / "wallet_monitor.py"
    assert (app / "StealthWalletMonitor.exe").is_file()
    assert (app / "welcome.html").is_file()
    repo = Path(__file__).resolve().parents[2]
    from build import MODULES
    assert {f.name for f in (app / "seo").iterdir()} == set(MODULES)
    for module in MODULES:
        actual = (app / "seo" / module).read_bytes()
        assert actual == (repo / "seo" / module).read_bytes(), module
    desktop(app / "StealthWalletMonitor.exe", env)
    print("Installed desktop window started and closed.", flush=True)

    def command(*args, current_env=env):
        return run([str(python), "-B", str(launcher), *map(str, args)],
                   current_env).stdout

    print(command("self-test"), end="", flush=True)
    assert "stopped" in command("status")
    assert "stopped" in command("stop-editor")
    assert "stopped" in command("stop")
    stop_standby(python, command, folder, env)
    print("Standby start, duplicate detection and stop passed.", flush=True)
    source = folder / "Source data"
    seed(source)
    backup = folder / "Public test backup.zip"
    other_env = dict(env, STEALTH_WALLET_DATA=str(source))
    command("export", backup, current_env=other_env)
    with zipfile.ZipFile(backup) as archive:
        assert len(archive.namelist()) == 5
        assert all("local_wallet_activity" not in n
                   for n in archive.namelist())
    command("import", backup)
    assert '"skipped": 4' in command("import", backup)
    html = (root / "github_out/wallets.html").read_text(encoding="utf8")
    for run_id in ("10", "11"):
        assert f'{run_id}/wallets.html' in html
    assert not (root / "try_tao_out/local_wallet_activity.json").exists()
    print("Two reports imported, links verified, private feed excluded.",
          flush=True)
    private_zip = folder / "Explicit private backup.zip"
    command("export", private_zip, "--include-local-feed",
            current_env=other_env)
    restored = folder / "Private restore"
    command("import", private_zip, "--include-local-feed",
            current_env=dict(env, STEALTH_WALLET_DATA=str(restored)))
    initial = source / "try_tao_out/local_wallet_activity.json"
    transferred = restored / "try_tao_out/local_wallet_activity.json"
    assert json.loads(transferred.read_text()) == json.loads(
        initial.read_text())
    print("Opt-in private checkpoint and bounded retry status round-tripped.",
          flush=True)
    exercise = folder / "verify-explorer.py"
    exercise.write_text(
        "from pathlib import Path\n"
        "import wallet_monitor as w\n"
        "seen = []\n"
        "w.os.startfile = seen.append\n"
        "w.open_transfers(w.data_root())\n"
        "assert seen and seen[0].is_file()\n"
        "print(seen[0].name)\n")
    cmd = [str(python), "-B", str(exercise)]
    assert "network_from_reports.html" in run(cmd, env).stdout
    if real_backup is not None:
        migrated = folder / "Migrated data"
        actual_env = dict(env, STEALTH_WALLET_DATA=str(migrated))
        print(command("import", real_backup, current_env=actual_env).strip(),
              flush=True)
        assert (migrated / "github_out/wallets.html").is_file()
        with zipfile.ZipFile(real_backup) as archive:
            members = [name for name in archive.namelist()
                       if name.startswith("github_out/")]
        for name in members:
            assert (migrated / name).is_file()
        runs = {name.split("/")[1] for name in members}
        navigation = (migrated / "github_out/wallets.html").read_text(
            encoding="utf8")
        for run_id in runs:
            assert f'{run_id}/wallets.html' in navigation
        assert "network_from_reports.html" in run(cmd, actual_env).stdout
        print(f"Imported {len(members) // 2} pairs in {len(runs)} runs "
              f"({', '.join(sorted(runs))}); rendered transfer evidence.",
              flush=True)
    sample = root / "github_out/network_from_reports.html"
    command("import-explorer", real_explorer or sample)
    imported = root / "explorer/network.html"
    assert imported.read_bytes() == (real_explorer or sample).read_bytes()
    assert "network.html" in run(cmd, env).stdout
    print("Saved standalone transfer explorer opened offline.", flush=True)
    verify = Path(__file__).with_name("smoke_editor.py")
    print(run([str(python), "-B", str(verify)], env).stdout, end="",
          flush=True)
    keep = app / "user-added.txt"
    keep.write_text("preserve")
    uninstall = install / "Uninstall.exe"
    run(f'"{uninstall}" /S', env)
    deadline = time.monotonic() + 60
    while (install / "wallet-install.id").exists() or uninstall.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("Uninstaller did not complete")
        time.sleep(.3)
    assert not python.exists() and not launcher.exists()
    assert keep.read_text() == "preserve"
    assert (root / "github_out/wallets.html").is_file()
    assert registration() == before
    print("Uninstall preserved user files and reports, without registration.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("setup", type=Path)
    p.add_argument("--backup", type=Path)
    p.add_argument("--explorer", type=Path)
    args = p.parse_args()
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="wallet-setup-smoke-") as tmp:
        smoke(args.setup.resolve(), Path(tmp), args.backup, args.explorer)
    print(f"Installation smoke completed in {time.monotonic() - start:.1f}s")


if __name__ == "__main__":
    main()
