import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing

import pytest

from seo import tao_portable as portable


STAMP = "2026-11-05T23:00:00+00:00"
REPORT = "tao-wallet-report/tao_collect.json"
STATE = "tao-wallet-state/wallets.sqlite"
FEED = "try_tao_out/local_wallet_activity.json"


def pair(root, run="12", stamp=STAMP):
    folder = root / "github_out" / run
    report = folder / REPORT
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps(dict(checked_at=stamp, following=[],
                                      coverage={}, performance={})))
    db = folder / STATE
    db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("create table wallet_events (id text)")
        conn.execute("create table wallet_performance_runs (checked_at text)")
        conn.execute("insert into wallet_performance_runs values (?)",
                     (stamp,))
        conn.commit()
    return report, db


def feed(root):
    path = root / FEED
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(dict(source="local-finney-events-v1",
                                    events=[], addresses=[],
                                    last_scanned_block=12345)))
    return path


def members(path):
    with zipfile.ZipFile(path) as z:
        return {info.filename: z.read(info) for info in z.infolist()}


def repack(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)


def test_portable_round_trip_only_pairs_and_reruns(tmp_path):
    src = tmp_path / "old"
    report, db = pair(src)
    pair(src, "12/artifacts/993")
    feed(src)
    for path in (src / ".env", db.with_name("debug.log"),
                 report.with_name("browser-profile.json")):
        path.write_text("PRIVATE_FIXTURE_SENTINEL")
    archive = tmp_path / "data.zip"
    portable.export_bundle(src, archive)
    files = members(archive)
    assert set(files) == {"manifest.json", *(
        "github_out/" + run + "/" + leaf
        for run in ("12", "12/artifacts/993") for leaf in (REPORT, STATE))}
    assert all(b"PRIVATE_FIXTURE_SENTINEL" not in v for v in files.values())
    dest = tmp_path / "new"
    portable.import_bundle(dest, archive)
    assert (dest / report.relative_to(src)).read_bytes() == report.read_bytes()
    assert portable.import_bundle(dest, archive)["skipped"] == 4
    assert portable.import_bundle(src, archive)["skipped"] == 4
    with pytest.raises((FileExistsError, ValueError), match="exist"):
        portable.export_bundle(src, archive)


def test_private_feed_explicit_and_locked(tmp_path):
    src = tmp_path / "old"
    pair(src)
    private = feed(src)
    archive = tmp_path / "private.zip"
    with closing(sqlite3.connect(str(private) + ".lock")) as lock:
        lock.execute("begin exclusive")
        with pytest.raises(ValueError, match="watcher"):
            portable.export_bundle(src, archive, include_local_feed=True)
    assert not archive.exists()
    portable.export_bundle(src, archive, include_local_feed=True)
    dest = tmp_path / "new"
    with pytest.raises(ValueError, match="include-local-feed"):
        portable.import_bundle(dest, archive)
    assert not (dest / "github_out").exists()
    portable.import_bundle(dest, archive, include_local_feed=True)
    assert (dest / FEED).read_bytes() == private.read_bytes()
    assert not any(name.endswith(".lock") for name in members(archive))


@pytest.mark.parametrize("fields", [
    {},
    {"rpc_attempts": 0},
    {"rpc_attempts": 1, "rpc_error": {"stage": "decode_events",
                                      "type": "invalid_data"}},
    {"rpc_attempts": 2, "rpc_retry": {"stage": "block_events",
                                      "type": "connection"},
     "rpc_error": {"stage": "connect", "type": "timeout"}},
])
def test_private_watch_versions_and_retry_round_trip(tmp_path, fields):
    private = feed(tmp_path)
    data = dict(json.loads(private.read_text()), **fields)
    private.write_text(json.dumps(data))
    archive = tmp_path / "move.zip"
    portable.export_bundle(tmp_path, archive, include_local_feed=True)
    restored = tmp_path / "restored"
    portable.import_bundle(restored, archive, include_local_feed=True)
    assert json.loads((restored / FEED).read_text()) == data


@pytest.mark.parametrize("fields", [
    {"rpc_attempts": True}, {"rpc_attempts": "2"}, {"rpc_attempts": 3},
    {"rpc_attempts": -1}, {"rpc_attempts": 1.0},
    {"rpc_retry": {"stage": "wss://example.invalid", "type": "connection"}},
    {"rpc_error": {"stage": "connect", "type": "PRIVATE_TEST_SENTINEL"}},
    {"rpc_error": {"stage": "connect", "type": "connection", "message": "x"}},
    {"rpc_error": {"stage": "connect"}}, {"rpc_retry": None},
    {"rpc_error": {"stage": ["connect"], "type": "connection"}},
])
def test_private_feed_rejects_untrusted_rpc_diagnostics(tmp_path, fields):
    private = feed(tmp_path)
    data = dict(json.loads(private.read_text()), **fields)
    private.write_text(json.dumps(data))
    archive = tmp_path / "move.zip"
    with pytest.raises(ValueError, match="watcher feed"):
        portable.export_bundle(tmp_path, archive, include_local_feed=True)
    assert not archive.exists()


def test_restore_rejects_untrusted_diagnostic_with_valid_checksum(tmp_path):
    private = feed(tmp_path)
    archive = tmp_path / "move.zip"
    portable.export_bundle(tmp_path, archive, include_local_feed=True)
    files = members(archive)
    data = json.loads(private.read_text())
    data.update(rpc_error={"stage": "connect", "type": "unexpected text"})
    files[FEED] = json.dumps(data).encode()
    manifest = json.loads(files["manifest.json"])
    manifest["files"][FEED] = dict(sha256=hashlib.sha256(files[FEED])
                                   .hexdigest(), size=len(files[FEED]))
    files["manifest.json"] = json.dumps(manifest)
    repack(archive, files)
    restored = tmp_path / "restored"
    with pytest.raises(ValueError, match="watcher feed RPC diagnostic"):
        portable.import_bundle(restored, archive, include_local_feed=True)
    assert not (restored / FEED).exists()


