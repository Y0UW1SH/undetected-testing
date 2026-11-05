"""Regressions for interpreting a wallet's observed, partial trade history."""
from seo.tao_ledger import inferred_links, net_transfers, portfolio, timing


def day(n):
    return "2026-10-%02dT00:00:00+00:00" % n


def event(n, kind, src, dst, alpha, tao=None, **fields):
    return dict(time=day(n), kind=kind, src=src, dst=dst, netuid=8,
                alpha=alpha, tao=tao, evidence="transaction",
                **{"id": str(n), **fields})


def checkpoint(n, addr, qty, swap=None):
    return dict(batch=str(n), address=addr, time=day(n),
                units={8: qty} if qty else {}, swap=swap)


def test_coldkey_moves_twice_even_when_receivers_are_new():
    checkpoints = [checkpoint(2, "a", 10), checkpoint(3, "a", 0, "b"),
                   checkpoint(3, "b", 10), checkpoint(4, "b", 0, "c"),
                   checkpoint(4, "c", 10)]
    trades = [event(1, "buy", None, "a", 10, 2),
              event(5, "sell", "c", None, 2, 0.7)]
    links = inferred_links(checkpoints, trades)
    assert [(e["src"], e["dst"], e["alpha"], e["evidence"])
            for e in links] == [("a", "b", 10, "coldkey_swap"),
                                ("b", "c", 10, "coldkey_swap")]
    positions, stats = portfolio(trades + links, checkpoints)
    assert positions["c", 8]["buy_price_tao"] == 0.2
    assert stats["c"]["realized_tao"] == 0.3


def test_ignore_coincidences_and_explicit_events():
    cp = [checkpoint(1, "a", 8), checkpoint(2, "a", 2),
          checkpoint(1, "b", 1), checkpoint(2, "b", 7)]
    link, = inferred_links(cp, [])
    assert link["alpha"] == 6
    assert link["evidence"] == "snapshot_match" and 0 < link["confidence"] < 1
    transfers = [event(2, "stake_transfer", "a", "b", 6)]
    assert inferred_links(cp, transfers) == []
    assert inferred_links(cp + [checkpoint(1, "c", 0),
                                checkpoint(2, "c", 6)], []) == []
    assert inferred_links(cp[0:2] + [checkpoint(2, "new", 6)], []) == []
    assert inferred_links(cp, [event(2, "buy", None, "b", 6, 3)]) == []


def test_probability_does_not_turn_estimated_profit_into_a_win():
    cp = [checkpoint(2, "a", 8), checkpoint(3, "a", 2),
          checkpoint(2, "b", 0), checkpoint(3, "b", 6)]
    trades = [event(1, "buy", None, "a", 8, 2),
              event(4, "stake_transfer", "b", "c", 6),
              event(5, "sell", "c", None, 2, 1)]
    estimated = inferred_links(cp, trades)
    positions, stats = portfolio(trades + estimated, cp)
    assert positions["c", 8]["probable_alpha"] == 4
    assert positions["c", 8]["buy_price_tao"] == 0.25
    assert stats["c"]["unpriced_sales"] == 1
    assert stats["c"]["priced_sales"] == stats["c"]["wins"] == 0


def test_preserve_known_fraction_of_mixed_basis():
    cp = [checkpoint(1, "a", 10), checkpoint(4, "b", 10)]
    trades = [event(2, "buy", None, "a", 10, 1),
              event(3, "stake_transfer", "a", "b", 10),
              event(5, "sell", "b", None, 2, 10)]
    positions, stats = portfolio(trades, cp)
    dest = positions["b", 8]
    assert dest["known_alpha"] == 4
    assert dest["buy_price_tao"] is None
    assert dest["known_cost_tao"] == 0.4
    assert stats["b"]["unpriced_sales"] == 1


def test_duplicate_observations_preserve_separate_events_in_one_extrinsic():
    tx = dict(extrinsic="123-4")
    a1 = event(1, "stake_transfer", "a", "b", 2, owner="a", id="a1", **tx)
    a2 = dict(a1, id="a2")
    b1 = dict(a1, id="b1", owner="b")
    b2 = dict(b1, id="b2")
    other = dict(a1, id="other", netuid=9)
    trades = [a1, a2, b1, b2, a1, b1, other]
    result = net_transfers(trades)
    assert sorted(e["netuid"] for e in result) == [8, 8, 9]
    assert net_transfers(trades[::-1]) == result


def test_timing_uses_first_future_quote_regardless_of_input_order():
    # The first eligible quote for the buy is 0.6 at precisely +24h.
    # The newest quote, 0.4 at +25h, would reverse the signal.
    buy = event(1, "buy", None, "a", 10, 5)
    sell = event(4, "sell", "b", None, 4, 2)
    quotes = [dict(netuid=8, price_tao=0.4,
                   fetched_at=day(2).replace("00:00:00", "01:00:00")),
              dict(netuid=8, price_tao=0.6, fetched_at=day(2)),
              dict(netuid=8, price_tao=0.1, fetched_at=day(6))]
    result = timing([buy, sell], quotes)
    assert result["a"] == {"favorable": 1, "evaluated": 1}
    assert "b" not in result


