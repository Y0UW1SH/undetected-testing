import json
import sqlite3
from contextlib import closing

from seo import tao_directory_view as viewer
from seo.sources import tao_census


def checkpoint(path):
    with closing(sqlite3.connect(path)) as db:
        tao_census._schema(db)
        with db:
            db.execute("insert into census_state values(1,2,4,4,'2026-01-02')")
            db.execute("insert into census_pages(page,row_count,min_block,"
                       "max_block) values(1,2,100,100)")
            db.executemany("insert into census_accounts "
                           "(address,network,first_page,last_page,rank,"
                           "block_number,staked_alpha_as_tao_rao) "
                           "values(?,'finney',1,1,?,100,?)",
                           [("A", 1, 4_000_000_000), ("B", 2, None)])


def test_trade_evidence_not_holdings_and_pool_is_checked(tmp_path):
    db = tmp_path / "registry.sqlite"
    checkpoint(db)
    r = tmp_path / "scan.json"
    event = dict(id="x", kind="buy", dst="B", alpha=2, tao=.4, netuid=8,
                 evidence="transaction", extrinsic="100-0003", time="old")
    r.write_text(json.dumps(dict(
        checked_at="2026-01-02T01:00:00+00:00",
        events=[event, event, dict(event, id="nottrade", dst="A",
                                   kind="stake_transfer"),
                dict(event, id="other", dst="C", extrinsic="101-0002"),
                dict(event, id="screened", dst="D", extrinsic="102-0001")],
        coverage={k: {"account": "complete"} for k in "ABCD"},
        performance={"wallets": [
            {"address": "A", "current_inventory": True,
             "positions": [{"alpha": 2, "netuid": 3}]},
            {"address": "B", "ranks": {"buy": 1}, "priced_sales": 0},
            {"address": "C", "priced_sales": 2, "realized_tao": .7},
            {"address": "D", "screens": ["balance_above_limit"]}]})))
    out = tmp_path / "accounts.html"
    model = viewer.write_view(db, [r, r], out)
    assert model["census"]["captured"] == 2
    assert model["census"]["pages"] == 1 and model["census"]["total"] == 4
    rows = model["research"]
    assert rows["B"]["fills"] == 1 and rows["B"]["in_directory"]
    assert rows["B"]["ranks"]["buy"] == 1
    assert rows["C"]["fills"] == 1 and not rows["C"]["in_directory"]
    assert rows["A"]["subnets"] == [3] and not rows["A"]["fills"]
    assert rows["D"]["screens"] and "D" not in model["shortlist"]
    assert model["shortlist"] == ["B", "C"]
    assert "connect-src 'none'" in out.read_text("utf-8")


def test_reclassified_move_removes_stale_trade_and_empty_is_searchable(
        tmp_path):
    db = tmp_path / "registry.sqlite"
    checkpoint(db)
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    base = dict(id="x", src="B", kind="sell", netuid=7, alpha=5, tao=1,
                evidence="transaction", extrinsic="100-0003")
    reports = [(old, "01", "sell"), (new, "02", "stake_reallocation")]
    for f, date, kind in reports:
        f.write_text(json.dumps(dict(checked_at=f"2026-01-{date}T00:00:00Z",
                                     events=[dict(base, kind=kind)])))
    out = tmp_path / "accounts.html"
    assert viewer.write_view(db, [new, old], out)["shortlist"] == []
    assert viewer.main(["--census", str(db), "--out", str(out)]) == 0
    assert viewer.write_view(db, [], out)["census"]["positive_alpha"] == 1


def test_unmatched_summary_does_not_override_live_checkpoint(tmp_path):
    db = tmp_path / "registry.sqlite"
    checkpoint(db)
    summary = tmp_path / "account-census.json"
    stale = dict(checked_at="yesterday", status="complete_single_block",
                 total_reported=12, network_accounts=12)
    summary.write_text(json.dumps(stale))
    model = viewer.write_view(db, [], tmp_path / "accounts.html", summary)
    assert model["census"]["status"] == "partial"


