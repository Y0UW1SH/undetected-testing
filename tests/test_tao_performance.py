"""Performance reporting with incomplete history and mixed cost basis."""
from seo import storage
from seo.models import WalletSnapshot
from seo.tao_ledger import portfolio
from tests.test_tao_ledger import day, event


def record(tmp_path, events, prices, stamp, coverage, **options):
    from seo import tao_performance as perf

    store = storage.Storage(str(tmp_path / "wallets.sqlite"))
    store.save([WalletSnapshot(address=a, snapshot_at=stamp,
                               positions_complete=True, balance_total=30)
                for a, c in coverage.items() if c["account"] == "complete"],
               stamp=stamp)
    subnets = {}
    positions, totals = portfolio(events, [], subnet_stats=subnets)
    report = perf.record_run(store, stamp, positions, totals, subnets,
                             events, prices, coverage, **options)
    store.close()
    return report


def price(n, val, date):
    return dict(netuid=n, price_tao=val, fetched_at=day(date))


COMPLETE = dict(account="complete", transactions="complete",
                transfers="complete")


def test_run_history_and_marked_positions(tmp_path):
    from seo import tao_performance as perf

    events = [event(1, "buy", None, "A", 10, 5),
              event(2, "sell", "A", None, 4, 3),
              event(3, "sell", "A", None, 1, 1.5),
              event(3, "stake_transfer", "A", "B", 1, id="3b")]
    prices = [price(8, 0.55, 2), price(8, 1, 4)]
    report = record(tmp_path, events, prices, day(4),
                    {"A": COMPLETE, "B": COMPLETE})
    assert report["rankings"]["profit"] == ["A"]
    wallets = {w["address"]: w for w in report["wallets"]}
    a, b = wallets["A"]["positions"][0], wallets["B"]["positions"][0]
    assert (a["alpha"], a["buy_price_tao"], a["marked_value_tao"],
            a["gross_unrealized_tao"], a["realized_tao"]) == (4, .5, 4, 2, 2)
    assert b["buy_price_tao"] == .5
    assert wallets["A"]["transfer_links"][0]["dst"] == "B"
    assert wallets["B"]["transfer_links"][0]["src"] == "A"
    more = record(tmp_path, events, prices, day(5),
                  {"A": {**COMPLETE, "transactions": "partial"}})
    a = next(w for w in more["wallets"] if w["address"] == "A")
    assert a["positions"][0]["quote_at"] == day(4)
    assert a["positions"][0]["gross_unrealized_tao"] is None
    assert "transactions_partial" in a["evidence_gaps"]
    assert not more["rankings"]["profit"]
    store = storage.Storage(str(tmp_path / "wallets.sqlite"))
    assert perf.load_run(store.conn, day(4)) == report
    assert [r["ranks"]["profit"] for r in perf.history(store.conn, "A")] == [
        None, 1]
    assert perf.load_run(store.conn) == more
    store.close()


def test_estimated_and_unknown_basis_never_enter_confirmed_gains(tmp_path):
    movements = [event(1, "buy", None, "A", 10, 1),
                 dict(event(2, "stake_transfer", "A", "B", 2),
                      evidence="snapshot_match", confidence=.9),
                 event(2, "stake_transfer", None, "B", 1, id="2b")]
    report = record(tmp_path, movements, [price(8, 1, 4)], day(4),
                    {"A": COMPLETE, "B": COMPLETE})
    b = next(w for w in report["wallets"] if w["address"] == "B")
    pos = b["positions"][0]
    assert (pos["known_alpha"], pos["probable_alpha"], pos["unknown_alpha"],
            pos["marked_value_tao"]) == (0, 2, 1, 3)
    assert pos["probable_unrealized_tao"] == 1.8
    assert pos["gross_unrealized_tao"] is None
    assert pos["buy_price_tao"] is None
    assert b["rankable_profit"] is False


def test_separate_timing_rankings_and_activity_screen(tmp_path):
    events = [event(1, "buy", None, "A", 1, 1),
              event(2, "buy", None, "A", 1, 1),
              event(3, "buy", None, "A", 1, 1),
              event(2, "sell", "A", None, 1, 1, id="sell"),
              event(4, "tao_transfer", "A", "B", None, 3, id="native")]
    prices = [price(8, 1.2, 2), price(8, 1.07, 3), price(8, 1.01, 4)]
    r = record(tmp_path, events, prices, day(4), {"A": COMPLETE})
    assert r["rankings"]["buy"] == ["A"]
    assert r["rankings"]["sell"] == []
    w = next(w for w in r["wallets"] if w["address"] == "A")
    assert w["timing"]["buy"]["favorable"] == 2
    assert w["timing"]["buy"]["evaluated"] == 3
    assert w["timing"]["sell"]["evaluated"] == 1
    assert w["timing"]["sell"]["favorable"] == 0
    limited = record(tmp_path, events, prices, day(4), {"A": COMPLETE},
                     max_activity_24h=1, max_balance_tao=20)
    a = next(w for w in limited["wallets"] if w["address"] == "A")
    assert {"balance_above_limit", "activity_above_limit"} <= set(a["screens"])
    assert not limited["rankings"]["buy"]


def test_mark_of_confirmed_lot_when_other_alpha_has_no_basis(tmp_path):
    alpha = 200.
    entry = .004975320913
    events = [event(1, "buy", None, "A", alpha, alpha * entry),
              event(2, "stake_transfer", None, "A", 124.688)]
    r = record(tmp_path, events, [price(8, .006, 3)], day(3),
               {"A": COMPLETE})
    w = r["wallets"][0]
    p = w["positions"][0]
    assert abs(p["known_buy_price_tao"] - entry) < 1e-15
    assert abs(p["known_unrealized_tao"] - (.006 - entry) * alpha) < 1e-12
    assert abs(p["unknown_alpha"] - 124.688) < 1e-12
    assert p["buy_price_tao"] is w["gross_unrealized_tao"] is None
    assert p["quote_at"] == day(3)


def test_timing_allows_proven_recent_window_with_capped_history(tmp_path):
    trades = [event(n, "buy", None, "A", 1, 1.) for n in (1, 2, 3)]
    prices = [price(8, 1.1, n) for n in (2, 3, 4)]
    partial = {**COMPLETE, "transactions": "partial", "transfers": "partial",
               "transactions_since": day(1), "transfers_since": day(3)}
    good = record(tmp_path, trades, prices, day(4), {"A": partial})
    assert good["rankings"]["buy"] == ["A"]
    assert good["wallets"][0]["screens"] == []
    assert good["wallets"][0]["evidence_gaps"] == ["transactions_partial"]
    partial.pop("transactions_since")
    changed = record(tmp_path, trades, prices, day(4), {"A": partial})
    assert changed["rankings"]["buy"] == []
    assert changed["wallets"][0]["screens"] == []
    assert "activity_24h_incomplete" in changed["wallets"][0]["evidence_gaps"]
