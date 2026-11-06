from copy import deepcopy

from seo import tao_wallet_groups as grouping


OLD = "2026-11-01T00:00:00Z"
NOW = "2026-11-02T00:00:00Z"


def transfer(src="A", dst="B", **fields):
    return dict(dict(id="move", src=src, dst=dst, kind="stake_transfer",
                     alpha=10, tao=None, netuid=24, time=OLD,
                     evidence="transaction", extrinsic="100-0002"), **fields)


def wallet(address, alpha=0, cost=0, value=0, realized=0, **fields):
    positions = [] if not alpha else [dict(
        netuid=24, alpha=alpha, known_alpha=alpha, probable_alpha=0,
        unknown_alpha=0, known_cost_tao=cost, current_quote=True,
        known_unrealized_tao=value - cost, marked_value_tao=value)]
    return dict(dict(address=address, current_inventory=True,
                     positions=positions, screens=[], priced_sales=0,
                     unpriced_sales=0, economic_realized_tao=realized,
                     known_realized_tao=realized,
                     gross_unrealized_tao=value - cost,
                     marked_value_tao=value,
                     open=dict(known_pnl_tao=value - cost)), **fields)


def report(stamp=NOW, wallets=(), events=(), **fields):
    return dict(dict(checked_at=stamp, events=list(events),
                     coverage={w["address"]: dict(account="complete",
                               transactions="complete", transfers="complete")
                               for w in wallets},
                     performance=dict(wallets=list(wallets))), **fields)


def test_transfer_chain_keeps_empty_members_and_deduplicates_receipts():
    first = transfer()
    second = transfer("B", "C", id="next", extrinsic="101-0002")
    receipt = dict(first, basis_status="confirmed", known_cost_tao=4,
                   origins=[dict(address="A", cost_tao=4, event_id="buy")])
    data = report(wallets=[wallet("A"), wallet("B"),
                           wallet("C", 10, 4, 7)], events=[first, second],
                  transfers=[first, second], transfer_details=[receipt])
    before = deepcopy(data)
    result = grouping.build_groups([data, data])
    group, = result["groups"]
    assert group["members"] == ["A", "B", "C"]
    assert len(result["edges"]) == 2
    assert result["nodes"]["A"]["holds_now"] is False
    assert result["nodes"]["C"]["holds_now"] is True
    assert result["nodes"]["C"]["held_netuids"] == [24]
    edge = next(e for e in result["edges"] if e["src"] == "A")
    assert edge["extrinsic"] == "100-0002" and edge["alpha"] == 10
    assert edge["receipts"][0]["origins"][0]["cost_tao"] == 4
    assert group["ownership"] == group["coordination"] == "unproven"
    assert data == before


def test_snapshot_matches_and_trade_similarity_do_not_merge_wallets():
    inferred = transfer(evidence="snapshot_match", confidence=.9,
                        extrinsic=None)
    trade = transfer("B", "C", kind="buy", id="trade", tao=5)
    result = grouping.build_groups([report(events=[inferred, trade])])
    assert result["groups"] == []
    edge, = result["edges"]
    assert edge["status"] == "inferred" and not edge["group_eligible"]
    assert edge["confidence"] == .9


def test_screened_hubs_remain_visible_without_joining_unrelated_peers():
    events = [transfer("H", a, id=a, extrinsic=f"10{i}-0001")
              for i, a in enumerate("ABC")]
    events.append(transfer("A", "Z", id="az", extrinsic="200-0001"))
    result = grouping.build_groups([report(
        events=events, wallets=[wallet("H", screens=["exchange"])])])
    group, = result["groups"]
    assert group["members"] == ["A", "Z"]
    assert len(group["bridge_edge_ids"]) == 1
    assert result["excluded_nodes"]["H"] == ["screen:exchange"]
    assert len(result["edges"]) == 4
    degree = grouping.build_groups([report(events=events)], max_degree=2)
    assert degree["groups"][0]["members"] == ["A", "Z"]
    assert "degree_above_limit" in degree["excluded_nodes"]["H"]


def test_activity_limit_and_explicit_exclusion_are_configurable():
    data = report(events=[transfer()], wallets=[wallet(
        "A", activity_24h=dict(trades=51, transfers=51))])
    assert grouping.build_groups([data])["groups"] == []
    enabled = grouping.build_groups([data], max_activity_24h=0)
    assert len(enabled["groups"]) == 1
    assert grouping.build_groups([data], max_activity_24h=0,
                                 excluded_addresses=["B"])["groups"] == []


