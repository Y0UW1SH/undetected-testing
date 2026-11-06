"""Explicit native receipts use the automatic verifier and durable cache."""
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from seo import tao_collect
from seo.sources import tao_extrinsic


def _pair(xid):
    base = dict(extrinsic=xid, time="2026-01-01T00:00:00+00:00",
                alpha=1, tao=.1, netuid=28, evidence="transaction")
    return [dict(base, id=xid + "-out", kind="sell", src="holder", dst=None,
                 event_index=1),
            dict(base, id=xid + "-in", kind="buy", src=None, dst="holder",
                 event_index=2)]


def _proof(xid, **extra):
    return dict(dict(extrinsic_id=xid, status="observed", success=True,
                     signer="holder", call="SubtensorModule.move_stake",
                     call_args=dict(alpha_amount=10**9, origin_netuid=28,
                                    destination_netuid=28)), **extra)


def test_requested_receipt_outside_auto_cap_cached_and_verified_on_replay(
        monkeypatch):
    requested = "100-0001"
    auto = []

    def fetch(session, xid):
        auto.append(xid)
        return dict(extrinsic_id=xid, status="unavailable")

    monkeypatch.setattr(tao_extrinsic, "fetch_evidence", fetch)
    with closing(sqlite3.connect(":memory:")) as db:
        store = SimpleNamespace(conn=db, followed=lambda: [])
        tao_collect._schema(db)
        tao_collect._save_events(db, [row for n in range(100, 130)
                                      for row in _pair(f"{n}-0001")])
        detail = _proof(requested)
        events, _ = tao_collect._read(store, object(), [detail])
        assert len(auto) == 24 and requested not in auto
        assert next(r for r in events if r["extrinsic"] == requested)[
            "evidence"] == "native_move"
        assert json.loads(db.execute("SELECT payload FROM wallet_move_evidence"
                                     " WHERE extrinsic=?", (requested,))
                          .fetchone()[0]) == detail
        # A later failed page cannot replace an earlier successful observation.
        events, _ = tao_collect._read(store, explicit=[
            dict(extrinsic_id=requested, status="unavailable")])
        assert next(r for r in events if r["extrinsic"] == requested)[
            "evidence"] == "native_move"


def test_explicit_call_must_match_even_if_auxiliary_event_unavailable():
    with closing(sqlite3.connect(":memory:")) as db:
        store = SimpleNamespace(conn=db)
        tao_collect._schema(db)
        partial = _proof("101-0001", status="partial",
                         requested_block_events=[{"status": "unavailable"}])
        entries = [_proof("100-0001", signer="different"), partial]
        tao_collect._save_events(db, [r for d in entries
                                      for r in _pair(d["extrinsic_id"])])
        rows, _ = tao_collect._read(store, explicit=entries)
        assert [r["evidence"] for r in rows] == ["matching_trade_legs",
                                                 "native_move"]
