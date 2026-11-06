from copy import deepcopy

import pytest

from seo import tao_subnet_risk as risk


NOW = "2026-11-02T12:00:00Z"
OLD = "2026-11-01T00:00:00Z"


def context(stamp=NOW, **fields):
    row = dict(netuid=24, active=True, registration_block=700000,
               generation_id="4:700000", age_blocks=300000,
               subnet_immune=False, subnet_immunity_remaining_blocks=0,
               generation_changed=False, liquidity=dict(
                   tao_reserve_rao="10000000000000",
                   tao_reserve_tao="10000", model="weighted_balancer"))
    row.update(fields)
    return dict(source="local-finney-subnet-context", checked_at=stamp,
                block_time=stamp, finalized_block=1000000,
                block_hash="0x" + "ab" * 32, subnets=[row])


def wallet(**fields):
    return dict(dict(address="A", checked_at=NOW, current_inventory=True,
                     known_realized_tao=7, gross_unrealized_tao=5,
                     positions=[dict(netuid=24, alpha=10, price_tao=.5,
                                     marked_value_tao=5, current_quote=True,
                                     quote_at=NOW)]), **fields)


def buy(block=800000, **fields):
    return dict(dict(kind="buy", dst="A", netuid=24,
                     extrinsic=f"{block}-0001", id="buy", time=NOW), **fields)


def assess(w=None, contexts=None, events=None, **settings):
    return risk.assess_wallet(
        w or wallet(), [context()] if contexts is None else contexts,
        checked_at=NOW, events=[buy()] if events is None else events,
        **settings)


def test_risk_is_separate_from_recorded_profit_and_preserves_inputs():
    w, ctx = wallet(), context()
    before = deepcopy((w, ctx))
    result = assess(w, [ctx])
    assert result["status"] == "no_flags_observed"
    assert result["qualified_candidate"] is True
    assert result["candidate_visible"] is True
    assert result["invalid_current_valuation"] is False
    assert result["subnets"][0]["exposure_ratio"] == .0005
    assert result["subnets"][0]["tao_reserve_tao"] == "10000"
    assert (w, ctx) == before
    assert "realized_tao" not in result


def test_new_immune_subnet_keeps_early_buyer_visible_as_provisional():
    ctx = context(registration_block=990000, age_blocks=10000,
                  generation_id="5:990000", subnet_immune=True,
                  subnet_immunity_remaining_blocks=5000)
    result = assess(contexts=[ctx], events=[buy(991000)])
    assert {"new_subnet", "subnet_in_immunity"} <= set(result["flags"])
    assert result["candidate_visible"] and result["provisional"]
    assert not result["qualified_candidate"]
    row, = result["subnets"]
    assert row["early_entry_observed"] is True
    assert row["age_blocks"] == 10000
    assert row["immunity_remaining_blocks"] == 5000


def test_inactive_subnet_never_fabricates_a_zero_value_or_realized_loss():
    w = wallet()
    result = assess(w, [context(active=False,
                                lifecycle_status="inactive_retained")])
    assert "subnet_inactive" in result["flags"]
    assert result["invalid_current_valuation"]
    assert result["subnets"][0]["exposure_ratio"] is None
    assert w["known_realized_tao"] == 7
    assert w["gross_unrealized_tao"] == 5


@pytest.mark.parametrize("state", [
    dict(lifecycle_status="dissolving"),
    dict(chain_lifecycle_state="PendingDissolution"),
    dict(chain_lifecycle_state="Dissolving"),
    dict(is_dissolving=True),
])
def test_dissolving_subnet_invalidates_exit_even_while_still_active(state):
    result = assess(contexts=[context(active=True, **state)])
    assert "subnet_retiring" in result["flags"]
    assert result["invalid_current_valuation"]
    assert result["subnets"][0]["exposure_ratio"] is None


def test_root_without_a_pool_skips_subnet_generation_and_liquidity_rules():
    ctx = context(netuid=0, registration_block=None, generation_id=None,
                  age_blocks=None, subnet_immune=None,
                  subnet_immunity_remaining_blocks=None,
                  liquidity=dict(model="root_no_pool"))
    w = wallet()
    w["positions"][0]["netuid"] = 0
    result = assess(w, [ctx], events=[])
    assert result["status"] == "no_flags_observed"
    assert result["qualified_candidate"]
    assert not result["subnets"][0]["liquidity_applicable"]
    assert result["subnets"][0]["exposure_ratio"] is None
    assert result["subnets"][0]["exit_quote_tao"] is None