def test_same_snapshot_economics_never_add_transferred_cost_twice():
    receipt = dict(transfer(), known_cost_tao=4, basis_status="confirmed")
    data = report(events=[transfer()], wallets=[wallet("A", realized=2),
                  wallet("B", 10, 4, 7)], transfer_details=[receipt])
    economics = grouping.build_groups([data])["groups"][0]["economics"]
    assert economics["status"] == "complete_same_snapshot"
    assert economics["realized_tao"] == 2
    assert economics["unrealized_tao"] == 3
    assert economics["total_pnl_tao"] == 5
    assert economics["known_cost_subtotal_tao"] == 4
    assert economics["coverage_fraction"] == 1


def test_mixed_snapshots_only_subtotal_the_freshest_members():
    old = report(OLD, wallets=[wallet("A", 10, 4, 7)])
    new = report(NOW, wallets=[wallet("B", 10, 4, 8)], events=[transfer()])
    result = grouping.build_groups([new, old])
    assert result == grouping.build_groups([old, new])
    economics = result["groups"][0]["economics"]
    assert economics["status"] == "mixed_snapshots"
    assert economics["total_pnl_tao"] is None
    assert economics["unrealized_tao"] is None and not economics["rankable"]
    assert economics["known_unrealized_subtotal_tao"] == 4
    assert economics["known_cost_subtotal_tao"] == 4
    assert economics["subtotal_member_count"] == 1
    assert economics["subtotal_as_of"] == NOW


def test_missing_cost_or_current_inventory_suppresses_group_profit():
    incomplete = wallet("B", 10, 4, 7, gross_unrealized_tao=None,
                        unpriced_sales=1)
    incomplete["positions"][0].update(known_alpha=5, unknown_alpha=5)
    data = report(events=[transfer()], wallets=[wallet("A"), incomplete])
    economics = grouping.build_groups([data])["groups"][0]["economics"]
    assert economics["total_pnl_tao"] is None
    assert economics["fully_costed_members"] == 1
    assert economics["coverage_fraction"] == .5


def test_latest_reclassification_removes_stale_edge_regardless_of_order():
    old = report(OLD, events=[transfer()])
    new = report(NOW, events=[transfer(kind="stake_reallocation")])
    assert grouping.build_groups([new, old])["edges"] == []


def test_documented_coldkey_swap_keeps_evidence_separate_from_transaction():
    result = grouping.build_groups([report(swaps=[["A", "B"]])])
    group, = result["groups"]
    assert group["members"] == ["A", "B"]
    edge, = result["edges"]
    assert edge["evidence"] == "coldkey_swap"
    assert edge["status"] == "documented"
    assert edge["extrinsic"] is None and edge["alpha"] is None


def test_unanchored_unknown_direction_or_failed_events_cannot_form_group():
    events = [transfer(id="no-proof", extrinsic=None),
              transfer(id="unknown", transfer_side="unknown"),
              transfer(id="failed", success=False)]
    result = grouping.build_groups([report(events=events)])
    assert not result["groups"]
    assert all(not e["group_eligible"] for e in result["edges"])


def test_chain_event_identity_keeps_distinct_events_but_deduplicates_views():
    first = transfer(event_index=2)
    other_view = dict(first, id="other-account-copy", owner="B")
    second = dict(first, id="second-event", event_index=3)
    result = grouping.build_groups([
        report(events=[first, other_view, second])])
    assert len(result["edges"]) == 2
    assert sorted(len(e["event_ids"]) for e in result["edges"]) == [1, 2]


def test_invalid_timestamps_fail_explicitly():
    import pytest

    with pytest.raises(ValueError, match="timestamp"):
        grouping.build_groups([report("not-a-date")])


def test_single_cash_flow_is_a_visible_lead_but_repeated_cash_can_join():
    cash = transfer(kind="tao_transfer", alpha=None, tao=2, netuid=None)
    one = grouping.build_groups([report(events=[cash, cash])])
    assert one["groups"] == []
    assert one["edges"][0]["status"] == "confirmed"
    second = dict(cash, id="cash2", extrinsic="101-0001")
    two = grouping.build_groups([report(events=[cash, second])])
    assert two["groups"][0]["members"] == ["A", "B"]
    assert not grouping.build_groups([report(events=[cash, second])],
                                     min_cash_tao=3)["groups"]


