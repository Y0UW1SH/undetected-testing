"""Regressions for interpreting a wallet's observed, partial trade history."""
import pytest

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


def test_scoped_zero_anchor_keeps_later_excess_unknown_after_priced_sale():
    trades = [dict(event(2, "buy", None, "a", 100, 10), netuid=28),
              dict(event(3, "sell", "a", None, 50, 10), netuid=28)]
    cp = [dict(checkpoint(1, "a", 0), netuid_scope=[28]),
          dict(checkpoint(4, "a", 0), units={28: 70})]
    sales = []
    positions, stats = portfolio(trades, cp, sale_details=sales)
    assert sales[0]["basis_status"] == "fully_priced"
    assert sales[0]["known_cost_tao"] == 5
    assert stats["a"]["realized_tao"] == 5
    held = positions["a", 28]
    assert held["alpha"] == 70 and held["known_alpha"] == 50
    assert held["known_cost_tao"] == 5
    assert held["unknown_alpha"] == 20
    assert held["probable_alpha"] == 0
    assert held["buy_price_tao"] is None


def test_scoped_inventory_does_not_clear_or_change_unqueried_subnets():
    trades = [event(1, "buy", None, "a", 10, 5),
              dict(event(3, "buy", None, "a", 100, 10), netuid=28),
              dict(event(4, "sell", "a", None, 50, 10), netuid=28)]
    cp = [dict(checkpoint(2, "a", 0), netuid_scope=[28]),
          dict(checkpoint(5, "a", 0), units={28: 70}, netuid_scope=[28])]
    positions, _ = portfolio(trades, cp)
    held = positions["a", 8]
    assert held["alpha"] == held["known_alpha"] == 10
    assert held["known_cost_tao"] == 5
    assert held["unknown_alpha"] == 0


def test_full_snapshot_after_scope_only_infers_unanchored_opening_units():
    trades = [dict(event(2, "buy", None, "a", 100, 10), netuid=28),
              dict(event(3, "sell", "a", None, 50, 10), netuid=28),
              event(2, "buy", None, "a", 100, 20, id="other_buy"),
              event(3, "sell", "a", None, 50, 20, id="other_sell")]
    cp = [dict(checkpoint(1, "a", 0), netuid_scope=[28]),
          dict(checkpoint(4, "a", 0), units={28: 70, 8: 70})]
    sales = []
    positions, _ = portfolio(trades, cp, sale_details=sales)
    by_subnet = {s["netuid"]: s for s in sales}
    assert by_subnet[28]["basis_status"] == "fully_priced"
    assert by_subnet[28]["known_cost_tao"] == 5
    assert by_subnet[8]["basis_status"] == "partially_priced"
    assert abs(by_subnet[8]["known_alpha"] - 125 / 3) < 1e-12
    assert positions["a", 28]["unknown_alpha"] == 20
    assert abs(positions["a", 8]["unknown_alpha"] - 35 / 3) < 1e-12


def test_scoped_inventory_is_not_evidence_for_inferred_transfers():
    cp = [checkpoint(1, "a", 10), checkpoint(1, "b", 0),
          dict(checkpoint(2, "a", 0), netuid_scope=[28]),
          checkpoint(2, "b", 10)]
    assert inferred_links(cp, []) == []
    cp = [checkpoint(1, "a", 10), checkpoint(2, "a", 0, "b"),
          dict(checkpoint(2, "b", 10), netuid_scope=[8])]
    assert inferred_links(cp, []) == []


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


def growth(start, end, address, before, after, **fields):
    return dict(id=f"growth/{address}/{start}/{end}", address=address,
                netuid=8, start_time=day(start), time=day(end),
                before_alpha=before, after_alpha=after,
                evidence="verified_stake_growth", **fields)


