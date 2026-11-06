"""Persistent discovery from verified, consecutive Taostats holder pages."""
import sqlite3

import pytest

from seo.sources import tao_holders


ADDRESSES = ["5" + c * 47 for c in "ABCDEFGHJK"]
STAMP = "2026-11-06T12:00:00+00:00"
LATER = "2026-11-07T12:00:00+00:00"


def row(index, rank=None, balance="100", netuid=23, hotkey="Z"):
    return {"netuid": netuid, "subnet_rank": rank or index + 1,
            "coldkey": {"ss58": ADDRESSES[index]},
            "hotkey": {"ss58": "5" + hotkey * 47}, "balance": balance}


def page(number, records, total_items=6, size=2):
    return {"status": "success", "data": {"data": records, "pagination": {
        "current_page": number,
        "total_pages": (total_items + size - 1) // size,
        "per_page": size, "total_items": total_items}}}


class Session:
    """Fake only the external browser; retain the production page reader."""
    def __init__(self, states, jump=False):
        self.sb = self
        self.states = states
        self.page = 1
        self.target = None
        self.jump = jump

    def pause(self):
        pass

    def goto(self, url, check_google=False):
        assert url == "https://taostats.io/subnets/23/chart"
        self.page = 1

    def evaluate(self, js):
        if js == tao_holders.SELECT_HOLDERS:
            return True
        if "const target =" in js:
            self.target = int(js.split("const target = ")[1].split(";")[0])
            if self.jump:
                return "jump"
            self.page = self.target
            return True
        if "HTMLInputElement.prototype" in js:
            return True
        if js == tao_holders.CONFIRM_JUMP:
            self.page = self.target
            return True
        return self.states.get(self.page, {"status": "error"})


@pytest.fixture(autouse=True)
def no_browser_wait(monkeypatch):
    monkeypatch.setattr(tao_holders.time, "sleep", lambda _: None)


@pytest.fixture
def conn():
    value = sqlite3.connect(":memory:")
    yield value
    value.close()


def checkpoints(conn):
    return conn.execute(
        "SELECT next_page, total_pages, total_items, page_size "
        "FROM holder_page_state WHERE netuid=23").fetchone()


def test_all_positive_rows_are_saved_beyond_the_old_six_wallet_sample(conn):
    from seo.sources.tao_holder_registry import collect_pages
    records = [row(i) for i in range(8)] + [row(8, balance="0")]
    result = collect_pages(Session({1: page(1, records, 9, 10)}),
                           conn, 23, STAMP, pages=1)
    assert result["captured"] == 8
    assert result["status"] == "traversed_live"
    assert result["pages_read"] == 1
    assert result["errors"] == []
    assert checkpoints(conn) == (2, 1, 9, 10)


def test_later_runs_resume_consecutive_pages_and_keep_the_completed_cursor(
        conn):
    from seo.sources.tao_holder_registry import collect_pages
    states = {1: page(1, [row(0), row(1)]),
              2: page(2, [row(2), row(3)]),
              3: page(3, [row(4), row(5)])}
    first = collect_pages(Session(states), conn, 23, STAMP, pages=1)
    assert (first["status"], first["next_page"], first["captured"]) == (
        "partial", 2, 2)
    second = collect_pages(Session(states, jump=True), conn, 23, LATER,
                           pages=2)
    assert (second["status"], second["next_page"], second["captured"]) == (
        "traversed_live", 4, 6)
    assert second["pages_read"] == 2
    assert conn.execute("SELECT last_seen FROM holder_registry "
                        "WHERE address=?", (ADDRESSES[0],)).fetchone() == (
                            STAMP,)
    third = collect_pages(Session(states), conn, 23, LATER, pages=2)
    assert third["status"] == "traversed_live"
    assert third["pages_read"] == 0
    assert checkpoints(conn) == (4, 3, 6, 2)


@pytest.mark.parametrize("bad_page", [
    page(2, [row(2)]),
    page(2, [row(2), row(3, netuid=99)]),
    page(1, [row(2), row(3)]),
    page(2, [row(2), {"netuid": 23}]),
    {"status": "error"},
])
def test_bad_resume_page_leaves_both_the_registry_and_cursor_unchanged(
        conn, bad_page):
    from seo.sources.tao_holder_registry import collect_pages
    bootstrap = page(1, [row(0), row(1)])
    collect_pages(Session({1: bootstrap}), conn, 23, STAMP, pages=1)
    result = collect_pages(Session({1: bootstrap, 2: bad_page}),
                           conn, 23, LATER, pages=1)
    assert checkpoints(conn) == (2, 3, 6, 2)
    assert result["captured"] == 2
    assert result["pages_read"] == 0
    assert result["status"] == "unavailable"
    assert result["errors"]


@pytest.mark.parametrize("mutation", [
    {"total_items": "bad"}, {"total_pages": 1}, {"per_page": 0},
    {"per_page": True}, {"total_pages": 3.0}, {"total_items": -1},
])
def test_invalid_pagination_never_creates_a_checkpoint(conn, mutation):
    from seo.sources.tao_holder_registry import collect_pages
    state = page(1, [row(0), row(1)])
    state["data"]["pagination"].update(mutation)
    result = collect_pages(Session({1: state}), conn, 23, STAMP, pages=1)
    assert result["status"] == "unavailable"
    assert result["captured"] == 0
    assert checkpoints(conn) is None


