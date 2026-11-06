"""Forced subnet settlement closes stake without inventing a trading win."""
import pytest

from seo.tao_ledger import net_transfers, portfolio, timing
from seo.sources.tao_prices import timing_evidence
from tests.test_tao_ledger import day, event, growth
from tests.test_tao_performance import COMPLETE, price, record


def liquidation(n, address="A", alpha=4, tao=3, **fields):
    return dict(event(n, "subnet_liquidation", address, None, alpha, tao,
                      extrinsic=None, block_number=8884353, **fields),
                evidence="taostats_subnet_liquidation",
                forced_disposal=True, trading_eligible=False)


def test_liquidation_consumes_inherited_cost_without_counting_a_sale():
    rows = [event(1, "buy", None, "donor", 10, 5),
            event(2, "stake_transfer", "donor", "A", 8), liquidation(3)]
    details, subnets = [], {}
    positions, totals = portfolio(net_transfers(rows), [],
                                  sale_details=details, subnet_stats=subnets)
    assert positions["A", 8]["alpha"] == 4
    assert positions["A", 8]["known_cost_tao"] == 2
    s = totals["A"]
    assert s["known_realized_tao"] == s["economic_realized_tao"] == 1
    assert s["known_liquidation_realized_tao"] == 1
    assert s["liquidations"] == s["priced_liquidations"] == 1
    assert s["unpriced_liquidations"] == 0
    assert s["wins"] == s["priced_sales"] == s["unpriced_sales"] == 0
    assert s["partial_sales"] == s["realized_tao"] == 0
    assert s["known_trading_realized_tao"] is None
    assert subnets["A", 8]["economic_realized_tao"] == 1
    detail = details[0]
    assert detail["kind"] == "subnet_liquidation"
    assert detail["forced_disposal"] is True
    assert detail["trading_eligible"] is False
    assert detail["known_trading_realized_tao"] is None
    assert detail["extrinsic"] is None and detail["block_number"] == 8884353
    assert detail["evidence"] == "taostats_subnet_liquidation"


@pytest.mark.parametrize("with_purchase", [False, True])
def test_unknown_liquidation_basis_keeps_full_economic_result_unknown(
        with_purchase):
    rows = ([event(1, "buy", None, "A", 4, 2)] if with_purchase else [])
    rows += [event(2, "stake_transfer", None, "A", 4),
             liquidation(3, alpha=4, tao=8)]
    details = []
    _, totals = portfolio(rows, [], sale_details=details)
    s = totals["A"]
    assert s["unpriced_liquidations"] == 1
    assert s["partial_liquidations"] == int(with_purchase)
    assert s["economic_realized_tao"] is None
    assert s["known_realized_tao"] == (3 if with_purchase else None)
    assert s["known_liquidation_realized_tao"] == s["known_realized_tao"]
    assert s["known_trading_realized_tao"] is None
    assert s["unpriced_sales"] == s["partial_sales"] == s["wins"] == 0
    assert details[0]["basis_status"] == (
        "partially_priced" if with_purchase else "unpriced")


def test_growth_and_probable_cost_remain_distinct_in_liquidation():
    rows = [event(1, "buy", None, "A", 10, 5),
            liquidation(4, alpha=10, tao=10)]
    _, totals = portfolio(rows, [], growth_events=[growth(2, 3, "A", 10, 20)])
    assert totals["A"]["economic_realized_tao"] == 7.5
    assert totals["A"]["known_trading_realized_tao"] is None
    assert totals["A"]["growth_sales"] == totals["A"]["wins"] == 0
    rows = [event(1, "buy", None, "B", 10, 5),
            dict(event(2, "stake_transfer", "B", "A", 4),
                 evidence="snapshot_match"), liquidation(3)]
    details = []
    _, totals = portfolio(rows, [], sale_details=details)
    assert totals["A"]["known_realized_tao"] is None
    assert totals["A"]["economic_realized_tao"] is None
    assert details[0]["probable_alpha"] == 4
    assert details[0]["probable_cost_tao"] == 2