def test_growth_keeps_purchase_cost_and_origins_through_partial_moves():
    events = [event(1, "buy", None, "a", 100, 50, extrinsic="100-1"),
              event(4, "stake_transfer", "a", "b", 60),
              event(7, "stake_transfer", "b", "c", 33)]
    growths = [growth(2, 3, "a", 100, 120),
               growth(5, 6, "b", 60, 66)]
    receipts = []
    positions, _ = portfolio(events, [], growth_events=growths,
                             transfer_details=receipts)
    held = positions["c", 8]
    assert held["alpha"] == held["known_alpha"] == 33
    assert held["known_cost_tao"] == 12.5
    assert held["growth_alpha"] == held["known_growth_alpha"] == 8
    assert held["unknown_alpha"] == 0
    assert held["buy_price_tao"] == 0.5
    assert held["effective_cost_per_alpha_tao"] == pytest.approx(12.5 / 33)
    receipt = receipts[-1]
    assert receipt["inherited_buy_price_tao"] == 0.5
    assert receipt["growth_alpha"] == 8
    origin, = receipt["origins"]
    assert origin["extrinsic"] == "100-1"
    assert origin["unit_buy_price_tao"] == 0.5
    assert origin["alpha"] == 33 and origin["purchased_alpha"] == 25
    assert origin["growth_alpha"] == 8 and origin["cost_tao"] == 12.5


def test_growth_sale_separates_economic_return_from_trading_signal():
    events = [event(1, "buy", None, "a", 100, 100),
              event(4, "sell", "a", None, 100, 100)]
    details, subnet = [], {}
    positions, stats = portfolio(
        events, [], growth_events=[growth(2, 3, "a", 100, 200)],
        sale_details=details, subnet_stats=subnet)
    sale, = details
    assert sale["basis_status"] == "fully_priced"
    assert sale["known_cost_tao"] == 50
    assert sale["known_realized_tao"] == 50
    assert sale["known_trading_realized_tao"] == 0
    assert sale["growth_proceeds_tao"] == 50
    assert sale["known_growth_proceeds_tao"] == 50
    assert sale["trading_eligible"] is False
    assert positions["a", 8]["known_cost_tao"] == 50
    assert positions["a", 8]["known_growth_alpha"] == 50
    assert stats["a"] == subnet["a", 8]
    assert stats["a"]["known_realized_tao"] == 50
    assert stats["a"]["known_trading_realized_tao"] == 0
    assert stats["a"]["growth_sales"] == 1
    assert stats["a"]["wins"] == stats["a"]["priced_sales"] == 0
    assert stats["a"]["unpriced_sales"] == stats["a"]["realized_tao"] == 0


def test_growth_preserves_unknown_and_probable_coverage_without_zero_cost():
    events = [event(1, "buy", None, "source", 10, 20),
              event(2, "stake_transfer", "source", "a", 10)]
    events[1]["evidence"] = "snapshot_match"
    events += [event(3, "buy", None, "a", 10, 30),
               event(4, "stake_transfer", None, "a", 10),
               event(7, "stake_transfer", "a", "b", 30),
               event(8, "sell", "b", None, 15, 45)]
    details = []
    positions, stats = portfolio(
        events, [], growth_events=[growth(5, 6, "a", 30, 60)],
        sale_details=details)
    held = positions["b", 8]
    assert held["alpha"] == 15
    assert held["known_alpha"] == held["probable_alpha"] == 5
    assert held["unknown_alpha"] == 5
    assert held["known_growth_alpha"] == held["probable_growth_alpha"] == 2.5
    assert held["unknown_growth_alpha"] == 2.5
    assert held["known_cost_tao"] == 7.5 and held["probable_cost_tao"] == 5
    assert held["buy_price_tao"] is None
    sale, = details
    assert sale["known_realized_tao"] == 7.5
    assert sale["known_trading_realized_tao"] == 0
    assert sale["growth_proceeds_tao"] == 22.5
    assert sale["known_growth_proceeds_tao"] == 7.5
    assert sale["unresolved_proceeds_tao"] == 30
    assert stats["b"]["wins"] == 0 and stats["b"]["unpriced_sales"] == 1