def test_cash_hub_filter_preserves_the_stake_chain():
    cash = [transfer("B", a, id=a, kind="tao_transfer", tao=2,
                     alpha=None, netuid=None, extrinsic=f"10{i}-0001")
            for i, a in enumerate("DEF")]
    data = report(events=[transfer(), transfer(
        "B", "C", id="bc", extrinsic="200-0001"), *cash])
    result = grouping.build_groups([data], max_degree=2, max_cash_degree=2,
                                   min_cash_transfers=1)
    group, = result["groups"]
    assert group["members"] == ["A", "B", "C"]
    assert "B" not in result["excluded_nodes"]
    assert len(group["bridge_edge_ids"]) == 3


def test_large_network_drops_cash_bridges_and_flags_large_stake_components():
    moves = [transfer("A", "B"),
             transfer("C", "D", id="cd", extrinsic="101-0001"),
             transfer("B", "C", id="bc", kind="tao_transfer", tao=2,
                      alpha=None, netuid=None, extrinsic="102-0001")]
    result = grouping.build_groups([report(events=moves)],
                                   min_cash_transfers=1, max_group_size=2)
    assert sorted(g["members"] for g in result["groups"]) == [
        ["A", "B"], ["C", "D"]]
    assert all(g["watchable"] for g in result["groups"])
    moves[-1] = dict(moves[-1], kind="stake_transfer", alpha=10,
                     tao=None, netuid=24)
    group, = grouping.build_groups([report(events=moves)],
                                   max_group_size=2)["groups"]
    assert group["members"] == ["A", "B", "C", "D"]
    assert group["oversized"] and not group["watchable"]


def test_same_date_native_overlay_wins_but_never_overrides_newer_report():
    base = report(events=[transfer()],
                  wallets=[wallet("A"), wallet("B", 10, 4, 7)])
    overlay = deepcopy(base)
    overlay["local_evidence"] = dict(source_report_sha256="verified-by-caller")
    overlay["performance"]["wallets"][1] = wallet("B", 10, 4, 8)
    newer = report("2026-11-03T00:00:00Z",
                   wallets=[wallet("A"), wallet("B", 10, 4, 9)])
    group, = grouping.build_groups([overlay, base])["groups"]
    assert group["economics"]["total_pnl_tao"] == 4
    group, = grouping.build_groups([newer, overlay, base])["groups"]
    assert group["economics"]["total_pnl_tao"] == 5


def test_equivalent_timezone_stamps_are_one_snapshot():
    first = report(NOW, wallets=[wallet("A")], events=[transfer()])
    second = report("2026-11-02T01:00:00+01:00",
                    wallets=[wallet("B", 10, 4, 7)])
    group, = grouping.build_groups([first, second])["groups"]
    assert group["economics"]["status"] == "complete_same_snapshot"
    assert group["economics"]["total_pnl_tao"] == 3


def test_later_paired_stake_receipt_absorbs_the_old_counterpart_leg():
    incoming = transfer(id="incoming", event_index=3, transfer_side="in")
    outgoing = transfer(id="outgoing", event_index=2, transfer_side="both",
                        paired_event_id="incoming")
    result = grouping.build_groups([report(OLD, events=[incoming]),
                                    report(NOW, events=[outgoing])])
    edge, = result["edges"]
    assert edge["event_ids"] == ["incoming", "outgoing"]
    assert edge["alpha"] == 10


def test_unknown_cost_is_not_reported_as_a_known_zero_subtotal():
    unknown = wallet("B", 10, 4, 7, gross_unrealized_tao=None)
    unknown["positions"][0].update(
        known_cost_tao=None, known_alpha=0, unknown_alpha=10,
        known_unrealized_tao=None)
    unknown["open"]["known_pnl_tao"] = None
    data = report(events=[transfer()], wallets=[unknown])
    economics = grouping.build_groups([data])["groups"][0]["economics"]
    assert economics["known_cost_subtotal_tao"] is None
    assert economics["known_unrealized_subtotal_tao"] is None


def test_unknown_liquidation_cost_preserves_only_known_group_subtotals():
    unknown = wallet("B", unpriced_sales=0, unpriced_liquidations=1,
                     economic_realized_tao=None, known_realized_tao=3)
    data = report(events=[transfer()], wallets=[wallet("A", realized=2),
                                                unknown])
    economics = grouping.build_groups([data])["groups"][0]["economics"]
    assert economics["status"] == "partial"
    assert economics["fully_costed_members"] == 1
    assert economics["coverage_fraction"] == .5
    assert economics["known_realized_subtotal_tao"] == 5
    assert economics["realized_tao"] is None
    assert economics["total_pnl_tao"] is None and not economics["rankable"]
