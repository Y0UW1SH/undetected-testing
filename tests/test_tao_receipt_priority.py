"""Fixed-browser-budget selection of same-subnet acquisition-cost evidence."""
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from seo import tao_collect, tao_ledger
from seo.sources import tao_extrinsic


def pair(block, owner, netuid=28, units=10):
    xid = f"{block}-0001"
    base = dict(extrinsic=xid, time="2026-01-01T00:00:00Z",
                alpha=units, tao=units / 2, netuid=netuid,
                evidence="transaction", owner=owner)
    return [dict(base, id=xid + "s", kind="sell", src=owner, dst=None,
                 event_index=1),
            dict(base, id=xid + "b", kind="buy", src=None, dst=owner,
                 event_index=2)]


def transfer(block, src, dst, units=10, **changes):
    xid = f"{block}-0001"
    e = dict(extrinsic=xid, time="2026-01-01T00:00:00Z", id=xid + "t",
             kind="stake_transfer", src=src, dst=dst, event_index=1,
             alpha=units, tao=units / 2, netuid=28, transfer_side="both",
             evidence="transaction")
    return dict(e, **changes)


def order(raw):
    normalized = tao_ledger.net_transfers(raw)
    missing = {e["extrinsic"] for e in normalized
               if e["kind"] == "stake_reallocation"}
    return tao_collect._receipt_order(normalized, missing, {"watched"})


def test_requires_corroborated_earlier_same_subnet_flow_with_two_hop_limit():
    raw = []
    for block, owner in enumerate(("third-hop", "grandparent", "parent",
                                   "one-sided", "inferred", "wrong-dest",
                                   "native-tao", "late-parent"), 1):
        raw.extend(pair(block, owner))
    raw.extend(pair(9, "parent", netuid=11))
    raw.extend([
        transfer(20, "third-hop", "grandparent"),
        transfer(21, "grandparent", "parent"),
        transfer(22, "parent", "watched"),
        transfer(23, "late-parent", "parent"),
        transfer(24, "one-sided", "watched", transfer_side="out"),
        transfer(25, "inferred", "watched", evidence="snapshot_match"),
        transfer(26, "wrong-dest", "watched", dst_netuid=11),
        transfer(27, "native-tao", "watched", kind="tao_transfer",
                 netuid=None, alpha=None),
    ])
    raw.extend(pair(28, "parent"))  # After the only receipt into the watchlist
    for block in range(100, 135):
        raw.extend(pair(block, "watched"))

    selected = order(raw)
    assert len(selected) == len(set(selected)) == 24
    assert set(selected[:2]) == {"2-0001", "3-0001"}
    assert all(int(x.split("-")[0]) >= 100 for x in selected[2:])
    assert order(list(reversed(raw))) == selected


def test_prefers_quantity_that_could_reach_watchlist_and_bounds_hubs():
    raw = []
    for n in range(1, 10):
        raw.extend(pair(n, "donor" + str(n), units=1000))
        # Donor1 moved a large amount but delivered only one unit. The
        # strongest six distinct donors receive upstream consideration.
        raw.append(transfer(30 + n, "donor" + str(n), "watched", units=n))
    for block in range(11, 20):
        raw.extend(pair(block, "donor9", units=1000))
    for block in range(100, 135):
        raw.extend(pair(block, "watched", units=1))

    selected = order(raw)
    upstream = [int(x.split("-")[0]) for x in selected
                if int(x.split("-")[0]) < 100]
    assert {4, 5, 6, 7, 8} <= set(upstream)
    assert not {1, 2, 3}.intersection(upstream)
    assert len([n for n in upstream if n == 9 or 11 <= n < 20]) == 4
    assert len(selected) == 24


def test_unsuccessful_explicit_attempt_does_not_spend_another_auto_slot(
        monkeypatch):
    seen = []

    def fetch(session, xid):
        seen.append(xid)
        return dict(extrinsic_id=xid, status="unavailable")

    monkeypatch.setattr(tao_extrinsic, "fetch_evidence", fetch)
    with closing(sqlite3.connect(":memory:")) as db:
        store = SimpleNamespace(conn=db, followed=lambda: [])
        tao_collect._schema(db)
        tao_collect._save_events(db, [
            row for block in range(10, 40) for row in pair(block, "holder")])
        explicit = [dict(extrinsic_id="39-0001", status="unavailable")]
        tao_collect._read(store, object(), explicit=explicit)
    assert len(seen) == len(set(seen)) == 24
    assert seen[0] == "38-0001" and "39-0001" not in seen


def test_native_rounding_move_is_verified_before_collapsing():
    sell, buy = pair(30, 'holder')
    buy['alpha'] = sell['alpha'] - 1e-9
    proof = dict(extrinsic_id=sell['extrinsic'],
                 call='SubtensorModule.move_stake',
                 status='observed', success=True, signer='holder',
                 call_args=dict(origin_netuid=sell['netuid'],
                                destination_netuid=sell['netuid'],
                                alpha_amount=round(sell['alpha'] * 1e9)))
    with closing(sqlite3.connect(':memory:')) as db:
        store = SimpleNamespace(conn=db, followed=lambda: [])
        tao_collect._schema(db)
        tao_collect._save_events(db, [sell, buy])
        events, _ = tao_collect._read(store, explicit=[proof])
    assert len(events) == 1
    assert events[0]['kind'] == 'stake_reallocation'
    assert events[0]['evidence'] == 'native_move'
    assert events[0]['received_alpha'] == buy['alpha']
