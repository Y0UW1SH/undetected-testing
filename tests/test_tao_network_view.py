import json
import sqlite3
from contextlib import closing

from seo import tao_network_view as view


def test_merge_scans_count_pairs_transactions_and_basis(tmp_path):
    db = tmp_path / "census.sqlite"
    with closing(sqlite3.connect(db)) as d:
        d.execute("create table census_pin(id, height, complete, pages)")
        d.execute("insert into census_pin values(1,900,0,1)")
        d.execute("create table native_accounts(address primary key)")
        d.executemany("insert into native_accounts values(?)",
                      [("A",), ("NOT_AN_ENDPOINT",)])
        d.commit()
    receipt = dict(id="a", src="A", dst="B", kind="stake_transfer",
                   extrinsic="890-0002", netuid=8, evidence="transaction",
                   transfer_side="both", alpha=10, time="2026-01-01",
                   basis_status="partial", known_buy_price_tao=.3,
                   known_alpha=7, unknown_alpha=3, origins=[dict(
                       address="C", unit_buy_price_tao=.3, alpha=7,
                       extrinsic="880-0001")])
    one = dict(receipt, id="b", src="A", dst="D", transfer_side="out",
               extrinsic="891-0002")
    inferred = dict(receipt, id="c", src="B", dst="D",
                    evidence="snapshot_match", extrinsic=None, confidence=.86)
    report = tmp_path / "report.json"
    report.write_text(json.dumps(dict(checked_at="2026-01-02",
                                      transfers=[receipt, one, inferred],
                                      transfer_details=[receipt])))
    native = dict(id="finney:890:9", src="A", dst="B", kind="stake_transfer",
                  extrinsic="890-0002", netuid=8, alpha=10, dst_netuid=8)
    window = tmp_path / "window.json"
    window.write_text(json.dumps(dict(
        source="local-finney-window-v1", status="complete", first_block=888,
        last_block=900, last_scanned_block=900, transfer_edges=[native])))
    data = view.build(db, [report, report], window)
    assert data["census"]["accounts"] == 2 and not data["census"]["complete"]
    assert data["census"]["enrolled"] == ["A"]
    assert data["counts"] == dict(pairs=3, confirmed_pairs=1, transactions=2,
                                  endpoints=3, records=4)
    assert set(data["census"]["enrolled"]) <= {"A", "B", "D"}
    ab = next(p for p in data["pairs"] if p["dst"] == "B")
    assert len(ab["records"]) == 2
    assert ab["records"][0]["origins"][0]["extrinsic"] == "880-0001"
    assert {p["records"][0]["status"] for p in data["pairs"]} == {
        "Confirmed", "One-sided", "Inferred"}


def test_empty_census_unknown_peer_and_escaped_payload(tmp_path):
    report = tmp_path / "scan.json"
    report.write_text(json.dumps(dict(checked_at="today", transfers=[dict(
        id="x", kind="stake_transfer", src="A</script>", dst=None,
        extrinsic="2-0004", transfer_side="out", evidence="transaction")])))
    out = tmp_path / "network.html"
    assert view.main(["--report", str(report), "--out", str(out)]) == 0
    assert out.exists()
    html = out.read_text(encoding="utf8")
    assert "A</script>" not in html and "A\\u003c/script>" in html
    assert "connect-src 'none'" in html
    data = view.build(None, [report])
    assert data["counts"]["pairs"] == 0
    assert data["counts"]["endpoints"] == 1
    assert data["counts"]["transactions"] == 1
    assert data["census"] is None


def test_native_duplicate_receipts_use_event_index_and_keep_multiplicity(
        tmp_path):
    base = dict(kind="tao_transfer", src="A", dst="B", tao=2,
                evidence="transaction", extrinsic="3-0001", netuid=None)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    for path, owner in [(a, "A"), (b, "B")]:
        path.write_text(json.dumps(dict(checked_at="2026-01-01", transfers=[
            dict(base, id=f"{owner}-{i}", owner=owner, event_index=i)
            for i in (3, 4)])))
    data = view.build(None, [a, b])
    assert data["counts"] == dict(pairs=1, confirmed_pairs=1, transactions=1,
                                  endpoints=2, records=2)


def test_paired_receipt_replaces_earlier_single_leg_and_keeps_basis(tmp_path):
    a, b = tmp_path / "early.json", tmp_path / "late.json"
    base = dict(kind="stake_transfer", src="A", dst="B", netuid=8,
                alpha=2, evidence="transaction", extrinsic="9-0002")
    a.write_text(json.dumps(dict(checked_at="2026-01-01", transfers=[
        dict(base, id="credit", transfer_side="in")])))
    b.write_text(json.dumps(dict(checked_at="2026-01-02", transfers=[
        dict(base, id="debit", transfer_side="both",
             paired_event_id="credit")], transfer_details=[
                 dict(base, id="debit", transfer_side="both",
                      inherited_buy_price_tao=.125)])))
    data = view.build(None, [b, a])
    assert data["counts"]["records"] == 1
    receipt, = data["pairs"][0]["records"]
    assert receipt["status"] == "Confirmed"
    assert receipt["inherited_buy_price_tao"] == .125
