"""Regressions for interpreting a wallet's observed, partial trade history."""
from seo.tao_ledger import inferred_links, net_transfers, portfolio, timing


def day(n):
    return "2026-10-%02dT00:00:00+00:00" % n


def test_transfer_details_trace_weighted_purchases_through_two_wallets():
    buys = [event(1, "buy", None, "a", 10, 2, extrinsic="1-1"),
            event(2, "buy", None, "a", 10, 4, extrinsic="2-1")]
    events = buys + [event(3, "stake_transfer", None, "a", 10),
                     event(4, "stake_transfer", "a", "b", 12),
                     event(5, "buy", None, "b", 4, 2, extrinsic="5-1"),
                     event(6, "stake_transfer", "b", "c", 8)]
    details = []
    positions, _ = portfolio(events, [], transfer_details=details)
    one, two = details[-2:]
    assert one["known_alpha"] == 8 and one["unknown_alpha"] == 4
    assert abs(one["known_buy_price_tao"] - .3) < 1e-12
    assert two["known_alpha"] == 6 and two["unknown_alpha"] == 2
    assert abs(two["known_cost_tao"] - 2.2) < 1e-12
    assert two["inherited_buy_price_tao"] is None
    assert two["basis_status"] == "partial"
    assert positions["c", 8]["known_alpha"] == two["known_alpha"]
    origins = {o["extrinsic"]: o for o in two["origins"]}
    assert {x: (o["address"], o["alpha"]) for x, o in origins.items()} == {
        "1-1": ("a", 2), "2-1": ("a", 2), "5-1": ("b", 2)}
    assert abs(sum(o["cost_tao"] for o in origins.values()) - 2.2) < 1e-12
    assert all(o["basis_status"] == "confirmed" for o in origins.values())


def test_transfer_details_separate_estimates_unpaired_and_native_tao():
    events = [event(1, "buy", None, "a", 10, 5, extrinsic="1-1"),
              dict(event(2, "stake_transfer", "a", "b", 4),
                   evidence="snapshot_match", confidence=.88),
              event(3, "stake_transfer", "b", "c", 2),
              event(4, "stake_transfer", "a", "x", 1, transfer_side="out"),
              event(5, "tao_transfer", "b", "c", None, 10)]
    details = []
    portfolio(events, [], transfer_details=details)
    assert details[0]["basis_status"] == "estimated"
    assert details[0]["confidence"] == .88
    for row in details[:2]:
        assert row["inherited_buy_price_tao"] == .5
        assert row["known_alpha"] == 0
        assert row["origins"][0]["basis_status"] == "estimated"
        assert row["origins"][0]["extrinsic"] == "1-1"
    assert details[2]["basis_status"] == "debit_only"
    assert details[2]["inherited_buy_price_tao"] is None
    assert details[3]["basis_status"] == "native_tao"
    assert details[3]["origins"] == []


def test_bounded_origin_sample_preserves_unsampled_cost_and_units():
    buys = [event(i, "buy", None, "a", 1, i, extrinsic=f"{i}-1")
            for i in range(1, 8)]
    moves = [event(8, "stake_transfer", "a", "b", 7),
             event(9, "stake_transfer", "b", "c", 3.5)]
    details = []
    portfolio(buys + moves, [], transfer_details=details)
    row = details[-1]
    assert len(row["origins"]) == 5
    assert row["known_alpha"] == 3.5 and row["inherited_buy_price_tao"] == 4
    assert row["other_origin_alpha"] == 1
    assert (sum(o["cost_tao"] for o in row["origins"])
            + row["other_origin_cost_tao"] == 14)


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
    sn8 = by_subnet["a", 8]
    assert (sn8["priced_sales"], sn8["wins"], sn8["unpriced_sales"],
            sn8["realized_tao"]) == (1, 1, 0, 0.5)
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


def test_sale_breakdown_attributes_only_the_known_fraction():
    purchases = [event(1, "buy", None, "a", 5, 2),
                 event(2, "stake_transfer", None, "a", 5),
                 event(3, "sell", "a", None, 4, 4, extrinsic="123-0001")]
    lots, by_subnet, details = [], {}, []
    _, stats = portfolio(purchases, lots, subnet_stats=by_subnet,
                         sale_details=details)
    sale, = details
    assert sale["extrinsic"] == "123-0001"
    assert sale["basis_status"] == "partially_priced"
    assert sale["known_alpha"] == sale["unknown_alpha"] == 2
    assert sale["known_cost_tao"] == 0.8
    assert sale["known_proceeds_tao"] == sale["unresolved_proceeds_tao"] == 2
    assert sale["known_realized_tao"] == 1.2
    a = stats["a"]
    assert a == by_subnet["a", 8]
    assert a["known_realized_tao"] == 1.2
    assert a["known_sale_cost_tao"] == 0.8
    assert a["unresolved_sale_proceeds_tao"] == 2
    assert a["partial_sales"] == a["unpriced_sales"] == 1
    assert a["realized_tao"] == a["wins"] == a["priced_sales"] == 0