def test_low_reserve_and_exposure_ratio_are_screens_not_slippage_quotes():
    ctx = context(liquidity=dict(tao_reserve_rao="500000000000",
                                 tao_reserve_tao="500", model="unknown"))
    w = wallet()
    w["positions"][0]["marked_value_tao"] = 30
    result = assess(w, [ctx])
    assert {"low_tao_reserve", "high_pool_exposure"} <= set(result["flags"])
    assert result["subnets"][0]["exposure_ratio"] == .06
    assert result["subnets"][0]["exit_quote_tao"] is None
    relaxed = assess(w, [ctx], min_tao_reserve=100,
                     max_exposure_ratio=.1)
    assert "low_tao_reserve" not in relaxed["flags"]
    assert "high_pool_exposure" not in relaxed["flags"]


def test_registration_block_qualifies_mature_pool_without_optional_counter():
    ctx = context(generation_id=None, registered_subnet_counter=None,
                  liquidity=dict(tao_reserve_rao="26166460000000",
                                 tao_reserve_tao="26166.46",
                                 model="weighted_balancer"))
    w = wallet()
    w["positions"][0]["marked_value_tao"] = 5259.24
    result = assess(w, [ctx])
    assert "context_generation_unknown" not in result["flags"]
    assert "high_pool_exposure" in result["flags"]
    assert result["subnets"][0]["exposure_ratio"] == pytest.approx(
        .201, abs=.00001)
    assert result["subnets"][0]["exit_quote_tao"] is None


def test_counter_appearing_does_not_change_the_same_registration_generation():
    old = context(OLD, generation_id="old-format:700000",
                  registered_subnet_counter=None)
    latest = context(generation_id="finney:24:registration:700000",
                     registered_subnet_counter=1)
    result = assess(contexts=[old, latest])
    assert "subnet_generation_changed" not in result["flags"]
    assert not result["invalid_current_valuation"]


def test_inconsistent_counter_is_unknown_not_an_invented_generation_change():
    result = assess(contexts=[context(generation_counter_inconsistent=True)])
    assert "generation_counter_inconsistent" in result["flags"]
    assert "subnet_generation_changed" not in result["flags"]
    assert not result["invalid_current_valuation"]
    assert result["qualified_candidate"] is False
    assert result["subnets"][0]["exposure_ratio"] is None


def test_missing_or_stale_context_is_unknown_and_unqualified():
    missing = assess(contexts=[])
    assert "context_missing" in missing["flags"]
    assert missing["status"] == "unknown" and missing["provisional"]
    stale = assess(contexts=[context(OLD)])
    assert "context_stale" in stale["flags"]
    assert stale["subnets"][0]["exposure_ratio"] is None
    assert stale["qualified_candidate"] is False


def test_recent_fetch_cannot_make_an_old_chain_pin_fresh():
    ctx = context()
    ctx["block_time"] = OLD
    result = assess(contexts=[ctx])
    assert "context_stale" in result["flags"]
    assert result["subnets"][0]["exposure_ratio"] is None


def test_disabled_trading_is_not_a_proven_exit_despite_a_saved_mark():
    result = assess(contexts=[context(trading_enabled=False)])
    assert "trading_disabled" in result["flags"]
    assert result["invalid_current_valuation"]


def test_future_context_does_not_rewrite_earlier_risk_or_profit():
    future = context("2026-11-03T00:00:00Z", active=False)
    result = assess(contexts=[future, context()])
    assert "subnet_inactive" not in result["flags"]
    assert result["subnets"][0]["context_checked_at"] == NOW
    future_only = assess(contexts=[future])
    assert "context_missing" in future_only["flags"]


def test_own_purchase_before_current_registration_invalidates_current_mark():
    w = wallet()
    w["positions"][0]["origins"] = [dict(
        extrinsic="800000-0001", alpha=10, address="A")]
    result = assess(w, contexts=[context(
        registration_block=900000, generation_id="5:900000")])
    assert "entry_generation_conflict" in result["flags"]
    assert result["invalid_current_valuation"]
    assert not result["qualified_candidate"]
    assert result["subnets"][0]["exposure_ratio"] is None
    assert result["subnets"][0]["entry_blocks"] == [800000]


