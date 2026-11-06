"""Resume verified account-table history without skipping moving pages."""
import math
import json
import re

from seo.sources import tao_history as history


WALLET = "5FUcTAuTxS5iS7FyP3CwHee53m3mtYYTVS9hJukfZF5hC8KU"


def trade(number):
    return dict(id=str(number), extrinsic_id=f"{number}-0001",
                timestamp=f"2026-11-05T12:{number // 60:02}:{number % 60:02}Z",
                action="DELEGATE", nominator=dict(ss58=WALLET),
                amount="1000000000", alpha="1000000000", netuid=24)


class Table:
    def __init__(self, numbers, size=10):
        self.rows = [trade(number) for number in numbers]
        self.size, self.page, self.reads = size, 1, []
        self.sb = self
        self.bad_page = None

    def goto(self, url, check_google=False):
        self.page = 1

    def pause(self):
        pass

    def evaluate(self, js):
        match = re.search(r"const target = (\d+);", js)
        if match:
            self.page = int(match[1])
            return True
        if js == history.RESOURCE_STATE:
            return {}
        if js == history.NEXT:
            self.page += 1
            return True
        self.reads.append(self.page)
        if self.page == self.bad_page:
            return dict(status="error")
        start = (self.page - 1) * self.size
        return dict(status="success", data=dict(
            data=self.rows[start:start + self.size], pagination=dict(
                current_page=self.page, total_pages=math.ceil(
                    len(self.rows) / self.size), per_page=self.size,
                total_items=len(self.rows))))


def run(table, checkpoint=None, known=(), pages=2):
    progress, window = {}, {}
    events, status = history.fetch_history(
        table, WALLET, "transactions", pages, checkpoint=checkpoint,
        known_ids=known, progress=progress, window=window)
    assert len(table.reads) <= pages + 3
    return events, status, progress, window


def test_successive_bounded_runs_advance_past_the_initial_fifty_rows():
    known, checkpoint = set(), None
    for _ in range(4):
        events, status, info, _ = run(
            Table(range(119, -1, -1)), checkpoint, known, pages=5)
        known.update(event["id"] for event in events)
        checkpoint = info["checkpoint"]
        if status == "complete":
            break
    assert len(known) == 120
    assert info["source_total"] == info["cumulative"] == 120
    assert info["remaining"] == 0 and status == "complete"
    assert info["completion_tier"] == "observed_source"


def test_one_page_backfill_budget_still_advances_with_bounded_validation():
    known, checkpoint = set(), None
    counts = []
    for _ in range(4):
        events, _, info, _ = run(Table(range(69, -1, -1)), checkpoint,
                                 known, pages=1)
        known.update(event["id"] for event in events)
        counts.append(len(known))
        checkpoint = info["checkpoint"]
    assert counts == [20, 30, 40, 50]


def test_new_head_rows_shift_pages_without_skipping_the_old_frontier():
    events, _, info, _ = run(Table(range(59, -1, -1)))
    known = {event["id"] for event in events}
    table = Table(range(72, -1, -1))
    events, status, info, window = run(table, info["checkpoint"], known)
    known.update(event["id"] for event in events)
    assert {history.normalize(trade(n), "transactions", WALLET)["id"]
            for n in range(30, 73)} <= known
    assert info["source_total"] == 73
    assert window["since"] >= "2026-11-05T12:00:30"
    assert status == "partial"


def test_failed_old_page_keeps_the_last_safe_frontier_and_rows():
    events, _, first, _ = run(Table(range(69, -1, -1)))
    known = {event["id"] for event in events}
    table = Table(range(69, -1, -1))
    table.bad_page = 4
    events, status, failed, _ = run(table, first["checkpoint"], known)
    assert status == "partial"
    assert failed["checkpoint"]["frontier_page"] == 3
    assert failed["errors"]
    assert failed["cumulative"] == len(known)
    assert len(events) >= 10


def test_changed_pagination_never_advances_past_the_changed_page():
    class Changing(Table):
        def evaluate(self, js):
            result = super().evaluate(js)
            if (isinstance(result, dict) and self.page == 2
                    and "data" in result):
                result["data"]["pagination"]["total_items"] += 1
                result["data"]["pagination"]["total_pages"] += 1
            return result

    _, status, info, _ = run(Changing(range(59, -1, -1)))
    assert status == "partial"
    assert info["checkpoint"]["frontier_page"] == 1
    assert info["errors"]


def test_missing_frontier_anchor_restarts_instead_of_skipping():
    events, _, first, _ = run(Table(range(69, -1, -1)))
    known = {event["id"] for event in events}
    cp = dict(first["checkpoint"], anchor_ids=[
        f"no-longer-in-source-{i}" for i in range(10)])
    _, status, info, _ = run(Table(range(69, -1, -1)), cp, known)
    assert status == "partial"
    assert info["checkpoint"]["frontier_page"] == 1
    assert "frontier_anchor_missing_restart" in info["errors"]