def test_dated_growth_does_not_seed_future_earned_units_as_opening_inventory():
    events = [event(1, "buy", None, "a", 100, 50),
              event(4, "sell", "a", None, 60, 60)]
    snapshots = [checkpoint(5, "a", 60)]
    details = []
    positions, stats = portfolio(
        events, snapshots, growth_events=[growth(2, 3, "a", 100, 120)],
        sale_details=details)
    assert details[0]["known_alpha"] == 60
    assert details[0]["known_cost_tao"] == 25
    assert positions["a", 8]["unknown_alpha"] == 0
    assert stats["a"]["known_trading_realized_tao"] == 25
    unknown, unknown_stats = portfolio(
        [event(4, "sell", "x", None, 10, 20)], [],
        growth_events=[growth(2, 3, "x", 10, 20)])
    assert unknown["x", 8]["unknown_alpha"] == 10
    assert unknown["x", 8]["unknown_growth_alpha"] == 5
    assert unknown_stats["x"]["known_realized_tao"] is None
    assert unknown_stats["x"]["known_trading_realized_tao"] is None


def test_growth_rejects_intervening_movements_and_conflicting_inventory():
    buy = event(1, "buy", None, "a", 100, 50)
    proof = growth(2, 4, "a", 100, 120)
    for movement in [event(3, "sell", "a", None, 10, 10),
                     event(3, "stake_transfer", "a", "b", 10),
                     event(4, "buy", None, "a", 10, 10),
                     event(3, "stake_reallocation", "a", "a", 100)]:
        with pytest.raises(ValueError, match="movement"):
            portfolio([buy, movement], [], growth_events=[proof])
    with pytest.raises(ValueError, match="inventory"):
        portfolio([buy], [dict(checkpoint(1, "a", 0), netuid_scope=[8],
                               time=day(1).replace("00:00:00", "00:00:01"))],
                  growth_events=[proof])


def test_growth_rejects_invalid_proofs_and_deduplicates_repeated_import():
    buy = event(1, "buy", None, "a", 100, 50)
    proof = growth(2, 3, "a", 100, 120)
    for fields in [dict(evidence="snapshot_match"), dict(before_alpha=0),
                   dict(after_alpha=float("nan")), dict(after_alpha=90),
                   dict(start_time=day(3)), dict(time=day(1))]:
        with pytest.raises(ValueError):
            portfolio([buy], [], growth_events=[dict(proof, **fields)])
    with pytest.raises(ValueError, match="(?i)overlap"):
        portfolio([buy], [], growth_events=[
            proof, growth(2, 4, "a", 100, 130)])
    with pytest.raises(ValueError, match="(?i)conflict"):
        portfolio([buy], [], growth_events=[
            proof, dict(proof, after_alpha=130)])
    positions, _ = portfolio([buy], [], growth_events=[proof, proof])
    assert positions["a", 8]["alpha"] == 120


def test_growth_does_not_override_a_contradictory_dated_inventory():
    buy = event(1, "buy", None, "a", 100, 50)
    proof = growth(2, 4, "a", 100, 120)
    for snapshot in [checkpoint(3, "a", 110), checkpoint(4, "a", 130)]:
        with pytest.raises(ValueError, match="inventory"):
            portfolio([buy], [snapshot], growth_events=[proof])


def test_growth_receipt_keeps_estimated_origins_and_unsampled_purchase_cost():
    buys = [event(i, "buy", None, "a", 10, i, extrinsic=f"{i}-1")
            for i in range(1, 8)]
    move = dict(event(10, "stake_transfer", "a", "b", 140),
                evidence="snapshot_match")
    receipts = []
    positions, _ = portfolio(buys + [move], [],
                             growth_events=[growth(8, 9, "a", 70, 140)],
                             transfer_details=receipts)
    held = positions["b", 8]
    assert held["known_alpha"] == held["known_growth_alpha"] == 0
    assert held["probable_alpha"] == 140
    assert held["probable_growth_alpha"] == 70
    assert held["probable_cost_tao"] == 28 and held["buy_price_tao"] == 0.4
    receipt, = receipts
    assert receipt["basis_status"] == "estimated"
    assert receipt["other_origin_alpha"] == 40
    assert receipt["other_origin_cost_tao"] == 13
    assert len(receipt["origins"]) == 5
    assert sum(o["cost_tao"] for o in receipt["origins"]) == 15
    assert all(o["purchased_alpha"] == o["growth_alpha"] == 10
               and o["basis_status"] == "estimated"
               for o in receipt["origins"])


