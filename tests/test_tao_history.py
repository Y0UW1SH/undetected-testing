"""The wallet tabs paginate independently and may fail before any rows load."""
import json

from seo.sources import tao_history as history

WALLET = "5FUcTAuTxS5iS7FyP3CwHee53m3mtYYTVS9hJukfZF5hC8KU"
PEER = "5F1tQr8K2VfBr2pG5MpAQf62n5xSAsjuCZheQUy82csaPavg"


def trade(i):
    return {"id": "finney-68-chain-5453624-%d" % i,
            "extrinsic_id": "5453624-0032", "block_number": 5453624,
            "timestamp": "2025-04-28T23:18:24.001Z", "action": "DELEGATE",
            "nominator": {"ss58": WALLET}, "delegate": {"ss58": PEER},
            "amount": "7092857", "alpha": "135443860", "netuid": 68,
            "is_transfer": None, "transfer_address": None}


def liquidation():
    return dict(
        id=f'sn-liquidation-36-8884353-{WALLET}-{PEER}', extrinsic_id='',
        block_number=8884353, timestamp='2026-08-20T06:56:36Z',
        action='UNDELEGATE', amount='9972836140', alpha='7205837776059',
        netuid=36, nominator={'ss58': WALLET}, delegate={'ss58': PEER},
        is_transfer=False)


def page(items, number, count):
    return {"status": "success", "data": {"data": items, "pagination": {
        "current_page": number, "total_pages": count}}}


class Table:
    def __init__(self, states):
        self.states = states
        self.page = 0
        self.sb = self

    def goto(self, url, check_google):
        self.page = 0

    def evaluate(self, js):
        if js == history.NEXT:
            self.page += 1
            return self.page < len(self.states)
        return self.states[self.page]

    def pause(self):
        pass


def test_old_event_id_supplies_chain_event_order():
    e = history.normalize(trade(140), "transactions", WALLET)
    assert e["event_index"] == 140
    assert (e["kind"], e["alpha"], e["tao"]) == (
        "buy", 0.13544386, 0.007092857)
    assert history.normalize(trade(139), "transactions", WALLET)["id"] != (
        e["id"])


def test_missing_numeric_fields_never_become_zero_price_or_nan():
    for bad in [None, "NaN", "Infinity", "bad", "-1"]:
        item = dict(trade(1), amount=bad)
        assert history.normalize(item, "transactions", WALLET) is None


def test_liquidation_without_extrinsic_preserves_typed_source_evidence():
    raw = liquidation()
    event = history.normalize(raw, 'transactions', WALLET)
    assert event['kind'] == 'subnet_liquidation'
    assert event['extrinsic'] is None and event['block_number'] == 8884353
    assert event['tao'] == 9.97283614 and event['alpha'] == 7205.837776059
    assert event['netuid'] == 36
    assert event['src'] == event['owner'] == WALLET
    assert event['dst'] is None and event['transfer_side'] is None
    assert event['evidence'] == 'taostats_subnet_liquidation'
    assert event['source_event_id'] == raw['id']
    assert event['forced_disposal'] is True
    assert event['trading_eligible'] is False
    assert json.loads(event['id']) == ['transactions', None, WALLET, raw['id']]
    assert history.normalize(dict(raw, extrinsic_id=None), 'transactions',
                             WALLET)['id'] == event['id']


def test_liquidation_requires_exact_id_fields_and_valid_amounts():
    raw = liquidation()
    changes = [dict(id=raw['id'].replace('-36-', '-35-')),
               dict(block_number=8884354), dict(netuid=35),
               dict(nominator={'ss58': PEER}), dict(delegate={'ss58': WALLET}),
               dict(action='DELEGATE'), dict(is_transfer=True),
               dict(extrinsic_id='8884353-0001'), dict(amount='-1'),
               dict(amount=None), dict(amount='NaN'), dict(alpha='0'),
               dict(alpha='-1'), dict(block_number=None), dict(netuid=True)]
    for change in changes:
        assert history.normalize(dict(raw, **change), 'transactions',
                                 WALLET) is None, change
    assert history.normalize(raw, 'transfers', WALLET) is None


def test_explicit_zero_liquidation_payout_is_not_a_zero_price_trade():
    event = history.normalize(dict(liquidation(), amount='0'),
                              'transactions', WALLET)
    assert event['kind'] == 'subnet_liquidation' and event['tao'] == 0
    assert event['alpha'] > 0 and event['trading_eligible'] is False
    assert history.normalize(dict(trade(1), amount='0'),
                             'transactions', WALLET) is None


def test_generic_missing_extrinsic_does_not_become_a_liquidation():
    assert history.normalize(dict(trade(1), extrinsic_id=''),
                             'transactions', WALLET) is None
    assert history.normalize(dict(liquidation(), id='unrecognized-system-row'),
                             'transactions', WALLET) is None


def test_unusable_event_records_keep_the_rest_of_a_page():
    near = Table([page([None, trade(140)], 1, 1)])
    events, coverage = history.fetch_history(near, WALLET, "transactions", 1)
    assert coverage == "partial" and len(events) == 1


def test_stale_second_page_cannot_report_complete():
    first = page([trade(140)], 1, 2)
    near = Table([first, first])
    events, coverage = history.fetch_history(near, WALLET, "transactions", 2)
    assert coverage == "partial" and len(events) == 1