def test_shrunk_source_and_changed_page_size_restart_truthfully():
    events, _, first, _ = run(Table(range(69, -1, -1)))
    known = {event["id"] for event in events}
    _, status, info, _ = run(
        Table(range(39, -1, -1), size=5), first["checkpoint"], known, pages=1)
    assert status == "partial"
    assert "pagination_changed_restart" in info["errors"]
    assert info["checkpoint"]["page_size"] == 5
    assert info["source_total"] == 40


def test_duplicate_or_invalid_rows_cannot_extend_coverage():
    table = Table(range(29, -1, -1))
    table.rows[10] = table.rows[11]
    _, status, info, _ = run(table)
    assert status == "partial"
    assert info["checkpoint"]["frontier_page"] == 1
    assert info["cumulative"] == 19


def test_empty_source_and_failure_are_distinct_and_owner_checkpoint_isolated():
    events, status, info, _ = run(Table([]))
    assert events == [] and status == "complete"
    assert info["remaining"] == 0
    table = Table([])
    table.bad_page = 1
    _, status, info, _ = run(table)
    assert status == "unavailable" and info["source_total"] is None
    other = dict(info["checkpoint"], address="different-wallet")
    _, _, info, _ = run(Table(range(29, -1, -1)), other, pages=1)
    assert "checkpoint_identity_mismatch" in info["errors"]


def test_large_head_gap_restarts_safely_and_then_finishes_across_runs():
    events, _, info, _ = run(Table(range(29, -1, -1)), pages=1)
    known = {row['id'] for row in events}
    events, status, info, _ = run(
        Table(range(99, -1, -1)), info['checkpoint'], known, pages=1)
    assert status == 'partial'
    assert 'head_gap_restart' in info['errors']
    assert info['checkpoint']['frontier_page'] == 4
    known.update(row['id'] for row in events)
    for _ in range(7):
        events, status, info, _ = run(
            Table(range(99, -1, -1)), info['checkpoint'], known, pages=1)
        known.update(row['id'] for row in events)
        if status == 'complete':
            break
    assert status == 'complete' and len(known) == 100


def test_throwing_browser_returns_earlier_rows_without_ahead_checkpoint():
    class Throwing(Table):
        def evaluate(self, js):
            if self.page == 2 and js != history.NEXT:
                raise RuntimeError('private browser details')
            return super().evaluate(js)

    events, status, info, _ = run(Throwing(range(29, -1, -1)))
    assert status == 'partial' and len(events) == 10
    assert info['checkpoint']['frontier_page'] == 1
    assert info['errors'] == ['page_read_failed']


def test_failed_head_returns_an_unchanged_independent_checkpoint():
    events, _, first, _ = run(Table(range(69, -1, -1)))
    known = {row['id'] for row in events}
    table = Table(range(69, -1, -1))
    table.bad_page = 1
    _, status, info, _ = run(table, first['checkpoint'], known)
    assert status == 'unavailable'
    assert info['checkpoint'] == first['checkpoint']
    assert info['checkpoint'] is not first['checkpoint']
    assert info['remaining'] is None and info['source_total'] is None
    assert info['cumulative'] == len(known)


def test_count_mismatch_cannot_complete_or_report_negative_remaining():
    events, status, info, _ = run(
        Table(range(9, -1, -1)), known={'old-removed-source-row'})
    assert status == 'partial' and len(events) == 10
    assert info['cumulative'] == 11 and info['source_total'] == 10
    assert info['remaining'] is None
    assert 'terminal_count_mismatch' in info['errors']
    assert 'stored_count_exceeds_source' in info['errors']


def test_resumed_capture_count_includes_overlap_but_added_count_does_not():
    events, _, info, _ = run(Table(range(99, -1, -1)), pages=1)
    known = {row['id'] for row in events}
    events, _, info, _ = run(
        Table(range(99, -1, -1)), info['checkpoint'], known, pages=1)
    assert info['captured_this_run'] == len(events) == 30
    assert info['new_rows'] == 10
    assert info['cumulative'] == 30 and info['remaining'] == 70


def test_fetching_cached_success_cannot_advance_a_checkpoint(monkeypatch):
    class Fetching(Table):
        def evaluate(self, js):
            result = super().evaluate(js)
            if isinstance(result, dict) and 'data' in result:
                result['diagnostic'] = {'fetch_status': 'fetching'}
            return result

    monkeypatch.setattr(history, 'PAGE_POLLS', 1)
    monkeypatch.setattr(history.time, 'sleep', lambda _: None)
    events, status, info, window = run(Fetching(range(29, -1, -1)))
    assert status == 'unavailable' and not events
    assert info['checkpoint']['frontier_page'] == 0
    assert 'since' not in window


def test_partial_malformed_head_preserves_valid_rows_without_coverage():
    table = Table(range(29, -1, -1))
    table.rows[4]['timestamp'] = 'invalid'
    events, status, info, window = run(table)
    assert len(events) == 9 and status == 'partial'
    assert info['source_total'] == 30
    assert info['checkpoint']['frontier_page'] == 0
    assert 'since' not in window


