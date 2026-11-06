"""Persistent holder research must advance without inventing coverage."""
import json
import sqlite3

import pytest


STAMP = "2026-11-06T12:00:00+00:00"
COMPLETE = {"account": "complete", "transactions": "complete"}


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE holder_registry (netuid INTEGER, address TEXT, "
                 "rank INTEGER, balance_units TEXT, first_seen TEXT, "
                 "last_seen TEXT, PRIMARY KEY(netuid,address))")
    yield conn
    conn.close()


def holders(db, *rows):
    db.executemany("INSERT INTO holder_registry VALUES (?,?,?,'100',?,?)",
                   [(n, a, rank, STAMP, STAMP) for n, a, rank in rows])


def history(db, address, coverage, stamp=STAMP, **extra):
    db.execute("CREATE TABLE IF NOT EXISTS wallet_performance "
               "(address TEXT, checked_at TEXT, payload TEXT NOT NULL, "
               "PRIMARY KEY(address,checked_at))")
    payload = dict(address=address, coverage=coverage, **extra)
    db.execute("INSERT INTO wallet_performance VALUES (?,?,?)",
               (address, stamp, json.dumps(payload)))


def test_round_robin_makes_new_batches_and_retains_largest(db):
    from seo import tao_holder_queue as queue

    holders(db, (1, "largest_a", 1), (1, "a", 2), (1, "c", 3),
            (2, "largest_b", 1), (2, "b", 2), (2, "d", 3))
    assert queue.select(db, [1, 2], 2, STAMP) == ["a", "b"]
    queue.record(db, {"a": COMPLETE, "b": COMPLETE}, STAMP)
    assert queue.select(db, [1, 2], 2, STAMP) == ["c", "d"]
    queue.record(db, {"c": COMPLETE, "d": COMPLETE}, STAMP)
    assert queue.select(db, [1, 2], 0, STAMP) == ["largest_a", "largest_b"]


def test_repeated_small_batches_do_not_starve_other_subnets(db):
    from seo import tao_holder_queue as queue

    holders(db, (1, "a", 2), (1, "c", 3), (1, "e", 4),
            (2, "b", 2), (2, "d", 3))
    assert queue.select(db, [1, 2], 1, STAMP) == ["a"]
    queue.record(db, {"a": COMPLETE}, STAMP)
    assert queue.select(db, [1, 2], 1, STAMP) == ["b"]


def test_duplicates_do_not_consume_slots_or_requested_subnets(db):
    from seo import tao_holder_queue as queue

    holders(db, (1, "shared", 2), (1, "a", 3),
            (2, "shared", 2), (2, "b", 3), (3, "outside", 2))
    assert queue.select(db, [1, 1, 2], 4, STAMP) == ["shared", "b", "a"]
    assert queue.summary(db, [1, 2], STAMP)["registered"] == 3
    assert queue.select(db, [], 0, STAMP) == []


def test_largest_holder_has_bounded_deferral(db):
    from seo import tao_holder_queue as queue

    holders(db, *((1, "rank_%s" % rank, rank) for rank in range(1, 20)))
    assert queue.select(db, [1], 6, STAMP) == [
        "rank_2", "rank_3", "rank_4", "rank_5", "rank_6", "rank_1"]


def test_only_attributed_usable_historical_coverage_is_inspected(db):
    from seo import tao_holder_queue as queue

    holders(db, (1, "full", 2), (1, "partial", 3), (1, "peer", 4),
            (1, "failed_account", 5), (1, "failed_history", 6),
            (1, "skipped", 7), (1, "zero_trades", 8))
    history(db, "full", COMPLETE)
    history(db, "partial", {**COMPLETE, "transactions": "partial",
                            "transactions_rows": 2})
    history(db, "peer", {}, transfer_links=[{"src": "full", "dst": "peer"}])
    history(db, "failed_account", {**COMPLETE, "account": "unavailable"})
    history(db, "failed_history", {**COMPLETE, "transactions": "unavailable"})
    history(db, "skipped", {**COMPLETE, "transactions": "skipped"})
    history(db, "zero_trades", COMPLETE, priced_sales=0, positions=[])
    assert queue.select(db, [1], 0, STAMP) == [
        "peer", "failed_account", "failed_history", "skipped"]
    counts = queue.summary(db, [1], STAMP)
    assert (counts["registered"], counts["usable_history"],
            counts["retry_wait"], counts["pending"]) == (7, 3, 0, 4)