def test_sale_coverage_distinguishes_unknown_cost_zero_pnl_and_estimates():
    events = [event(1, "buy", None, "a", 12, 3),
              dict(event(2, "stake_transfer", "a", "b", 4),
                   evidence="snapshot_match"),
              event(3, "sell", "b", None, 2, 5),
              event(4, "sell", "a", None, 8, 2),
              dict(event(5, "sell", "a", None, 1, 6), netuid=12)]
    sales = []
    _, stats = portfolio(events, [], sale_details=sales)
    estimated, even, unknown = sales
    assert estimated["known_realized_tao"] is None
    assert estimated["probable_alpha"] == 2
    assert estimated["probable_cost_tao"] == 0.5
    assert stats["b"]["known_realized_tao"] is None
    assert even["known_realized_tao"] == 0
    assert stats["a"]["known_realized_tao"] == 0
    assert unknown["basis_status"] == "unpriced"
    assert unknown["unknown_alpha"] == 1
    assert stats["a"]["known_sale_proceeds_tao"] == 2
    assert stats["a"]["unresolved_sale_proceeds_tao"] == 6


def test_matched_reallocation_legs_do_not_reset_entry_or_score_trades():
    buy = event(1, "buy", None, "a", 10, 5)
    sell_leg = event(2, "sell", "a", None, 4, 4, id="debit",
                     extrinsic="500-0020", event_index=51)
    buy_leg = event(2, "buy", None, "a", 4, 4, id="credit",
                    extrinsic="500-0020", event_index=52)
    real_sale = event(3, "sell", "a", None, 2, 1.8)
    rows = [buy_leg, real_sale, sell_leg, buy]
    from seo.tao_ledger import matching_reallocation_ids
    assert matching_reallocation_ids(rows) == {"500-0020"}
    for verified in [True, False]:
        movements = net_transfers(rows, verified_moves={"500-0020"}
                                  if verified else set())
        move, = [e for e in movements if e["kind"] == "stake_reallocation"]
        assert move["paired_event_id"] == "credit"
        assert move["evidence"] == (
            "native_move" if verified else "matching_trade_legs")
        positions, stats = portfolio(movements, [])
        assert positions["a", 8]["alpha"] == 8
        assert stats["a"]["priced_sales"] + stats["a"]["unpriced_sales"] == 1
        assert stats["a"]["wins"] == int(verified)
        assert stats["a"]["known_realized_tao"] == (0.8 if verified else 0.48)


def test_nonconsecutive_or_unequal_fills_are_preserved_as_trades():
    sell = event(2, "sell", "a", None, 4, 3, id="s", extrinsic="5-0003",
                 event_index=11)
    buy = event(2, "buy", None, "a", 4, 3, id="b", extrinsic="5-0003",
                event_index=12)
    for changed in (dict(buy, alpha=4.01), dict(buy, tao=3.0001),
                    dict(buy, event_index=14), dict(buy, netuid=9),
                    dict(buy, dst="b"), dict(buy, time=day(3))):
        movements = net_transfers([sell, changed])
        assert sorted(e["kind"] for e in movements) == ["buy", "sell"]


def test_quote_after_subnet_reregistration_cannot_score_legacy_trade():
    buy = event(1, "buy", None, "a", 5, 1)
    sell = event(4, "sell", "a", None, 2, 2)
    prices = [dict(netuid=8, fetched_at=day(2), price_tao=3,
                   registered_at="2026-10-01T16:00:00Z"),
              dict(netuid=8, fetched_at=day(5), price_tao=0.5,
                   registered_at="2026-10-01T16:00:00Z")]
    assert timing([buy, sell], prices)["a"] == {"evaluated": 1, "favorable": 1}


def test_replay_orders_equivalent_offset_timestamps_by_chain_time():
    buy = dict(event(1, "buy", None, "a", 8, 4), time="2026-10-01T00:00:00Z")
    sell = dict(event(1, "sell", "a", None, 8, 8),
                time="2026-10-01T00:00:00.001000+00:00")
    _, stats = portfolio([sell, buy], [])
    assert stats["a"]["wins"] == 1
    assert stats["a"]["realized_tao"] == 4


def test_timing_proof_retains_time_of_selected_quote():
    from seo.tao_ledger import timing_observations
    e = event(1, "buy", None, "a", 1, 1)
    mark = "2026-10-02T02:25:00+00:00"
    prices = [dict(netuid=8, fetched_at=mark, price_tao=2)]
    assert list(timing_observations([e], prices, with_timestamp=True)) == [
        (e, 2, mark)]