def test_liquidation_and_voluntary_sales_keep_independent_statistics():
    rows = [event(1, "buy", None, "A", 10, 5), liquidation(2),
            event(3, "sell", "A", None, 2, 1.5)]
    _, totals = portfolio(rows, [])
    s = totals["A"]
    assert s["economic_realized_tao"] == s["known_realized_tao"] == 1.5
    assert s["known_trading_realized_tao"] == s["realized_tao"] == .5
    assert s["known_sale_proceeds_tao"] == 1.5
    assert s["known_sale_cost_tao"] == 1
    assert s["wins"] == s["priced_sales"] == s["liquidations"] == 1


def test_zero_payout_liquidation_records_loss_only_on_known_cost():
    rows = [event(1, "buy", None, "A", 4, 2), liquidation(2, tao=0)]
    positions, totals = portfolio(rows, [])
    assert ("A", 8) not in positions
    assert totals["A"]["economic_realized_tao"] == -2
    assert totals["A"]["priced_sales"] == totals["A"]["wins"] == 0
    _, unknown = portfolio([liquidation(2, tao=0)], [])
    assert unknown["A"]["economic_realized_tao"] is None
    assert unknown["A"]["known_realized_tao"] is None


def test_finalization_liquidation_follows_same_block_purchase():
    rows = [liquidation(1, alpha=10, tao=8),
            event(1, "buy", None, "A", 10, 5, extrinsic="8884353-0042")]
    positions, totals = portfolio(rows, [])
    assert ("A", 8) not in positions
    assert totals["A"]["economic_realized_tao"] == 3


@pytest.mark.parametrize("unknown", [False, True])
def test_forced_payout_never_qualifies_as_sale_or_partial_winner(
        tmp_path, unknown):
    rows = [event(1, "buy", None, "A", 4, 2)]
    if unknown:
        rows.append(event(2, "stake_transfer", None, "A", 4))
    rows += [liquidation(3, alpha=2, tao=8), liquidation(4, alpha=2, tao=8)]
    run = record(tmp_path, rows, [price(8, .1, 5)], day(5), {"A": COMPLETE})
    w = run["wallets"][0]
    assert run["rankings"]["profit"] == run["rankings"]["profit_partial"] == []
    assert run["rankings"]["sell"] == []
    assert w["win_rate"] is None
    assert w["priced_sales"] == w["unpriced_sales"] == w["partial_sales"] == 0
    assert w["known_sale_return_pct"] is None
    assert w["timing"]["sell"]["mature_trades"] == 0
    assert w["known_realized_tao"] == (7 if unknown else 14)
    assert w["economic_realized_tao"] == (None if unknown else 14)
    assert w["gross_total_pnl_tao"] == (None if unknown else 14)


def test_liquidation_cannot_be_price_timing_evidence():
    forced = liquidation(2, alpha=10, tao=10)
    quotes = [price(8, .1, 3)]
    assert dict(timing([forced], quotes)) == {}
    evidence = timing_evidence([forced], quotes, day(4))
    assert evidence["eligible_trades"] == evidence["matched_trades"] == 0
    assert evidence["awaiting_24h"] == 0
    assert evidence["matches"] == []


def test_forced_gain_cannot_rescue_a_losing_partial_sale(tmp_path):
    rows = [event(1, "buy", None, "A", 8, 4),
            event(2, "stake_transfer", None, "A", 8),
            event(3, "sell", "A", None, 4, 1), liquidation(4, tao=100)]
    run = record(tmp_path, rows, [price(8, 1, 5)], day(5), {"A": COMPLETE},
                 min_sales=1)
    w = run["wallets"][0]
    assert w["known_realized_tao"] == 48.5
    assert w["known_sale_realized_tao"] == -.5
    assert w["known_trading_realized_tao"] == -.5
    assert w["known_sale_return_pct"] == -50
    assert w["partial_sales"] == 1
    assert run["rankings"]["profit_partial"] == []


def test_cli_distinguishes_forced_payout_and_voluntary_partial_sale(capsys):
    from seo.tao_performance_cli import _liquidations, _partial_sales

    rows = [event(1, "buy", None, "A", 8, 4),
            event(2, "stake_transfer", None, "A", 8),
            event(3, "sell", "A", None, 4, 1), liquidation(4, tao=100)]
    _, totals = portfolio(rows, [])
    _partial_sales(totals["A"])
    _liquidations(totals["A"])
    output = capsys.readouterr().out
    assert "realized -0.5 TAO" in output
    assert "1 forced subnet liquidations" in output
    assert "Excluded from voluntary sales, trading wins and timing" in output
