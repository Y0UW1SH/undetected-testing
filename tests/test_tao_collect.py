from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from seo import storage, tao_collect
from seo.models import WalletSnapshot


A = "5" + "A" * 47
B = "5" + "B" * 47
STAMP = "2026-11-06T00:00:00Z"


def event(number, address=A, tab="transactions"):
    return dict(id=json.dumps([tab, f"{number}-1", address, str(number)],
                              separators=(",", ":")),
                owner=address, kind="buy" if tab == "transactions" else
                "tao_transfer", src=None, dst=address, netuid=24, alpha=1,
                tao=1, evidence="transaction", extrinsic=f"{number}-1",
                time=f"2026-11-0{number}T00:00:00+00:00")


def state_for(frontier, tab="transactions", complete=False):
    return dict(version=1, address=A, tab=tab, frontier_page=frontier,
                anchor_ids=[event(frontier)["id"]] if frontier else [],
                head_anchor_ids=[event(1)["id"]], complete=complete,
                page_size=1, total_items=6, total_pages=6)


def test_history_checkpoint_and_event_rows_commit_together(tmp_path):
    with sqlite3.connect(tmp_path / "state.sqlite") as db:
        tao_collect._schema(db)
        initial = state_for(1)
        tao_collect._save_history(db, A, "transactions", [event(1)],
                                  {"checkpoint": initial}, STAMP)
        db.execute("CREATE TRIGGER fail_checkpoint BEFORE UPDATE ON "
                   "wallet_history_progress BEGIN "
                   "SELECT RAISE(ABORT,'checkpoint failure'); END")
        db.commit()
        with pytest.raises(sqlite3.Error):
            tao_collect._save_history(db, A, "transactions", [event(2)],
                                      {"checkpoint": state_for(2)}, STAMP)
        state, known = tao_collect._history_state(db, A, "transactions")
        assert state == initial
        assert known == {event(1)["id"]}
        count, = db.execute("SELECT COUNT(*) FROM wallet_events").fetchone()
        assert count == 1


def test_existing_normalized_history_is_scoped_by_wallet_and_tab(tmp_path):
    with sqlite3.connect(tmp_path / "state.sqlite") as db:
        db.execute("CREATE TABLE wallet_events(id TEXT PRIMARY KEY,"
                   "payload TEXT)")
        rows = [event(1), event(2, B), event(3, A, "transfers")]
        rows.append(dict(event(4), id="native-receipt"))
        db.executemany("INSERT INTO wallet_events VALUES (?,?)",
                       [(e["id"], json.dumps(e)) for e in rows])
        db.commit()
        tao_collect._schema(db)
        state, known = tao_collect._history_state(db, A, "transactions")
        assert state is None and known == {event(1)["id"]}
        _, cash = tao_collect._history_state(db, A, "transfers")
        assert cash == {event(3, A, "transfers")["id"]}


def test_bounded_inspections_resume_and_report_new_vs_cumulative_rows(
        tmp_path, monkeypatch):
    batches = [[event(1), event(2)], [event(1), event(3), event(4)],
               [event(1), event(5), event(6)]]
    run = 0

    def history(session, address, tab, pages, *, window, checkpoint,
                known_ids, progress):
        assert pages == 2 and address == A
        if tab == "transfers":
            progress.update(source_total=0, captured_this_run=0, cumulative=0,
                            remaining=0, status="complete", pages_read=1,
                            checkpoint=dict(version=1, address=A, tab=tab,
                                            frontier_page=1, complete=True),
                            completion_tier="observed_source")
            return [], "complete"
        previous = None if run == 0 else state_for(run * 2)
        assert checkpoint == previous
        expected = {event(i)["id"] for i in range(1, run * 2 + 1)}
        assert set(known_ids) == expected
        status = "complete" if run == 2 else "partial"
        state = state_for((run + 1) * 2, complete=run == 2)
        progress.update(source_total=6, captured_this_run=len(batches[run]),
                        cumulative=(run + 1) * 2, new_rows=2,
                        remaining=4 - run * 2, status=status, pages_read=3,
                        checkpoint=state, head_since=STAMP,
                        completion_tier="observed_source")
        window["since"] = "2020-01-01T00:00:00Z"
        return deepcopy(batches[run]), status

    monkeypatch.setattr(tao_collect.tao_history, "fetch_history", history)
    monkeypatch.setattr(tao_collect.site, "fetch_account", lambda *args: (
        WalletSnapshot(address=A, balance_total=5, positions_complete=True,
                       snapshot_at=STAMP), []))
    args = SimpleNamespace(max_balance_tao=1000, history_pages=2)
    session = SimpleNamespace(pause=lambda: None)
    path = str(tmp_path / "state.sqlite")
    for run in range(3):
        store = storage.Storage(path)
        try:
            tao_collect._schema(store.conn)
            coverage = {}
            _, rows = tao_collect._inspect(args, session, store, A, None,
                                           STAMP, coverage)
            assert len(rows) == len(batches[run])
            assert coverage["transactions_rows"] == (run + 1) * 2
            assert coverage["transactions_rows_this_run"] == len(batches[run])
            assert coverage["transactions_rows_added"] == 2
            assert coverage["transactions_rows_cumulative"] == (run + 1) * 2
            assert coverage["transactions_since"] == STAMP
            assert coverage["transactions"] == (
                "complete" if run == 2 else "partial")
            assert coverage["transactions_history"]["source_total"] == 6
            assert coverage["transactions_history"]["remaining"] == 4 - run * 2
            assert "checkpoint" not in coverage["transactions_history"]
        finally:
            store.close()