def test_purchase_at_registration_block_has_unknown_intrablock_order():
    result = assess(events=[buy(700000)])
    assert "entry_registration_boundary" in result["flags"]
    assert not result["invalid_current_valuation"]
    assert not result["qualified_candidate"]
    assert result["subnets"][0]["exposure_ratio"] is None


def test_later_purchase_does_not_qualify_an_earlier_wallet_snapshot():
    stamp = "2026-11-02T11:00:00Z"
    w = wallet(checked_at=stamp)
    w["positions"][0]["quote_at"] = stamp
    result = assess(w, events=[buy(time=NOW)])
    assert "entry_generation_unknown" in result["flags"]
    assert not result["qualified_candidate"]
    assert result["subnets"][0]["entry_blocks"] == []


def test_actual_inventory_time_allows_movements_after_collection_start():
    w = wallet(checked_at="2026-11-02T11:00:00Z",
               account_as_of="2026-11-02T11:05:00Z")
    w["positions"][0]["quote_at"] = w["checked_at"]
    result = assess(w, events=[buy(time="2026-11-02T11:02:00Z")])
    assert result["subnets"][0]["entry_blocks"] == [800000]
    assert result["inventory_as_of"] == "2026-11-02T11:05:00Z"
    assert result["wallet_as_of"] == "2026-11-02T11:00:00Z"
    after = assess(w, events=[buy(time="2026-11-02T11:06:00Z")])
    assert "entry_generation_unknown" in after["flags"]


def test_transferred_purchase_origins_cannot_evade_generation_conflict():
    edge = dict(kind="stake_transfer", src="B", dst="A", netuid=24,
                extrinsic="950000-0001", receipts=[dict(
                    src="B", dst="A", netuid=24, origins=[dict(
                        address="Chef", event_id="original-buy",
                        extrinsic="650000-0003", alpha=10, cost_tao=2)])])
    w = wallet()
    w["positions"][0]["origins"] = edge["receipts"][0]["origins"]
    result = assess(w, events=[edge])
    assert "entry_generation_conflict" in result["flags"]
    assert result["invalid_current_valuation"]
    assert result["subnets"][0]["entry_blocks"] == [650000]


def test_closed_old_generation_does_not_invalidate_proven_current_origins():
    w = wallet()
    w["positions"][0]["origins"] = [dict(
        extrinsic="800000-0001", alpha=10, address="A")]
    events = [buy(600000), buy(650000, kind="sell", src="A", dst=None),
              buy(800000)]
    result = assess(w, events=events)
    assert not result["invalid_current_valuation"]
    assert "entry_generation_conflict" not in result["flags"]
    assert result["subnets"][0]["mark_compatible"]
    assert result["subnets"][0]["current_entry_blocks"] == [800000]


def test_historical_transfer_origin_is_a_lead_not_remaining_lot_proof():
    receipt = dict(kind="stake_transfer", src="B", dst="A", netuid=24,
                   origins=[dict(extrinsic="600000-0001", alpha=10)])
    result = assess(events=[receipt])
    assert "historical_generation_overlap" in result["flags"]
    assert result["provisional"]
    assert not result["invalid_current_valuation"]


def test_previous_generation_context_detects_a_stale_held_mark():
    previous = context(OLD)
    previous["finalized_block"] = 850000
    latest = context(registration_block=900000, generation_id="5:900000",
                     generation_changed=True)
    w = wallet(checked_at=OLD)
    w["positions"][0]["quote_at"] = OLD
    result = assess(w, [latest, previous], events=[])
    assert "subnet_generation_changed" in result["flags"]
    assert "mark_generation_conflict" in result["flags"]
    assert result["invalid_current_valuation"]


def test_embedded_context_history_preserves_generation_evidence_only():
    previous = context(OLD)
    previous["finalized_block"] = 850000
    previous["subnets"][0].pop("liquidity")
    latest = context(registration_block=900000, generation_id="5:900000")
    latest["history"] = [previous]
    w = wallet(checked_at=OLD)
    w["positions"][0]["quote_at"] = OLD
    result = assess(w, latest, events=[])
    assert "mark_generation_conflict" in result["flags"]
    assert result["invalid_current_valuation"]
    assert result["subnets"][0]["tao_reserve_tao"] == "10000"