def test_infers_observed_head_width_without_guessing_ten_rows():
    class NoSize(Table):
        def evaluate(self, js):
            result = super().evaluate(js)
            if isinstance(result, dict) and 'data' in result:
                result['data']['pagination'].pop('per_page')
            return result

    events, status, info, _ = run(NoSize(range(20, -1, -1), size=7))
    assert status == 'complete' and len(events) == 21
    assert info['checkpoint']['page_size'] == 7


def test_unsupported_pagination_keeps_rows_and_safe_schema_diagnostics():
    class NoCounts(Table):
        def evaluate(self, js):
            result = super().evaluate(js)
            if isinstance(result, dict) and 'data' in result:
                result['data']['pagination'].pop('total_items')
            return result

    events, status, info, _ = run(NoCounts(range(29, -1, -1)))
    assert status == 'partial' and len(events) == 10
    assert info['source_total'] is None
    assert info['checkpoint']['frontier_page'] == 0
    assert info['errors'] == ['unsupported_pagination']
    assert info['pagination_observed'] == dict(
        keys=['current_page', 'per_page', 'total_pages'],
        integer_fields=dict(current_page=1, total_pages=3, per_page=10))


def test_corrupt_checkpoint_metadata_cannot_jump_beyond_verified_history():
    events, _, first, _ = run(Table(range(69, -1, -1)))
    known = {row['id'] for row in events}
    for change in (dict(total_pages=90), dict(frontier_page=90),
                   dict(anchor_ids=['just-one-anchor'])):
        checkpoint = dict(first['checkpoint'], **change)
        table = Table(range(69, -1, -1))
        _, status, info, _ = run(table, checkpoint, known, pages=1)
        assert status == 'partial'
        assert 'checkpoint_invalid_restart' in info['errors']
        assert table.reads == [1, 2]


def test_empty_after_nonempty_is_not_complete_on_a_changed_source():
    _, _, first, _ = run(Table(range(9, -1, -1)))
    _, status, info, _ = run(Table([]), first['checkpoint'])
    assert status == 'partial'
    assert info['remaining'] is None
    assert info['errors'] == ['pagination_changed_restart']


def test_rejected_public_row_diagnostic_explains_stalled_page_safely():
    table = Table(range(29, -1, -1))
    table.rows[13].update(alpha='0', amount='0', cookies='secret-cookie',
                          headers={'Authorization': 'secret-token'})
    events, status, info, _ = run(table)
    detail = info['row_validation'][0]
    assert status == 'partial' and len(events) == 19
    assert info['checkpoint']['frontier_page'] == 1
    assert detail['page'] == 2
    assert detail['expected_rows'] == detail['received_rows'] == 10
    assert detail['normalized_rows'] == 9
    assert detail['rejection_counts'] == {'alpha_non_positive': 1}
    rejected = detail['rejected_rows'][0]
    assert rejected['row_number'] == 4
    assert rejected['reason'] == 'alpha_non_positive'
    assert rejected['fields']['extrinsic_id'] == '16-0001'
    assert rejected['fields']['alpha'] == '0'
    assert rejected['fields']['amount'] == '0'
    serialized = json.dumps(detail)
    assert 'secret-' not in serialized and 'Authorization' not in serialized
    assert history.normalize(table.rows[13], 'transactions', WALLET) is None


def test_rejection_diagnostics_distinguish_duplicates_order_and_short_page():
    duplicate = Table(range(29, -1, -1))
    duplicate.rows[13] = duplicate.rows[12]
    _, _, info, _ = run(duplicate)
    detail = info['row_validation'][0]
    assert detail['reasons'] == ['duplicate_event_ids']
    assert detail['normalized_rows'] == 10
    assert detail['unique_normalized_rows'] == 9
    assert detail['duplicate_rows'][0]['row_number'] == 4
    assert detail['duplicate_rows'][0]['first_row_number'] == 3

    unordered = Table(range(29, -1, -1))
    unordered.rows[12], unordered.rows[13] = (
        unordered.rows[13], unordered.rows[12])
    _, _, info, _ = run(unordered)
    detail = info['row_validation'][0]
    assert detail['reasons'] == ['timestamp_order_invalid']
    assert detail['order_breaks'][0]['row_number'] == 4


def test_rejection_samples_are_bounded_and_never_echo_arbitrary_values():
    table = Table(range(29, -1, -1))
    for row in table.rows[10:20]:
        row.update(amount='https://private.invalid/?token=private-secret',
                   nominator={'ss58': WALLET, 'name': 'private-name'})
    _, _, info, _ = run(table)
    detail = info['row_validation'][0]
    assert detail['rejection_counts'] == {'trade_amount_invalid': 10}
    assert len(detail['rejected_rows']) == 3
    assert detail['samples_truncated'] is True
    assert 'private-' not in json.dumps(detail)