def test_paging_over_a_shifting_live_list_reports_partial():
    near = Table([page([trade(140)], 1, 2),
                  page([trade(140), trade(139)], 2, 2)])
    events, coverage = history.fetch_history(near, WALLET, "transactions", 3)
    assert coverage == "partial" and len(events) == 2


def test_empty_confirmation_and_failure_have_distinct_coverage():
    empty = Table([page([], 1, 0)])
    broken = Table([{"status": "error"}])
    assert history.fetch_history(empty, WALLET, "transfers", 1) == (
        [], "complete")
    assert history.fetch_history(broken, WALLET, "transfers", 1) == (
        [], "unavailable")


def test_raw_tao_transfers_are_distinct_from_subnet_movements():
    row = {"id": "one", "timestamp": "2026-11-05T12:00:00+01:00",
           "extrinsic_id": "123-0002", "from": {"ss58": WALLET},
           "to": {"ss58": PEER}, "amount": "1400000000"}
    event = history.normalize(row, "transfers", PEER)
    assert (event["kind"], event["src"], event["dst"], event["netuid"],
            event["alpha"], event["tao"], event["time"]) == (
        "tao_transfer", WALLET, PEER, None, None, 1.4,
        "2026-11-05T11:00:00+00:00")
    stake = dict(trade(140), action="UNDELEGATE", is_transfer=True,
                 transfer_address={"ss58": PEER})
    event = history.normalize(stake, "transactions", WALLET)
    assert event["transfer_side"] == "out"
    assert history.normalize(dict(stake, action="DELEGATE"), "transactions",
                             WALLET)["transfer_side"] == "in"
    assert (event["kind"], event["src"], event["dst"], event["netuid"]) == (
        "stake_transfer", WALLET, PEER, 68)


def test_paginated_success_and_guardrail_limit():
    near = Table([page([trade(140)], 1, 2), page([trade(139)], 2, 2)])
    events, coverage = history.fetch_history(near, WALLET, "transactions", 2)
    assert len(events) == 2 and coverage == "complete"
    events, coverage = history.fetch_history(near, WALLET, "transactions", 1)
    assert len(events) == 1 and coverage == "partial"


def test_global_activity_discovery_reads_trades_from_any_wallet():
    row = dict(trade(141), action="UNDELEGATE",
               is_transfer=True, transfer_address={"ss58": PEER})
    near = Table([page([row, trade(140)], 1, 1)])
    events, status = history.fetch_recent_trades(near)
    assert status == "complete"
    assert [(e["owner"], e["kind"]) for e in events] == [(WALLET, "buy")]


def test_delayed_tab_waits_for_success_after_widget_initializes(monkeypatch):
    monkeypatch.setattr(history.time, "sleep", lambda seconds: None)

    class SlowTable(Table):
        polls = 0

        def evaluate(self, js):
            self.polls += 1
            if self.polls <= 25:
                return {"status": "pending"}
            return super().evaluate(js)

    near = SlowTable([page([trade(140)], 1, 1)])
    events, status = history.fetch_history(near, WALLET, "transactions", 1)
    assert status == "complete" and len(events) == 1


def test_waits_once_for_active_verification_control(monkeypatch):
    monkeypatch.setattr(history.time, "sleep", lambda _: None)

    class VerifyingTable(Table):
        attempts = 0

        def evaluate(self, js):
            if self.attempts:
                return page([trade(140)], 1, 1)
            return {"status": "pending", "diagnostic": {
                "widget_present": True, "widget_completed": False}}

        def solve_captcha(self):
            self.attempts += 1

    session = VerifyingTable([])
    rows, status = history.fetch_history(session, WALLET, "transactions", 1)
    assert status == "complete" and len(rows) == 1
    assert session.attempts == 1


def test_stalled_widget_records_one_visible_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(history.time, "sleep", lambda _: None)
    monkeypatch.setenv("TAO_DIAGNOSTIC_DIR", str(tmp_path))

    class Stalled(Table):
        pictures = []
        clicks = 0

        def evaluate(self, js):
            if js == history.RESOURCE_STATE:
                return {"table_requests": 0}
            return {"status": "pending", "diagnostic": {
                "widget_present": True, "widget_completed": False}}

        def gui_click_captcha(self):
            self.clicks += 1

        def save_screenshot(self, path):
            from pathlib import Path
            self.pictures.append(path)
            Path(path).touch()

    s = Stalled([])
    _, status = history.fetch_history(s, WALLET, "transactions", 1)
    assert status == "unavailable" and s.clicks == 1 and len(s.pictures) == 1


def test_uses_taostats_checkbox_even_when_grid_has_spacer(monkeypatch):
    monkeypatch.setattr(history.time, "sleep", lambda _: None)

    class GridWithWidget(Table):
        target = None

        def evaluate(self, js):
            if self.target:
                return page([trade(140)], 1, 1)
            return {"status": "pending", "diagnostic": {
                "widget_present": True, "widget_completed": False,
                "taostats_control_visible": True}}

        def click_with_offset(self, selector, x, y, scroll=True):
            self.target = (selector, x, y, scroll)

        def gui_click_captcha(self):
            raise AssertionError("generic helper matches the grid spacer")

    session = GridWithWidget([])
    rows, status = history.fetch_history(session, WALLET, "transactions", 1)
    assert status == "complete" and len(rows) == 1
    assert session.target == ("#cf-turnstile", 20, 32, False)