def test_growth_start_follows_same_timestamp_purchase_and_growth_end():
    buy = event(1, "buy", None, "a", 100, 50)
    proofs = [growth(1, 2, "a", 100, 120), growth(2, 3, "a", 120, 144)]
    positions, _ = portfolio([buy], [], growth_events=proofs[::-1])
    held = positions["a", 8]
    assert held["alpha"] == held["known_alpha"] == 144
    assert held["known_cost_tao"] == 50 and held["known_growth_alpha"] == 44
    assert held["buy_price_tao"] == 0.5 and held["unknown_alpha"] == 0


def test_one_nano_rounding_preserves_cost_without_hiding_unknown_units():
    events = [event(1, "buy", None, "a", 10, 5),
              event(3, "stake_transfer", "a", "b", 12.000000001)]
    proofs = [growth(1, 2, "a", 10, 12),
              growth(3, 4, "b", 12, 13.2)]
    positions, _ = portfolio(events, [], growth_events=proofs)
    held = positions["b", 8]
    assert held["known_cost_tao"] == 5
    assert held["unknown_alpha"] == pytest.approx(1.1e-9, abs=1e-14)
    assert held["buy_price_tao"] is None
    assert held["known_growth_alpha"] == pytest.approx(3.2, abs=2e-9)


def test_growth_cannot_be_injected_as_unvalidated_ordinary_event():
    injected = dict(event(2, "stake_growth", None, "a", 20),
                    before_alpha=100, after_alpha=120)
    with pytest.raises(ValueError, match="growth_events"):
        portfolio([event(1, "buy", None, "a", 100, 50), injected], [])


def test_growth_tolerance_rejects_zero_balance_or_negative_delta():
    with pytest.raises(ValueError, match="inventory"):
        portfolio([], [checkpoint(1, "a", 0)],
                  growth_events=[growth(2, 3, "a", 1e-10, 2e-10)])
    with pytest.raises(ValueError, match="inventory"):
        portfolio([event(1, "buy", None, "a", 100.000000001, 50)], [],
                  growth_events=[growth(2, 3, "a", 100, 100.0000000005)])


def test_lossy_reallocation_is_only_a_candidate_until_native_verification():
    from seo.tao_ledger import matching_reallocation_ids
    debit = event(2, "sell", "a", None, 10, 5, id="debit",
                  extrinsic="200-1", event_index=1)
    credit = event(2, "buy", None, "a", 9.999999999, 5, id="credit",
                   extrinsic="200-1", event_index=2)
    assert matching_reallocation_ids([debit, credit]) == {"200-1"}
    assert [row["kind"] for row in net_transfers([debit, credit])] == [
        "sell", "buy"]
    for alpha in (9.999999998, 10.000000001):
        assert matching_reallocation_ids([
            debit, dict(credit, alpha=alpha)]) == set()