def test_cache_latest_attempt_only_deduplicates_explicit_and_skips_invalid(
        tmp_path):
    cache = tmp_path / "cache"
    db = tmp_path / "registry.sqlite"
    checkpoint(db)

    def save(folder, stamp):
        path = cache / folder / "tao_collect.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(
            checked_at=stamp, following=[], performance={"wallets": []})))
        return path

    save("123/report", "2026-01-01T00:00:00Z")
    latest = save("123/artifacts/789/report", "2026-01-02T00:00:00Z")
    bad = save("234/report", "invalid date")
    bad.write_text("{")
    save("nonnumeric/report", "2026-01-03T00:00:00Z")
    out = tmp_path / "directory.html"
    m = viewer.write_view(db, [latest, latest], out, cache=cache)
    assert m["reports"] == ["2026-01-02T00:00:00Z"]
    assert not viewer.write_view(db, [], out, cache=tmp_path / "gone")[
        "reports"]


def test_traceable_pnl_and_screened_candidates_separate_unassessed_fills(
        tmp_path):
    db = tmp_path / "registry.sqlite"
    checkpoint(db)
    stamp = "2026-01-01T00:00:00+00:00"
    trade = dict(kind="buy", alpha=10, tao=2, netuid=8,
                 evidence="transaction", extrinsic="123-0002")
    profile = dict(address="A", current_inventory=True, balance_tao=10,
                   balance_fetched_at=stamp, activity_24h_complete=True,
                   screens=[], evidence_gaps=[], priced_sales=1,
                   unpriced_sales=2, known_realized_tao=1.3,
                   unresolved_sale_proceeds_tao=4.2,
                   open=dict(known_pnl_tao=-.7, known_value_fraction=.58))
    report = tmp_path / "scan.json"
    gap = dict(profile, address="C", current_inventory=False,
               activity_24h_complete=False)
    report.write_text(json.dumps(dict(
        checked_at=stamp, events=[dict(trade, id=a, dst=a) for a in "ABC"],
        coverage={a: {"account": "complete"} for a in "AC"},
        performance=dict(settings=dict(max_balance_tao=100,
                                       max_activity_24h=20),
                         wallets=[profile, gap]))))
    model = viewer.write_view(db, [report], tmp_path / "view.html")
    assert model["candidates"] == ["A"] and model["leads"] == ["B", "C"]
    assert model["research"]["A"]["known_open"] == -.7
    assert model["research"]["A"]["known_value_fraction"] == .58
    assert model["research"]["A"]["known_realized"] == 1.3
    assert model["research"]["A"]["unresolved_proceeds"] == 4.2
    assert model["research"]["C"]["known_open"] is None
    assert model["research"]["B"]["known_value_fraction"] is None


def test_inventory_and_timing_use_latest_inspected_wallet(tmp_path):
    samples = dict(evaluated=9, mature_trades=18, favorable=9,
                   trade_days=2, quote_coverage=.5)
    paths = []
    for name, stamp, inspected, profile in [
        ("old", "2026-01-01T00:00:00Z", True,
         dict(current_inventory=True, balance_tao=50,
              positions=[dict(netuid=24, alpha=3)],
              timing={"buy": dict(samples, evaluated=3)})),
        ("new", "2026-01-02T00:00:00Z", True,
         dict(current_inventory=True, balance_tao=.01,
              balance_fetched_at="2026-01-02T00:00:00Z", positions=[],
              timing={"buy": samples}, ranks={"buy": 1})),
        ("uninspected", "2026-01-03T00:00:00Z", False,
         dict(current_inventory=False, balance_tao=None, positions=[],
              timing={"buy": dict(samples, evaluated=0)})),
    ]:
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(dict(
            checked_at=stamp,
            coverage={"A": {"account": "complete"}} if inspected else {},
            performance={"wallets": [dict(profile, address="A")]})))
        paths.append(path)
    research, _ = viewer._research(paths, set())
    a = research["A"]
    assert a["stamp"] == "2026-01-02T00:00:00Z"
    assert a["current_inventory"] is True and a["held"] == []
    assert a["balance_tao"] == .01
    assert a["balance_fetched_at"] == a["stamp"]
    assert a["timing"]["buy"] == samples
    uninspected = viewer._new()
    assert uninspected["current_inventory"] is False
    assert uninspected["balance_tao"] is None
    assert uninspected["timing"] == {}
