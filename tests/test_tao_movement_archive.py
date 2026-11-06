"""Passive movement recording from the watcher's existing finalized reads."""
from contextlib import closing
import json
import sqlite3

import pytest

from seo import tao_local_watch as watch
from seo import tao_movement_archive as archive
from tests.test_tao_local_watch import A, B, HOT, Chain, evt

C = "5" + "C" * 47


class RecordedChain(Chain):
    head = 53

    def __init__(self):
        super().__init__()
        self.calls = []

    def get_block_hash(self, n):
        self.calls.append(("hash", n))
        return super().get_block_hash(n)

    def get_events(self, block_hash):
        self.calls.append(("events", block_hash))
        n = self.get_block_number(block_hash)
        if n == 52:
            return [
                evt("StakeAdded", [C, HOT, 100, 200, 8]),
                evt("ExtrinsicSuccess", None, "System"),
                evt("Transfer", [C, B, 123], "Balances", ex=4),
                evt("ExtrinsicFailed", {}, "System", ex=4),
                evt("Transfer", [C, B, 123], "Balances", ex=5),
                evt("ExtrinsicSuccess", None, "System", ex=6),
                evt("OtherEvent", [], "OtherModule", ex=6),
            ]
        return super().get_events(block_hash)

    def query(self, module, name, *, block_hash):
        self.calls.append(("timestamp", block_hash))
        return super().query(module, name, block_hash=block_hash)


def test_passive_capture_matches_rpc_trace_and_leaves_feed_narrow(tmp_path):
    db = tmp_path / "archive.sqlite"
    old = {"last_scanned_block": 49}
    without = RecordedChain()
    baseline = watch.poll(without, old, [A])
    with_archive = RecordedChain()
    with closing(archive.MovementArchive(db)) as ar:
        feed = watch.poll(with_archive, old, [A], archive=ar)
        # Replayed after a hypothetical crash before watched-feed persistence.
        replay = watch.poll(RecordedChain(), old, [A], archive=ar)
    assert without.calls == with_archive.calls
    assert feed["events"] == baseline["events"] == replay["events"]
    assert feed["last_scanned_block"] == 53
    result = archive.report(db, wallet=A)
    assert result["blocks"] == 4 and result["movements"] == 3
    assert result["active_wallet_count"] == 3
    assert result["coverage"][0]["first_block"] == 50
    assert result["coverage"][0]["last_block"] == 53
    assert len(result["coverage"]) == 1
    assert result["by_kind"] == dict(tao_transfer=1, stake_transfer=1,
                                     stake_increase=1)
    assert result["transfer_peers"][0]["address"] == B
    assert result["transfer_peers"][0]["movements"] == 2
    assert result["active_wallets"][0]["address"] == C
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT chain_time FROM blocks WHERE height=52"
                            ).fetchone() == (None,)
        assert conn.execute("SELECT n_events FROM blocks WHERE height=53"
                            ).fetchone() == (0,)


def test_archive_write_failure_rolls_back_and_preserves_watch_checkpoint(
        tmp_path, monkeypatch):
    db = tmp_path / "archive.sqlite"
    with closing(archive.MovementArchive(db)) as ar:
        ar.db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON movements "
                      "WHEN NEW.height=52 BEGIN "
                      "SELECT RAISE(ABORT, 'test-only-private-path'); END")
        clients = []

        def connect(*_):
            clients.append(RecordedChain())
            return clients[-1]

        monkeypatch.setattr(watch, "_connect", connect)
        notices = []
        old = {"last_scanned_block": 49}
        feed = watch._poll_cycle("test", old, [A, C], 10, 180, notices,
                                 archive=ar)
        assert len(clients) == 1
        assert feed["status"] == "archive_error"
        assert feed["last_scanned_block"] == 51
        assert "test-only-private-path" not in json.dumps(feed)
        assert all(e["block_number"] <= 51 for e in notices)
        assert archive.report(db)["blocks"] == 2
        ar.db.execute("DROP TRIGGER fail_insert")
        feed = watch._poll_cycle("test", feed, [A, C], 10, 180, notices,
                                 archive=ar)
    assert feed["status"] == "current" and feed["last_scanned_block"] == 53
    assert archive.report(db)["blocks"] == 4
    assert len(notices) == len({r["id"] for r in notices})
    assert any(e["block_number"] == 52 for e in notices)


def test_archive_reports_gaps_prunes_whole_blocks_and_checks_hash(tmp_path):
    db = tmp_path / "archive.sqlite"
    with closing(archive.MovementArchive(db, keep_blocks=5,
                                         keep_events=2)) as ar:
        transfer = evt("Transfer", [A, B, 30], "Balances")
        row, = watch.block_events(10, [transfer], None)
        for height in (10, 11, 13):
            event = dict(row, block_number=height, extrinsic=f"{height}-0002")
            ar.record("finney", height, str(height), "seen", None, [event])
        result = archive.report(db, wallet=A)
        ranges = [(r["first_block"], r["last_block"])
                  for r in result["coverage"]]
        assert ranges == [(11, 11), (13, 13)]
        assert result["transfer_peers"][0]["movements"] == 2
        ar.record("finney", 10, "10", "later", None, [row])
        assert archive.report(db)["coverage"] == result["coverage"]
        with pytest.raises(ValueError):
            ar.record("other-chain", 14, "14", "seen", None, [])
        with pytest.raises(ValueError):
            ar.record("finney", 13, "unexpected", "seen", None, [])
        ar.record("finney", 20, "20", "seen", None, [])
        result = archive.report(db)
        assert result["blocks"] == 1 and result["movements"] == 0
        assert result["coverage"][0]["first_block"] == 20


def test_archive_cli_reads_offline_and_refuses_foreign_database(tmp_path,
                                                                capsys):
    path = tmp_path / "passive.sqlite"
    with closing(archive.MovementArchive(path)) as ar:
        ar.record("genesis", 10, "a", "now", None, [])
    assert archive.main(["--db", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["blocks"] == 1
    foreign = tmp_path / "existing.sqlite"
    with sqlite3.connect(foreign) as conn:
        conn.execute("CREATE TABLE important (a TEXT)")
    with pytest.raises(ValueError):
        archive.MovementArchive(foreign)
    with sqlite3.connect(foreign) as conn:
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == [
            ("important",)]


def test_watch_cli_archive_opt_in_keeps_feed_portable(tmp_path, monkeypatch):
    from seo.tao_portable import _feed

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(watch, "_endpoint", lambda prompt: "test")
    monkeypatch.setattr(watch, "_connect", lambda *args: Chain())
    assert watch.main(["--once", "--address", A]) == 0
    assert not archive.DEFAULT_PATH.exists()
    assert watch.main(["--once", "--address", A, "--out", "feed.json",
                       "--movement-archive"]) == 0
    data = archive.report()
    assert data["blocks"] == 1 and data["movements"] == 2
    assert data["by_kind"] == {"stake_transfer": 1, "tao_transfer": 1}
    _feed(tmp_path / "feed.json")
    assert watch.main(["--once", "--movement-archive", "feed.json",
                       "--out", "feed.json"]) == 2
