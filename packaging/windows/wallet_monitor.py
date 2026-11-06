"""Start the installed Windows wallet tools with a per-user data root."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser


APP = Path(__file__).resolve().parent
PYTHON = APP / "runtime" / "python.exe"
GH = APP / "tools" / "gh.exe"


def data_root():
    local = Path(os.environ["LOCALAPPDATA"])
    location = os.environ.get("STEALTH_WALLET_DATA")
    return Path(location or local / "StealthWalletMonitor" / "Data").resolve()


def stop_event_name(root, role="monitor"):
    path = os.path.normcase(str(root)).encode("utf8")
    key = hashlib.sha256(path).hexdigest()
    name = "Local\\StealthWalletMonitor-" + key[:32]
    return name if role == "monitor" else name + "-Editor"


def kernel():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL,
                               wintypes.BOOL, wintypes.LPCWSTR)
    k.CreateEventW.restype = wintypes.HANDLE
    k.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    k.OpenEventW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    k.SetEvent.argtypes = (wintypes.HANDLE,)
    k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k.GenerateConsoleCtrlEvent.argtypes = (wintypes.DWORD, wintypes.DWORD)
    return k


def running(root, stop=False, role="monitor"):
    k = kernel()
    handle = k.OpenEventW(2, False, stop_event_name(root, role))
    if not handle:
        if ctypes.get_last_error() == 2:
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if stop and not k.SetEvent(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k.CloseHandle(handle)
    return True


def worker(root, run, role="monitor"):
    k = kernel()
    handle = k.CreateEventW(None, True, False, stop_event_name(root, role))
    err = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(err)
    if err == 183:
        k.CloseHandle(handle)
        if role == "editor":
            return reopen_editor(root)
        print("The local monitor is already running.")
        return 0
    done = threading.Event()

    def listen():
        signaled = k.WaitForSingleObject(handle, 0xffffffff) == 0
        if signaled and not done.is_set():
            # This worker owns its own console. Ctrl+C lets the watcher finish
            # its local feed write and release its lock.
            k.GenerateConsoleCtrlEvent(0, 0)

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    try:
        return run()
    finally:
        done.set()
        k.SetEvent(handle)
        thread.join(timeout=5)
        k.CloseHandle(handle)


def stop_monitor(root, role="monitor"):
    label = "local monitor" if role == "monitor" else "watchlist editor"
    if not running(root, stop=True, role=role):
        print(f"The {label} is stopped.")
        return 0
    end = time.monotonic() + 20
    while time.monotonic() < end:
        if not running(root, role=role):
            print(f"Stopped the {label}.")
            return 0
        time.sleep(.2)
    print(f"The {label} is still stopping.", file=sys.stderr)
    return 1


def reopen_editor(root, port=8749):
    from seo.tao_watchlist import same_instance
    if not same_instance(root, port):
        raise RuntimeError("The editor is starting or another data folder "
                           "owns the local port. Try again after it closes.")
    webbrowser.open(f"http://127.0.0.1:{port}/")
    print("Opened the local watchlist.")
    return 0


def edit_watchlist(root):
    from seo.tao_watchlist import same_instance
    if same_instance(root, 8749):
        return reopen_editor(root)
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    # A dedicated hidden console lets the installer signal this editor alone.
    proc = subprocess.Popen(
        [str(PYTHON), "-B", str(Path(__file__)), "editor-worker"], cwd=root,
        env=dict(github_environment(), STEALTH_WALLET_DATA=str(root)),
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=info,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    end = time.monotonic() + 12
    while time.monotonic() < end:
        if same_instance(root, 8749):
            print("Opened the local watchlist.")
            return 0
        if proc.poll() is not None:
            if proc.returncode == 0:
                return 0
            raise RuntimeError("The watchlist could not open. Close any "
                               "other editor on local port 8749 and retry.")
        time.sleep(.15)
    raise RuntimeError("The editor has not opened. Check the data directory "
                       "and local port 8749.")


def open_report(root):
    from seo import tao_view
    cache = root / "github_out"
    output = cache / "wallets.html"
    if not output.is_file():
        snapshot = tao_view.newest_cached(cache)
        if snapshot is None:
            os.startfile(APP / "welcome.html")
            return 0
        tao_view.main(["--input", str(snapshot), "--out", str(output),
                       "--cache", str(cache)])
    os.startfile(output)
    return 0


def rebuild_imported_views(root):
    from seo import tao_view
    cache = root / "github_out"
    latest = tao_view.newest_cached(cache)
    if latest is None:
        return
    for folder in cache.iterdir():
        if (folder.is_dir() and folder.name.isascii()
                and folder.name.isdecimal()):
            source = tao_view.run_snapshot(folder)
            if source and source != latest:
                saved = tao_view.load_snapshot(source)
                tao_view._archive("", saved, source, cache)
    tao_view.main(["--input", str(latest), "--out",
                   str(cache / "wallets.html"), "--cache", str(cache)])


def github_environment():
    keys = {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "HOME", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "TMP", "TEMP", "TMPDIR"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keys}
    env.update(GH_NO_UPDATE_NOTIFIER="1", GH_HOST="github.com")
    return env


def import_explorer(root, source):
    # The user explicitly selects this standalone HTML from their transfer kit.
    if source.suffix.lower() != ".html" or not source.is_file():
        raise ValueError("Select the standalone network HTML file.")
    dest = root / "explorer" / "network.html"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source == dest:
        return
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as copied:
        temporary = Path(copied.name)
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(dest)
    finally:
        temporary.unlink(missing_ok=True)
    print("Saved transfer explorer:", dest)


def open_transfers(root):
    from seo import tao_network_view, tao_view
    saved = root / "explorer" / "network.html"
    if saved.is_file():
        os.startfile(saved)
        return
    cache = root / "github_out"
    sources = []
    if cache.is_dir():
        for folder in sorted(cache.iterdir()):
            if folder.is_dir() and folder.name.isdecimal():
                report = tao_view.run_snapshot(folder)
                data = tao_view.load_snapshot(report) if report else {}
                if "transfer_details" in data or "transfers" in data:
                    sources.extend(["--report", str(report)])
    if not sources:
        raise ValueError("Import a wallet backup containing transfer history "
                         "or a standalone explorer to see transfers.")
    output = cache / "network_from_reports.html"
    tao_network_view.main([*sources, "--out", str(output)])
    os.startfile(output)


def execute(args, root):
    if args.command == "open":
        return open_report(root)
    if args.command == "edit-watchlist":
        return edit_watchlist(root)
    if args.command == "editor-worker":
        from seo.tao_watchlist import main as editor
        try:
            return worker(root, lambda: editor(["--open"]), role="editor")
        except KeyboardInterrupt:
            return 0
    if args.command == "stop-editor":
        return stop_monitor(root, role="editor")
    if args.command == "transfers":
        return open_transfers(root)
    if args.command == "import-explorer":
        return import_explorer(root, args.archive)
    if args.command == "download":
        from seo import tao_view
        cache = root / "github_out"
        snapshot, warning = tao_view.refresh_latest(cache)
        if warning:
            print(warning, file=sys.stderr)
        if snapshot is None:
            print("No saved wallet report yet. Use GitHub login or import "
                  "a backup.", file=sys.stderr)
            return 1
        tao_view.main(["--input", str(snapshot), "--cache", str(cache),
                       "--out", str(cache / "wallets.html")])
        print("Snapshot:", snapshot)
        return 1 if warning else 0
    if args.command in ("import", "export"):
        from seo import tao_portable
        argv = [args.command, str(args.archive), "--root", str(root)]
        if args.include_local_feed:
            argv.append("--include-local-feed")
        result = tao_portable.main(argv)
        if result == 0 and args.command == "import":
            rebuild_imported_views(root)
        return result
    if args.command == "login":
        login = [str(GH), "auth", "login", "--hostname", "github.com", "--web"]
        return subprocess.call(login, env=github_environment())
    if args.command == "status":
        print("Local monitor:", "running" if running(root) else "stopped")
        print("Wallet data:", root)
        return 0
    if args.command == "stop":
        return stop_monitor(root)
    if args.command == "start":
        if running(root):
            print("The local monitor is already running.")
            return 0
        proc = subprocess.Popen([str(PYTHON), "-B", str(Path(__file__)),
                                 "worker"], cwd=root,
                                creationflags=subprocess.CREATE_NEW_CONSOLE)
        for _ in range(60):
            if running(root):
                print("Started the local monitor in a separate console.")
                return 0
            if proc.poll() is not None:
                return proc.returncode or 1
            time.sleep(.1)
        print("The monitor is still starting. See its console.")
        return 0
    if args.command == "worker":
        from seo.tao_local_watch import main as watch
        return worker(root, lambda: watch([
            "--refresh-snapshots", "--prompt"]))
    if args.command == "self-test":
        from substrateinterface import SubstrateInterface
        from seo import tao_local_watch, tao_portable, tao_view
        from seo import tao_chain_census, tao_chain_window, tao_network_view
        from seo import tao_directory_view, tao_movement_archive, tao_watchlist
        report = {"performance": {"wallets": [], "rankings": {}}}
        assert callable(tao_local_watch.poll) and callable(tao_portable.main)
        assert callable(tao_chain_census.main) and callable(
            tao_chain_window.main)
        assert "<html" in tao_network_view.render(
            tao_network_view.build()).lower()
        assert "<!doctype html>" in tao_view.render(report).lower()
        assert SubstrateInterface
        assert callable(tao_directory_view.main)
        assert callable(tao_movement_archive.report)
        assert callable(tao_watchlist.server)
        assert (APP / "seo/watchlist_editor.html").is_file()
        subprocess.run([str(GH), "--version"], check=True, timeout=15,
                       env=github_environment())
        print("Offline viewer, migration and local watcher imports passed.")
        return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    subs = p.add_subparsers(dest="command", required=True)
    for command in ("open", "transfers", "download", "login", "start", "stop",
                    "status",
                    "worker", "self-test", "edit-watchlist", "editor-worker",
                    "stop-editor"):
        subs.add_parser(command)
    for command in ("import", "export"):
        sp = subs.add_parser(command)
        sp.add_argument("archive", type=Path)
        sp.add_argument("--include-local-feed", action="store_true")
    subs.add_parser("import-explorer").add_argument("archive", type=Path)
    args = p.parse_args(argv)
    if args.command in ("import", "export", "import-explorer"):
        args.archive = args.archive.resolve()
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(GH.parent) + os.pathsep + path
    try:
        return execute(args, root) or 0
    except (OSError, ValueError, RuntimeError,
            subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
