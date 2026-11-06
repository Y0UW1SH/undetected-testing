"""Build the Windows x64 installer from a reviewed, hash-locked payload."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile


SOURCE = Path(__file__).resolve().parent
REPO = SOURCE.parents[1]
MODULES = ("__init__.py", "tao_view.py", "wallet_view.html",
           "tao_portable.py", "tao_local_watch.py", "tao_chain_census.py",
           "tao_chain_window.py", "tao_network_view.py", "tao_watchlist.py",
           "watchlist_editor.html", "tao_directory_view.py",
           "tao_movement_archive.py", "tao_wallet_groups.py",
           "tao_subnet_risk.py", "tao_subnet_context.py",
           "tao_native_holders.py", "tao_native_priority.py",
           "tao_native_evidence.py", "tao_holder_queue.py")


def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def inputs(cache, offline):
    tools = cache / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    result = {}
    specs = json.loads((SOURCE / "sources.json").read_text())
    for name, spec in specs.items():
        path = tools / spec["file"]
        if not path.is_file() or sha(path) != spec["sha256"]:
            if offline:
                raise ValueError(f"Unverified offline input: {path.name}")
            for url in spec["urls"]:
                try:
                    request = urllib.request.Request(
                        url, headers={"User-Agent": "StealthWalletMonitor"})
                    with tempfile.NamedTemporaryFile(
                            dir=tools, delete=False) as f:
                        temp = Path(f.name)
                        try:
                            with urllib.request.urlopen(
                                    request, timeout=90) as response:
                                shutil.copyfileobj(response, f)
                        except Exception:
                            f.close()
                            temp.unlink(missing_ok=True)
                            raise
                    try:
                        if sha(temp) != spec["sha256"]:
                            raise ValueError(f"Checksum mismatch: {path.name}")
                        os.replace(temp, path)
                    finally:
                        temp.unlink(missing_ok=True)
                    break
                except OSError:
                    continue
            else:
                raise OSError(f"Could not fetch verified input: {path.name}")
        result[name] = path
    return result


def wheel_inputs(cache, offline):
    lock = SOURCE / "watch-wheels.txt"
    pins = set(re.findall(r"--hash=sha256:([a-f0-9]{64})", lock.read_text()))
    folder = cache / "wheels"
    folder.mkdir(exist_ok=True)
    found = {sha(w): w for w in folder.glob("*.whl")}
    if not pins <= found.keys() and not offline:
        subprocess.run([sys.executable, "-m", "pip", "--isolated", "download",
                        "--index-url", "https://pypi.org/simple",
                        "--require-hashes",
                        "--platform", "win_amd64", "--python-version", "3.13",
                        "--implementation", "cp", "--abi", "cp313",
                        "--abi", "abi3",
                        "--only-binary=:all:", "--no-deps", "-r", str(lock),
                        "-d", str(folder)], check=True)
        found = {sha(w): w for w in folder.glob("*.whl")}
    if not pins or not pins <= found.keys():
        raise ValueError("Incomplete hash-locked wheel cache")
    return sorted(found[key] for key in pins)


def relative(name):
    parts = PurePosixPath(name).parts
    if not parts or any(part in (".", "..", "/") or ":" in part
                        or "\\" in part for part in parts):
        raise ValueError("Unexpected path in a build archive")
    return Path(*parts)


def unpack_zip(archive, target):
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            if entry.is_dir():
                continue
            dest = target / relative(entry.filename)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(entry) as source, dest.open("wb") as out:
                shutil.copyfileobj(source, out)


def runtime(archive, wheels, payload):
    root = payload / "runtime"
    excluded = {"__pycache__", "site-packages", "test", "tests", "idlelib",
                "turtledemo", "tkinter", "ensurepip", "venv"}
    with tarfile.open(archive) as t:
        for member in t:
            if not member.isfile():
                continue
            path = relative(member.name).relative_to("python")
            if len(path.parts) > 1 and path.parts[0] not in ("Lib", "DLLs"):
                continue
            if (excluded.intersection(path.parts)
                    or path.suffix in (".pyc", ".pdb")):
                continue
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            with t.extractfile(member) as source, dest.open("wb") as out:
                shutil.copyfileobj(source, out)
    site = root / "Lib" / "site-packages"
    for wheel in wheels:
        unpack_zip(wheel, site)
    (root / "python313._pth").write_text(
        "Lib\nDLLs\n.\nLib/site-packages\n..\n", encoding="ascii")


def payload_files(payload, tool, wheels):
    runtime(tool["python"], wheels, payload)
    (payload / "seo").mkdir()
    for name in MODULES:
        shutil.copyfile(REPO / "seo" / name, payload / "seo" / name)
    for name in ("wallet_monitor.py", "wallet-monitor.cmd", "welcome.html"):
        shutil.copyfile(SOURCE / name, payload / name)
    licenses = payload / "licenses"
    licenses.mkdir()
    shutil.copyfile(REPO / "LICENSE", licenses / "application.txt")
    (payload / "tools").mkdir()
    with zipfile.ZipFile(tool["gh"]) as z:
        (payload / "tools" / "gh.exe").write_bytes(z.read("bin/gh.exe"))
        (licenses / "GitHub-CLI.txt").write_bytes(z.read("LICENSE"))
    with zipfile.ZipFile(tool["nsis"]) as z:
        (licenses / "NSIS.txt").write_bytes(z.read("nsis-3.12/COPYING"))
    framework = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64"
    csc = framework / "v4.0.30319/csc.exe"
    subprocess.run([str(csc), "/nologo", "/target:winexe", "/optimize+",
                    "/codepage:65001",
                    "/r:System.dll", "/r:System.Drawing.dll",
                    "/r:System.Windows.Forms.dll",
                    "/out:" + str(payload / "StealthWalletMonitor.exe"),
                    str(SOURCE / "Desktop.cs")], check=True)


def uninstall_manifest(payload, output):
    lines = []
    dirs = []
    for path in sorted(payload.rglob("*")):
        rel = str(path.relative_to(payload)).replace("$", "$$")
        if path.is_dir():
            dirs.append(rel)
        else:
            lines.append(f'Delete "$INSTDIR\\app\\{rel}"')
    for directory in sorted(dirs, key=lambda p: p.count("\\"), reverse=True):
        lines.append(f'RMDir "$INSTDIR\\app\\{directory}"')
    lines.append('RMDir "$INSTDIR\\app"')
    output.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def build(cache, output, offline):
    if sys.platform != "win32":
        raise SystemExit("Run this build on Windows x64 with Python 3.11+.")
    cache.mkdir(parents=True, exist_ok=True)
    tool = inputs(cache, offline)
    wheels = wheel_inputs(cache, offline)
    with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as work:
        tmp = Path(work)
        payload = tmp / "payload"
        payload_files(payload, tool, wheels)
        unpack_zip(tool["nsis"], tmp)
        cleanup = tmp / "cleanup.nsh"
        installer = tmp / "StealthWalletMonitor-x64.exe"
        uninstall_manifest(payload, cleanup)
        compiler = tmp / "nsis-3.12" / "makensis.exe"
        subprocess.run([str(compiler), "/V2", "/NOCD",
                        f"/DPAYLOAD={payload}", f"/DCLEANUP={cleanup}",
                        f"/DOUTFILE={installer}",
                        str(SOURCE / "installer.nsi")], check=True)
        with installer.open("rb") as f:
            if f.read(2) != b"MZ":
                raise ValueError("Missing installer executable")
        output.parent.mkdir(parents=True, exist_ok=True)
        # A move would retain the private temporary directory's ACL.
        staged = output.with_name(f".wallet-{uuid.uuid4().hex}.exe")
        try:
            shutil.copyfile(installer, staged)
            staged.replace(output)
        finally:
            staged.unlink(missing_ok=True)
    digest = sha(output)
    output.with_suffix(".exe.sha256").write_text(
        f"{digest}  {output.name}\n", encoding="ascii")
    print(f"{output} ({output.stat().st_size:,} bytes)\nSHA256 {digest}")


def write_kit(installer, target):
    """Bundle only the setup, checked digest and installation guide."""
    digest_file = installer.with_suffix(".exe.sha256")
    expected = f"{sha(installer)}  {installer.name}"
    if (installer.suffix.lower() != ".exe" or target.suffix.lower() != ".zip"
            or digest_file.read_text("ascii").strip() != expected):
        raise ValueError("Use the verified installer and its SHA-256 file")
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".kit-{uuid.uuid4().hex}.zip")
    try:
        with zipfile.ZipFile(staged, "x", zipfile.ZIP_STORED) as kit:
            for path in (installer, digest_file, SOURCE / "README.md"):
                kit.write(path, path.name)
        with zipfile.ZipFile(staged) as kit:
            if kit.testzip():
                raise ValueError("Installer kit verification failed")
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path,
                   default=REPO / "try_tao_out" / "packager")
    p.add_argument("--output", type=Path, default=REPO / "dist" / "windows"
                   / "StealthWalletMonitor-Setup-x64.exe")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--kit", type=Path,
                   help="also create a clean ZIP containing setup and guide")
    args = p.parse_args()
    build(args.cache.resolve(), args.output.resolve(), args.offline)
    if args.kit is not None:
        write_kit(args.output.resolve(), args.kit.resolve())


if __name__ == "__main__":
    main()