def test_old_usable_history_survives_newer_failed_run(db):
    from seo import tao_holder_queue as queue

    holders(db, (1, "a", 2))
    history(db, "a", COMPLETE, stamp="2026-11-05T12:00:00+00:00")
    history(db, "a", {"account": "unavailable"})
    assert queue.select(db, [1], 0, STAMP) == []


def test_unavailable_retries_at_24_hours_without_hiding_partial_history(db):
    from seo import tao_holder_queue as queue

    holders(db, (1, "unavailable", 2), (1, "skipped", 3),
            (1, "partial", 4), (1, "empty", 5))
    queue.record(db, {
        "unavailable": {"account": "unavailable"},
        "skipped": {**COMPLETE, "transactions": "skipped"},
        "partial": {**COMPLETE, "transactions": "partial",
                    "transactions_rows": 2},
        "empty": {**COMPLETE, "transactions_rows": 0},
    }, STAMP)
    assert queue.select(db, [1], 0, "2026-11-07T11:59:59Z") == []
    counts = queue.summary(db, [1], "2026-11-07T11:59:59Z")
    assert (counts["usable_history"], counts["retry_wait"],
            counts["pending"]) == (2, 2, 0)
    assert queue.select(db, [1], 0, "2026-11-07T12:00:00Z") == [
        "unavailable", "skipped"]


def test_failure_does_not_erase_a_previous_success(db):
    from seo import tao_holder_queue as queue

    holders(db, (1, "a", 2), (1, "b", 3))
    queue.record(db, {"a": COMPLETE, "b": {"account": "unavailable"}}, STAMP)
    queue.record(db, {"a": {"account": "unavailable"}, "b": COMPLETE},
                 "2026-11-07T12:00:00Z")
    assert queue.select(db, [1], 0, "2026-11-09T12:00:00Z") == []
    assert queue.summary(db, [1], STAMP)["usable_history"] == 2


def test_unreadable_partial_history_retries_after_delay(db):
    from types import SimpleNamespace

    from seo import tao_holder_queue as queue
    from seo.sources import tao_history

    address = "5" + "A" * 47
    state = {"status": "success", "data": {
        "data": [{"malformed": True}],
        "pagination": {"current_page": 1, "total_pages": 1}}}
    session = SimpleNamespace(sb=SimpleNamespace(evaluate=lambda _: state))
    events, status = tao_history._read_pages(
        session, "delegate", address, "transactions", 1)
    holders(db, (1, address, 2))
    queue.record(db, {address: {
        "account": "complete", "transactions": status,
        "transactions_rows": len(events)}}, STAMP)
    counts = queue.summary(db, [1], STAMP)
    assert counts["usable_history"] == 0
    assert counts["retry_wait"] == 1
    assert queue.select(db, [1], 0, "2026-11-07T12:00:00Z") == [address]


@pytest.mark.parametrize("rows", [None, 0])
def test_saved_partial_history_without_usable_rows_stays_pending(db, rows):
    from seo import tao_holder_queue as queue

    holders(db, (1, "partial", 2))
    coverage = {**COMPLETE, "transactions": "partial"}
    if rows is not None:
        coverage["transactions_rows"] = rows
    history(db, "partial", coverage)
    assert queue.summary(db, [1], STAMP)["usable_history"] == 0
    assert queue.select(db, [1], 0, STAMP) == ["partial"]


def test_screened_accounts_are_not_claimed_as_usable_history(db):
    from seo import tao_holder_queue as queue

    holders(db, (1, "large", 1), (1, "flagged", 2), (1, "missing", 3))
    queue.record(db, {
        "large": {"account": "complete", "transactions": "skipped",
                  "link_expansion": "screened"},
        "flagged": {"account": "complete", "transactions": "skipped",
                    "screened": True},
        "missing": {"account": "unavailable", "screened": True},
    }, STAMP)
    counts = queue.summary(db, [1], STAMP)
    assert (counts["usable_history"], counts["screened"],
            counts["retry_wait"], counts["pending"]) == (0, 2, 1, 0)
    assert queue.select(db, [1], 0, "2026-11-08T12:00:00Z") == ["missing"]


def test_older_database_without_registry_is_empty(db):
    from seo import tao_holder_queue as queue

    db.execute("DROP TABLE holder_registry")
    queue.schema(db)
    queue.schema(db)
    assert queue.select(db, [1], 0, STAMP) == []
    assert queue.summary(db, [1], STAMP)["registered"] == 0


def test_negative_limit_is_rejected(db):
    from seo import tao_holder_queue as queue

    with pytest.raises(ValueError, match="limit"):
        queue.select(db, [1], -1, STAMP)
