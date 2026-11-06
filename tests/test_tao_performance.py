"""Performance reporting with incomplete history and mixed cost basis."""
from seo import storage
from seo.models import WalletSnapshot
from seo.tao_ledger import portfolio
from tests.test_tao_ledger import day, event


def record(tmp_path, events, prices, stamp, coverage, *, snapshot_at=None,
           growth_events=(), **options):
    from seo import tao_performance as perf

    store = storage.Storage(str(tmp_path / "wallets.sqlite"))
    store.save([WalletSnapshot(address=a, snapshot_at=snapshot_at or stamp,
                               positions_complete=True, balance_total=30)
                for a, c in coverage.items() if c["account"] == "complete"],
               stamp=stamp)
    subnets = {}
    positions, totals = portfolio(events, [], subnet_stats=subnets,
                                  growth_events=growth_events)
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


def test_unsold_holders_rank_by_return_and_proven_cost(tmp_path):
    from seo import tao_performance as perf

    events = [event(1, "buy", None, "A", 2, 1),
              event(2, "buy", None, "B", 50, 40),
              event(1, "buy", None, "C", 20, 10, id="c-buy"),
              event(2, "stake_transfer", None, "C", 2, id="c-in"),
              event(3, "stake_transfer", None, "D", 2, id="d-in")]
    r = record(tmp_path, events, [price(8, 1, 4)], day(4),
               {a: COMPLETE for a in "ABCD"})
    assert r["rankings"]["profit"] == []
    assert r["rankings"]["open"] == ["A", "B"]
    assert r["rankings"]["open_partial"] == ["C"]
    w = {w["address"]: w for w in r["wallets"]}
    assert w["A"]["open"]["return_pct"] == 100
    assert w["A"]["open"]["days_since_addition"] == 3
    assert w["A"]["priced_sales"] == 0
    assert w["C"]["open"]["return_pct"] is None
    assert w["C"]["open"]["known_pnl_tao"] == 10
    assert w["C"]["open"]["known_return_pct"] == 100
    assert w["C"]["open"]["known_value_fraction"] == 20 / 22
    assert w["D"]["open"]["known_pnl_tao"] is None
    assert perf.follow_candidates(r, 4) == ["A", "B"]


def test_open_ranks_require_current_prices_inventory_and_no_trading_loss(
        tmp_path):
    events = [event(1, "buy", None, "A", 10, 5),
              event(2, "sell", "A", None, 9, .5)]
    r = record(tmp_path, events, [price(8, 2, 3)], day(3), {"A": COMPLETE})
    w = r["wallets"][0]
    assert abs(w["open"]["known_pnl_tao"] - 1.5) < 1e-12
    assert abs(w["gross_total_pnl_tao"] + 2.5) < 1e-12
    assert not r["rankings"]["open"]
    stale = record(tmp_path, events, [price(8, 2, 3)], day(4),
                   {"A": COMPLETE})
    assert stale["wallets"][0]["open"]["known_pnl_tao"] is None
    assert not stale["rankings"]["open"]
    assert not stale["rankings"]["open_partial"]


def test_partial_realized_research_is_separate_from_confirmed_winners(
        tmp_path):
    from seo.tao_performance import follow_candidates

    events = [event(1, "buy", None, "A", 2, 1),
              event(2, "stake_transfer", None, "A", 2),
              event(3, "sell", "A", None, 2, 2),
              event(4, "sell", "A", None, 2, 2)]
    r = record(tmp_path, events, [price(8, 1., 5)], day(5), {"A": COMPLETE})
    assert r["rankings"]["profit_partial"] == ["A"]
    assert not r["rankings"]["profit"]
    assert follow_candidates(r, 4) == []
    w = r["wallets"][0]
    assert w["known_realized_tao"] == 1.
    assert w["known_sale_value_fraction"] == .5


def test_timing_requires_distinct_winning_days_and_mature_quote_coverage(
        tmp_path):
    events = [event(1, "buy", None, "A", 1, 1, id="first"),
              event(1, "buy", None, "A", 1, 1, id="second"),
              event(2, "buy", None, "A", 1, 1, id="flat")]
    quotes = [price(8, 1.2, 2), price(8, 1., 3)]
    r = record(tmp_path, events, quotes, day(4), {"A": COMPLETE})
    t = r["wallets"][0]["timing"]["buy"]
    assert t["mature_trades"] == t["evaluated"] == 3
    assert (t["trade_days"], t["favorable_days"]) == (2, 1)
    assert not r["rankings"]["buy"]
    extra = [event(n, "buy", None, "A", 1, 1, id="extra-%d" % n)
             for n in (5, 6, 7, 8, 10, 11)]
    r = record(tmp_path, events + extra, [price(8, 1.2, 3), quotes[0]],
               day(10), {"A": COMPLETE})
    t = r["wallets"][0]["timing"]["buy"]
    assert t["mature_trades"] == 7 and t["evaluated"] == 3
    assert t["favorable_days"] == 2 and t["quote_coverage"] == 3 / 7
    assert not r["rankings"]["buy"]
    assert t["examples"][0]["trade_at"] == day(2)
    assert t["examples"][0]["quote_at"] == day(3)
    assert t["examples"][0]["quote_tao"] == 1.2


def test_stale_snapshot_and_native_moves_do_not_pass_screens(tmp_path):
    events = [event(1, "buy", None, "A", 2, 1),
              event(2, "buy", None, "A", 2, 1),
              event(3, "buy", None, "A", 2, 1),
              dict(event(5, "stake_transfer", "A", "A", 2),
                   evidence="native_move"),
              dict(event(5, "stake_transfer", "A", "A", 2, id="move2"),
                   evidence="matching_trade_legs")]
    prices = [price(8, 1., n) for n in (2, 3, 4, 6)]
    r = record(tmp_path, events, prices, day(6), {"A": COMPLETE},
               max_activity_24h=1, snapshot_at=day(2))
    w = r["wallets"][0]
    assert w["activity_24h"]["reallocations"] == 2
    assert sum(w["activity_24h"].values()) == 2
    assert "snapshot_stale" in w["evidence_gaps"]
    assert "activity_above_limit" in w["screens"]
    assert w["open"]["known_pnl_tao"] is None
    assert not r["rankings"]["buy"] and not r["rankings"]["open"]