def test_distinct_hotkey_rows_are_combined_without_double_counting_duplicates(
        conn):
    from seo.sources.tao_holder_registry import collect_pages
    records = [row(0, balance="200"), row(0, balance="200"),
               row(0, rank=2, balance="300", hotkey="Y"), row(1, balance="0")]
    result = collect_pages(Session({1: page(1, records, 4, 4)}),
                           conn, 23, STAMP, pages=1)
    assert result["captured"] == 1
    assert conn.execute(
        "SELECT address,rank,balance_units,first_seen,last_seen "
        "FROM holder_registry").fetchall() == [
            (ADDRESSES[0], 1, "500", STAMP, STAMP)]


def test_verified_empty_grid_finishes_without_inventing_holders(conn):
    from seo.sources.tao_holder_registry import collect_pages
    result = collect_pages(Session({1: page(1, [], 0)}), conn, 23, STAMP)
    assert result["status"] == "traversed_live"
    assert result["captured"] == 0
    assert checkpoints(conn) == (1, 0, 0, 2)


def test_a_database_failure_rolls_back_the_whole_page(conn):
    from seo.sources.tao_holder_registry import collect_pages, schema
    schema(conn)
    conn.execute("CREATE TRIGGER reject_second "
                 "BEFORE INSERT ON holder_registry WHEN NEW.address='%s' "
                 "BEGIN SELECT RAISE(ABORT, 'private detail'); "
                 "END" % ADDRESSES[1])
    result = collect_pages(Session({1: page(1, [row(0), row(1)])}),
                           conn, 23, STAMP, pages=1)
    assert conn.execute("SELECT COUNT(*) FROM holder_registry").fetchone() == (
        0,)
    assert checkpoints(conn) is None
    assert result["status"] == "unavailable"
    assert "private detail" not in str(result)


def test_failed_page_preserves_uncommitted_writes_owned_by_the_caller(conn):
    from seo.sources.tao_holder_registry import collect_pages, schema
    schema(conn)
    conn.execute("CREATE TABLE preferences (address TEXT, muted INTEGER)")
    conn.execute("CREATE TRIGGER reject_holder "
                 "BEFORE INSERT ON holder_registry "
                 "BEGIN SELECT RAISE(ABORT, 'rejected'); END")
    conn.execute("INSERT INTO preferences VALUES (?,1)", (ADDRESSES[0],))
    collect_pages(Session({1: page(1, [row(0), row(1)])}),
                  conn, 23, STAMP, pages=1)
    assert conn.execute(
        "SELECT address,muted FROM preferences").fetchall() == [
            (ADDRESSES[0], 1)]


def test_growth_rechecks_the_previous_partial_last_page_without_a_restart(
        conn):
    from seo.sources.tao_holder_registry import collect_pages
    initial = {1: page(1, [row(0), row(1)], 3), 2: page(2, [row(2)], 3)}
    collect_pages(Session(initial), conn, 23, STAMP, pages=2)
    grown = {1: page(1, [row(0), row(1)], 5),
             2: page(2, [row(2), row(3)], 5), 3: page(3, [row(4)], 5)}
    result = collect_pages(Session(grown), conn, 23, LATER, pages=2)
    assert result["captured"] == 5
    assert result["pages_read"] == 2
    assert result["next_page"] == 4
    assert result["status"] == "traversed_live"
    assert conn.execute("SELECT last_seen FROM holder_registry "
                        "WHERE address=?", (ADDRESSES[0],)).fetchone() == (
                            STAMP,)


def test_shrinking_pagination_does_not_turn_an_incomplete_pass_into_success(
        conn):
    from seo.sources.tao_holder_registry import collect_pages
    collect_pages(Session({1: page(1, [row(0), row(1)])}),
                  conn, 23, STAMP, pages=1)
    result = collect_pages(Session({1: page(1, [row(0), row(1)], 2)}),
                           conn, 23, LATER, pages=1)
    assert result["status"] == "unavailable"
    assert result["errors"]
    assert checkpoints(conn) == (2, 3, 6, 2)


def test_totals_changing_mid_run_do_not_advance_past_the_last_verified_page(
        conn):
    from seo.sources.tao_holder_registry import collect_pages
    states = {1: page(1, [row(0), row(1)]),
              2: page(2, [row(2), row(3)], 8)}
    result = collect_pages(Session(states), conn, 23, STAMP, pages=2)
    assert result["status"] == "partial"
    assert result["captured"] == 2
    assert result["pages_read"] == 1
    assert checkpoints(conn) == (2, 3, 6, 2)


@pytest.mark.parametrize("budget", [0, 101, -1, True, 1.5])
def test_page_budget_is_bounded(conn, budget):
    from seo.sources.tao_holder_registry import collect_pages
    with pytest.raises(ValueError):
        collect_pages(Session({}), conn, 23, STAMP, pages=budget)
