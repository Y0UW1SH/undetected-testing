"""The Windows launcher ships the editor and isolates its data."""
import importlib.util
from pathlib import Path
import zipfile

import pytest

WINDOWS = Path(__file__).resolve().parents[1] / "packaging" / "windows"


def load(name):
    path = WINDOWS / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_payload_includes_local_editor_archive_and_template():
    modules = load("build").MODULES
    needed = {"tao_watchlist.py", "tao_directory_view.py",
              "tao_movement_archive.py", "watchlist_editor.html"}
    assert needed <= set(modules)
    assert all(name.endswith((".py", ".html")) for name in modules)


def test_editor_reopens_only_verified_data_root(tmp_path, monkeypatch):
    launcher = load("wallet_monitor")
    import seo.tao_watchlist as watchlist
    opened = []
    monkeypatch.setattr(watchlist, "same_instance", lambda root, port: False,
                        raising=False)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    with pytest.raises(RuntimeError):
        launcher.reopen_editor(tmp_path)
    assert not opened
    monkeypatch.setattr(watchlist, "same_instance",
                        lambda root, port: root == tmp_path and port == 8749)
    launcher.reopen_editor(tmp_path)
    assert opened == ["http://127.0.0.1:8749/"]
    assert launcher.stop_event_name(tmp_path, "editor") != (
        launcher.stop_event_name(tmp_path))


def test_kit_contains_only_installer_digest_and_guide(tmp_path):
    builder = load("build")
    exe = tmp_path / "Example-Setup.exe"
    exe.write_bytes(b"MZfixture")
    (tmp_path / "dwellir.txt").write_text("private-fixture")
    (tmp_path / "local_watchlist.sqlite").write_bytes(b"private-fixture")
    exe.with_suffix(".exe.sha256").write_text(
        builder.sha(exe) + "  " + exe.name + "\n")
    kit = tmp_path / "installer-kit.zip"
    builder.write_kit(exe, kit)
    with zipfile.ZipFile(kit) as archive:
        assert set(archive.namelist()) == {
            exe.name, exe.name + ".sha256", "README.md"}
        assert archive.read(exe.name) == exe.read_bytes()