def test_explicit_full_context_wins_over_its_minimal_history_copy():
    previous = context()
    minimal = deepcopy(previous)
    minimal["subnets"][0].pop("liquidity")
    future = context("2026-11-03T00:00:00Z")
    future["history"] = [minimal]
    result = assess(contexts=[previous, future])
    assert result["status"] == "no_flags_observed"
    assert result["subnets"][0]["tao_reserve_tao"] == "10000"


def test_absent_entry_provenance_and_future_mark_stay_unknown():
    result = assess(events=[])
    assert "entry_generation_unknown" in result["flags"]
    assert result["qualified_candidate"] is False
    assert result["subnets"][0]["exposure_ratio"] is None


@pytest.mark.parametrize("first_quote_current", [True, False])
def test_every_constituent_quote_is_checked_before_summing_a_subnet_mark(
        first_quote_current):
    w = wallet()
    w["positions"][0]["current_quote"] = first_quote_current
    w["positions"].append(dict(
        netuid=24, alpha=10, marked_value_tao=600, current_quote=True,
        quote_at="2026-11-03T00:00:00Z"))
    result = assess(w)
    assert "mark_future" in result["flags"]
    assert result["invalid_current_valuation"]
    assert result["subnets"][0]["mark_compatible"] is False
    assert result["subnets"][0]["exposure_ratio"] is None
    assert result["subnets"][0]["held_marked_value_tao"] is None
    w = wallet()
    w["positions"][0]["quote_at"] = "2026-11-03T00:00:00Z"
    result = assess(w)
    assert "mark_future" in result["flags"]
    assert result["subnets"][0]["exposure_ratio"] is None


def test_reused_netuid_counter_is_caution_without_inventing_a_conflict():
    ctx = context(registration_block=9447220,
                  generation_id="2:9447220", registered_subnet_counter=2,
                  age_blocks=2539, subnet_immune=True,
                  subnet_immunity_period_blocks=432000,
                  subnet_immunity_remaining_blocks=429461,
                  liquidity=dict(tao_reserve_rao="253765051114",
                                 tao_reserve_tao="253.765051114",
                                 model="weighted_balancer"))
    ctx["finalized_block"] = 9449759
    result = assess(contexts=[ctx], events=[buy(9448000)])
    assert "subnet_id_reused" in result["flags"]
    assert "low_tao_reserve" in result["flags"]
    assert "subnet_in_immunity" in result["flags"]
    assert not result["invalid_current_valuation"]
    assert result["subnets"][0]["generation_id"] == "2:9447220"


def test_reused_subnet_does_not_erase_a_fully_closed_historical_win():
    w = wallet(positions=[], economic_realized_tao=7)
    result = assess(w, [context(active=False, registration_block=900000,
                                generation_id="2:900000",
                                registered_subnet_counter=2)])
    assert not result["invalid_current_valuation"]
    assert "entry_generation_conflict" not in result["flags"]
    assert result["subnets"] == []
    assert w["economic_realized_tao"] == 7


def test_directory_projection_and_wallet_map_wrapper_include_all_subnets():
    w = dict(address="A", stamp=NOW, current_inventory=True,
             held=[dict(netuid=24, alpha=10, price_tao=.5,
                        current_quote=True),
                   dict(netuid=128, alpha=3, current_quote=False)],
             examples=[buy()])
    result = risk.assess_wallets({"A": w}, [context()], checked_at=NOW)
    assert set(result) == {"A"}
    assert [row["netuid"] for row in result["A"]["subnets"]] == [24, 128]
    assert result["A"]["subnets"][0]["held_marked_value_tao"] == 5
    assert "context_missing" in result["A"]["flags"]


def test_no_current_inventory_does_not_claim_to_assess_stale_positions():
    result = assess(wallet(current_inventory=False))
    assert "current_inventory_unknown" in result["flags"]
    assert result["subnets"] == []
    assert not result["qualified_candidate"]


def test_invalid_settings_and_naive_assessment_time_fail_explicitly():
    with pytest.raises(ValueError, match="threshold"):
        assess(max_exposure_ratio=-1)
    with pytest.raises(ValueError, match="timezone"):
        risk.assess_wallet(wallet(), [], checked_at="2026-11-02")