def test_committed_wal_only_and_detect_changed_db(tmp_path):
    src = tmp_path / "old"
    _, db = pair(src)
    archive = tmp_path / "data.zip"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("pragma journal_mode=wal")
        conn.execute("insert into wallet_events values ('committed')")
        conn.commit()
        conn.execute("insert into wallet_events values ('in flight')")
        portable.export_bundle(src, archive)
        conn.rollback()
    dest = tmp_path / "new"
    portable.import_bundle(dest, archive)
    restored = dest / db.relative_to(src)
    with closing(sqlite3.connect(restored)) as conn:
        assert conn.execute("select id from wallet_events").fetchall() == [
            ("committed",)]
        conn.execute("pragma journal_mode=wal")
        conn.execute("insert into wallet_events values ('post-import')")
        conn.commit()
        with pytest.raises(ValueError, match="conflict"):
            portable.import_bundle(dest, archive)


def test_incomplete_pair_and_wrong_timestamp_rejected(tmp_path):
    report, db = pair(tmp_path)
    archive = tmp_path / "bundle.zip"
    report.write_text(report.read_text().replace("23:00:00", "00:00:00"))
    with pytest.raises(ValueError, match="timestamp"):
        portable.export_bundle(tmp_path, archive)
    db.unlink()
    with pytest.raises(ValueError, match="incomplete"):
        portable.export_bundle(tmp_path, archive)
    assert not archive.exists()


def test_select_runs_and_check_restored_private_feed(tmp_path):
    src = tmp_path / "old"
    pair(src)
    pair(src, "13")
    private = feed(src)
    data = json.loads(private.read_text())
    data.update(events=[{"dst_alpha": 5, "dst_alpha_units": "5000000000"}],
                snapshot_refresh=dict(checked_at=STAMP, status="ok"))
    private.write_text(json.dumps(data))
    archive = tmp_path / "backup.zip"
    assert portable.main(["export", str(archive), "--root", str(src),
                          "--run", "13", "--include-local-feed"]) == 0
    dest = tmp_path / "new"
    portable.import_bundle(dest, archive, include_local_feed=True)
    assert not (dest / "github_out/12").exists()
    assert (dest / FEED).read_bytes() == private.read_bytes()
    extra = tmp_path / "missing.zip"
    with pytest.raises(ValueError, match="requested"):
        portable.export_bundle(src, extra, run_ids=["14"])


@pytest.mark.parametrize("bad", ["../escaped.json", "C:/out", "A\\B", ".env",
                                 "github_out/12/" + STATE + ":x"])
def test_malicious_member_rejected_before_any_restore(tmp_path, bad):
    src = tmp_path / "old"
    pair(src)
    archive = tmp_path / "backup.zip"
    portable.export_bundle(src, archive)
    files = members(archive)
    files[bad] = b"wrong"
    repack(archive, files)
    dest = tmp_path / "new"
    with pytest.raises(ValueError):
        portable.import_bundle(dest, archive)
    assert not (dest / "github_out").exists()
    assert not (tmp_path / "escaped.json").exists()


def test_checksum_and_conflict_do_not_partially_restore(tmp_path):
    src = tmp_path / "old"
    pair(src)
    report, _ = pair(src, "13")
    archive = tmp_path / "backup.zip"
    portable.export_bundle(src, archive)
    dest = tmp_path / "new"
    conflict = dest / report.relative_to(src)
    conflict.parent.mkdir(parents=True)
    conflict.write_text("a user's existing file")
    with pytest.raises(ValueError, match="conflict"):
        portable.import_bundle(dest, archive)
    assert conflict.read_text() == "a user's existing file"
    assert not (dest / "github_out/12").exists()
    files = members(archive)
    name = "github_out/12/" + REPORT
    files[name] = files[name].replace(b"23:00:00", b"01:00:00")
    repack(archive, files)
    with pytest.raises(ValueError, match="checksum"):
        portable.import_bundle(dest, archive)
    assert not (dest / "github_out/12").exists()


def test_duplicate_version_bomb_and_unpaired_manifest(tmp_path, monkeypatch):
    src = tmp_path / "old"
    pair(src)
    archive = tmp_path / "backup.zip"
    portable.export_bundle(src, archive)
    files = members(archive)
    name = "github_out/12/" + REPORT
    with zipfile.ZipFile(archive, "a") as z, pytest.warns(UserWarning):
        z.writestr(name, b"extra")
    with pytest.raises(ValueError, match="duplicate"):
        portable.import_bundle(tmp_path / "new", archive)
    manifest = json.loads(files["manifest.json"])
    manifest["version"] = 42
    files["manifest.json"] = json.dumps(manifest)
    repack(archive, files)
    with pytest.raises(ValueError, match="version"):
        portable.import_bundle(tmp_path / "new", archive)
    manifest["version"] = 1
    del files[name]
    del manifest["files"][name]
    files["manifest.json"] = json.dumps(manifest)
    repack(archive, files)
    with pytest.raises(ValueError, match="incomplete"):
        portable.import_bundle(tmp_path / "new", archive)
    files[name] = b" " * 2_000_000
    digest = hashlib.sha256(files[name]).hexdigest()
    manifest["files"][name] = dict(size=len(files[name]), sha256=digest)
    files["manifest.json"] = json.dumps(manifest)
    repack(archive, files)
    monkeypatch.setattr(portable, "MAX_RATIO", 2)
    with pytest.raises(ValueError, match="compression"):
        portable.import_bundle(tmp_path / "new", archive)