def test_real_pager_and_reopened_store_extend_history_beyond_fifty_rows(
        tmp_path, monkeypatch):
    from tests.test_tao_wallet_history import Table, WALLET

    class AccountTabs(Table):
        def __init__(self):
            super().__init__(range(119, -1, -1))
            self.transactions = self.rows

        def goto(self, url, check_google=False):
            self.rows = [] if url.endswith("/transfers") else self.transactions
            super().goto(url, check_google)

    monkeypatch.setattr(tao_collect.site, "fetch_account", lambda *args: (
        WalletSnapshot(address=WALLET, balance_total=5,
                       positions_complete=True, snapshot_at=STAMP), []))
    args = SimpleNamespace(max_balance_tao=1000, history_pages=5)
    path = str(tmp_path / "resume.sqlite")
    totals, states, added = [], [], []
    for _ in range(3):
        store = storage.Storage(path)
        try:
            tao_collect._schema(store.conn)
            coverage = {}
            tao_collect._inspect(args, AccountTabs(), store, WALLET, None,
                                 STAMP, coverage)
            totals.append(coverage["transactions_rows_cumulative"])
            added.append(coverage["transactions_rows_added"])
            states.append(coverage["transactions"])
            assert coverage["transactions_history"]["source_total"] == 120
            assert coverage["transactions_history"]["pages_read"] <= 8
            assert coverage["transfers"] == "complete"
        finally:
            store.close()
    assert totals == [60, 110, 120]
    assert added == [60, 50, 10]
    assert states == ["partial", "partial", "complete"]


def test_balance_screen_preserves_cumulative_history_counts(
        tmp_path, monkeypatch):
    monkeypatch.setattr(tao_collect.site, "fetch_account", lambda *args: (
        WalletSnapshot(address=A, balance_total=10000,
                       positions_complete=True, snapshot_at=STAMP), []))

    def unexpected_fetch(*args, **kwargs):
        pytest.fail("A screened wallet fetched history")

    monkeypatch.setattr(tao_collect.tao_history, "fetch_history",
                        unexpected_fetch)
    store = storage.Storage(str(tmp_path / "screened.sqlite"))
    try:
        tao_collect._schema(store.conn)
        tao_collect._save_history(store.conn, A, "transactions", [event(1)],
                                  {"checkpoint": state_for(1)}, STAMP)
        coverage = {}
        tao_collect._inspect(SimpleNamespace(max_balance_tao=1000,
                                             history_pages=2),
                             SimpleNamespace(pause=lambda: None), store, A,
                             None, STAMP, coverage)
        assert coverage["transactions"] == "skipped"
        assert coverage["transactions_rows_cumulative"] == 1
        assert coverage["transactions_rows_this_run"] == 0
        assert coverage["transactions_rows_added"] == 0
        assert coverage["transactions_history"]["source_total"] is None
    finally:
        store.close()