def test_non_overlapping_snapshot_intervals_cannot_explain_a_transfer():
    cp = [checkpoint(1, "a", 8), checkpoint(2, "a", 2),
          dict(checkpoint(1, "b", 0), time=day(3)),
          dict(checkpoint(2, "b", 6), time=day(4))]
    assert inferred_links(cp, []) == []


def test_small_unknown_fraction_of_large_position_cannot_score_win():
    cp = [checkpoint(1, "a", 0.001)]
    events = [event(2, "buy", None, "a", 10000000, 1000000),
              event(3, "sell", "a", None, 1000, 200)]
    positions, stats = portfolio(events, cp)
    assert positions["a", 8]["buy_price_tao"] is None
    assert stats["a"]["unpriced_sales"] == 1
    assert stats["a"]["wins"] == 0


def test_cross_subnet_legs_only_change_the_wallet_and_asset_observed():
    original = event(1, "buy", None, "a", 10, 2)
    sent = event(2, "stake_transfer", "a", "b", 6,
                 owner="a", extrinsic="333-9", id="sent")
    received = dict(sent, netuid=9, alpha=3, owner="b", id="received")
    sold = dict(event(3, "sell", "b", None, 1, 20), netuid=9)
    movements = net_transfers([original, sent, received, sold, sent, received])
    positions, stats = portfolio(movements, [])
    assert set(positions) == {("a", 8), ("b", 9)}
    assert positions["a", 8]["alpha"] == 4
    assert positions["b", 9]["alpha"] == 2
    assert positions["b", 9]["buy_price_tao"] is None
    assert stats["b"]["unpriced_sales"] == 1
    assert stats["b"]["wins"] == 0


def test_late_second_leg_enables_same_subnet_cost_without_phantom_balances():
    buy = event(1, "buy", None, "a", 10, 3)
    sent = event(2, "stake_transfer", "a", "b", 4,
                 owner="a", extrinsic="234-7", id="sent")
    received = dict(sent, owner="b", id="received")
    positions, _ = portfolio(net_transfers([buy, sent]), [])
    assert set(positions) == {("a", 8)}
    assert positions["a", 8]["alpha"] == 6
    positions, _ = portfolio(net_transfers([buy, received]), [])
    assert positions["a", 8]["alpha"] == 10
    assert positions["b", 8]["buy_price_tao"] is None
    both = net_transfers([buy, received, sent])
    assert len(both) == 2
    positions, _ = portfolio(both, [])
    assert positions["a", 8]["alpha"] == 6
    assert positions["b", 8]["buy_price_tao"] == 0.3


def test_missing_counterparty_cannot_attach_cost_to_another_wallet():
    buy = event(1, "buy", None, "a", 8, 2)
    sent = event(2, "stake_transfer", "a", None, 2,
                 owner="a", extrinsic="222-5", id="sent")
    received = dict(sent, owner="b", src=None, dst="b", id="received")
    positions, stats = portfolio(net_transfers([buy, sent, received]), [])
    assert positions["a", 8]["alpha"] == 6
    assert positions["b", 8]["alpha"] == 2
    assert positions["b", 8]["buy_price_tao"] is None
    assert not stats


def test_same_wallet_subnet_conversion_changes_only_observed_positions():
    buy = event(1, "buy", None, "a", 8, 2)
    sent = event(2, "stake_transfer", "a", "a", 2, owner="a",
                 transfer_side="out", extrinsic="222-5", id="sent")
    received = dict(sent, alpha=3, netuid=9, transfer_side="in", id="in")
    positions, _ = portfolio(net_transfers([buy, sent, received]), [])
    assert positions["a", 8]["alpha"] == 6
    assert positions["a", 9]["alpha"] == 3
    assert positions["a", 9]["buy_price_tao"] is None


def test_realized_breakdown_separates_subnets_from_mixed_wallet_result():
    trades = [event(1, "buy", None, "a", 10, 1),
              dict(event(2, "buy", None, "a", 2, 4), netuid=9),
              event(3, "sell", "a", None, 5, 1),
              dict(event(4, "sell", "a", None, 1, 1), netuid=9),
              dict(event(5, "sell", "a", None, 1, 20), netuid=10)]
    by_subnet = {}
    _, stats = portfolio(trades, [], subnet_stats=by_subnet)
    assert stats["a"]["realized_tao"] == -0.5
    assert by_subnet["a", 8] == dict(priced_sales=1, wins=1,
                                     unpriced_sales=0, realized_tao=0.5)
    assert by_subnet["a", 9]["realized_tao"] == -1
    assert by_subnet["a", 10]["priced_sales"] == 0
    assert by_subnet["a", 10]["unpriced_sales"] == 1


def test_observation_iteration_returns_each_trade_and_first_eligible_quote():
    from seo.tao_ledger import timing_observations
    buy = event(1, "buy", None, "a", 5, 1)
    sell = event(3, "sell", "a", None, 5, 2)
    quotes = [dict(netuid=8, price_tao=0.95, fetched_at=day(2)),
              dict(netuid=8, price_tao=0.15, fetched_at=day(4))]
    assert list(timing_observations([buy, sell], quotes)) == [
        (buy, 0.95), (sell, 0.15)]
