import sqlite3

from seo.tao_monitor import update


def test_failed_tab_preserves_cursor_and_reports_catchup_only_once():
    db = sqlite3.connect(":memory:")

    def day(n):
        return f"2026-10-{n:02}T12:00:00+00:00"

    def event(n):
        return dict(id=str(n), time=day(n), src="A", dst="B",
                    kind="tao_transfer")

    def check(n, status):
        return update(db, day(n), ["A"], [event(1), event(3)],
                      {"A": {"transactions": "complete", "transfers": status}})
    first = check(2, "complete")
    assert first["recent"] == []
    assert first["status"]["A"]["transfers"] == "baseline"
    assert check(3, "unavailable")["status"]["A"]["transfers"] == "gap"
    next_run = check(4, "complete")
    assert next_run["new_count"] == 1
    assert [e["id"] for e in next_run["recent"]] == ["3"]
    assert check(5, "complete")["new_count"] == 0
    late = dict(event(3), id="delayed")
    recovered = update(db, day(6), ["A"], [late],
                       {"A": {"transfers": "complete"}})
    assert recovered["new_count"] == 1