def test_verified_rounding_keeps_cost_and_original_price_through_transfer():
    original = event(1, "buy", None, "a", 10, 5, extrinsic="100-1")
    debit = event(2, "sell", "a", None, 10, 7, id="debit",
                  extrinsic="200-1", event_index=1)
    credit = event(2, "buy", None, "a", 9.999999999, 7, id="credit",
                   extrinsic="200-1", event_index=2)
    moved = event(3, "stake_transfer", "a", "b", 9.999999999)
    events = net_transfers([original, debit, credit, moved],
                           verified_moves={"200-1"})
    reallocation = events[1]
    assert reallocation["kind"] == "stake_reallocation"
    assert reallocation["received_alpha"] == 9.999999999
    assert reallocation["rounding_loss_alpha"] == pytest.approx(1e-9)
    receipts = []
    positions, stats = portfolio(events, [], transfer_details=receipts)
    held = positions["b", 8]
    assert held["alpha"] == held["known_alpha"] == 9.999999999
    assert held["known_cost_tao"] == 5 and held["buy_price_tao"] == 0.5
    assert held["unknown_alpha"] == 0 and not stats
    receipt, = receipts
    assert receipt["inherited_buy_price_tao"] == 0.5
    origin, = receipt["origins"]
    assert origin["purchased_alpha"] == 10 and origin["cost_tao"] == 5
    assert origin["alpha"] == 9.999999999
    assert origin["purchased_rounding_loss_alpha"] == pytest.approx(1e-9)
    positions, stats = portfolio(events + [
        event(4, "sell", "b", None, 4.9999999995, 2.5)], [])
    assert positions["b", 8]["known_cost_tao"] == 2.5
    assert positions["b", 8]["known_purchased_alpha"] == 5
    assert positions["b", 8]["buy_price_tao"] == 0.5
    assert stats["b"]["known_realized_tao"] == 0


def test_verified_rounding_loss_of_grown_stake_keeps_paid_units_distinct():
    debit = event(4, "sell", "a", None, 100, 100, id="debit",
                  extrinsic="400-1", event_index=1)
    credit = event(4, "buy", None, "a", 99.999999999, 100, id="credit",
                   extrinsic="400-1", event_index=2)
    events = net_transfers(
        [event(1, "buy", None, "a", 100, 50), debit, credit],
        verified_moves={"400-1"})
    positions, _ = portfolio(events, [],
                             growth_events=[growth(2, 3, "a", 100, 200)])
    held = positions["a", 8]
    assert held["alpha"] == pytest.approx(199.999999999, abs=1e-13)
    assert held["known_cost_tao"] == 50 and held["buy_price_tao"] == 0.5
    assert held["known_growth_alpha"] == pytest.approx(
        99.9999999995, abs=1e-13)
    assert held["known_purchased_alpha"] == 100
    assert held["unknown_alpha"] == 0


def test_growth_rejects_interior_inventory_changes_even_if_later_restored():
    buy = event(1, "buy", None, "a", 100, 50)
    proof = growth(2, 5, "a", 100, 120)
    for amount in (90, 110):
        snapshots = [checkpoint(3, "a", amount), checkpoint(4, "a", 100)]
        with pytest.raises(ValueError, match="inventory"):
            portfolio([buy], snapshots, growth_events=[proof])


def test_native_rounding_then_growth_and_estimated_move_retains_origins():
    debit = event(2, "sell", "a", None, 10, 7, id="debit",
                  extrinsic="200-1", event_index=1)
    credit = event(2, "buy", None, "a", 9.999999999, 7, id="credit",
                   extrinsic="200-1", event_index=2)
    move = dict(event(5, "stake_transfer", "a", "b", 19.999999998),
                evidence="snapshot_match")
    events = net_transfers(
        [event(1, "buy", None, "a", 10, 5), debit, credit, move],
        verified_moves={"200-1"})
    receipts = []
    positions, _ = portfolio(
        events, [], transfer_details=receipts,
        growth_events=[growth(3, 4, "a", 9.999999999, 19.999999998)])
    held = positions["b", 8]
    assert held["probable_cost_tao"] == 5 and held["buy_price_tao"] == 0.5
    assert held["probable_purchased_alpha"] == 10
    assert held["probable_growth_alpha"] == 9.999999999
    assert held["known_alpha"] == held["known_purchased_alpha"] == 0
    origin, = receipts[0]["origins"]
    assert origin["purchased_alpha"] == 10
    assert origin["growth_alpha"] == 9.999999999
    assert origin["cost_tao"] == 5 and origin["basis_status"] == "estimated"
    assert origin["alpha"] == pytest.approx(19.999999998, abs=1e-13)
