"""Match historical marks to the right subnet and observation time."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
import sqlite3
from seo.sources import taostats_wallets

STAMP = "2021-11-05T00:00:00+00:00"


def payload(pools):
    query = {"dehydratedAt": 1, "queryKey": ["dtaoSubnetPools", {}],
             "state": {"status": "success", "data": {"data": pools}}}
    return "self.__next_f.push([1,%s])" % json.dumps(json.dumps(query))


def pool(net, registration, marks):
    return {"netuid": net, "registration_timestamp": registration,
            "seven_day_prices": [dict(timestamp=t, price=p, block_number=b)
                                 for b, t, p in marks]}


class Browser:
    def __init__(self, pools):
        self.pools = pools
        self.visits = []

    def html(self):
        return payload(self.pools)

    def url(self):
        return taostats_wallets.BASE + "/subnets"

    def goto(self, url, check_google):
        self.visits.append(url)

    def pause(self):
        pass


def test_exact_followup_mark_and_missing_market_history():
    from seo.sources.tao_prices import refresh_history
    db = SimpleNamespace(conn=sqlite3.connect(":memory:"))
    q = pool(23, "2020-01-01T00:00:00Z", [
        (10, "2021-11-04T23:59:00Z", "9"), (11, STAMP, "1.2"),
        (12, "2021-11-05T02:00:00Z", "1.1"),
        (13, "2021-11-05T03:00:00Z", "NaN")])
    buy = dict(kind="buy", dst="A", src=None, time="2021-11-04T00:00:00Z",
               id="fill", netuid=23, tao=1.0, alpha=1.0)
    sell = dict(buy, kind="sell", src="B", dst=None, id="sale", netuid=7)
    quotes, info = refresh_history(db, Browser([q]), [buy, sell])
    assert len(quotes) == 3 and info["matched_trades"] == 1
    mark = info["matches"][0]
    assert mark["id"] == "fill" and mark["quote_time"] == STAMP
    assert mark["price_tao"] == 1.2 and mark["hours_after"] == 24
    quotes, _ = refresh_history(db, Browser([q]), [])
    assert len(quotes) == 3


def test_new_registration_does_not_join_to_old_trades():
    from seo.sources.tao_prices import refresh_history
    db = SimpleNamespace(conn=sqlite3.connect(":memory:"))
    old = pool(23, "2019-01-01T00:00:00Z", [(9, "2021-11-04T12:00:00Z",
                                            "0.05")])
    new = pool(23, "2021-11-04T23:00:00Z", [(9, "2021-11-04T12:00:00Z",
                                             "0.05"), (11, STAMP, "1")])
    refresh_history(db, Browser([old]), [])
    trade = dict(kind="buy", id="old", time="2021-11-04T00:00:00Z",
                 dst="A", src=None, netuid=23, tao=1.0, alpha=1.0)
    quotes, info = refresh_history(db, Browser([new]), [trade])
    assert len(quotes) == 1 and quotes[0]["block_number"] == 11
    assert info["matched_trades"] == 0


def test_chart_open_uses_positive_volume_tao_hourly_bars():
    from seo.sources.tao_prices import chart_marks
    observed = datetime(2021, 5, 1, tzinfo=timezone.utc)
    t = int(observed.timestamp())
    data = {"s": "ok", "t": [t, t + 3600], "o": [.33, .27],
            "c": [.38, .29], "v": [4, 0]}
    marks = chart_marks(23, "2019-01-01T00:00:00+00:00",
                        {"symbol": "SUB-23", "resolution": "60"}, data)
    assert [(r["fetched_at"], r["price_tao"]) for r in marks] == [
        (observed.isoformat(), .33)]
    assert chart_marks(23, STAMP, {"symbol": "USD", "resolution": "60"},
                       data) == []


def test_combined_timing_uses_archived_marks_with_real_time_and_generation():
    from seo.sources.tao_prices import timing_evidence
    price = dict(netuid=23, fetched_at=STAMP, price_tao=1., source="pool",
                 registered_at="2021-11-04T00:00:00+00:00", block_number=10)
    rows = [price, dict(netuid=23, fetched_at="2021-11-05T00:30:00Z",
                        price_tao=2),
            dict(netuid=23, fetched_at="2021-11-05T01:00:00Z", price_tao=7)]
    trade = dict(kind="sell", id="old", time="2021-11-03T23:30:00Z",
                 dst=None, src="B", netuid=23, tao=1.0, alpha=1.0)
    events = [trade, dict(trade, id="flat", time="2021-11-04T00:00:00Z"),
              dict(trade, id="later", time="2021-11-04T00:10:00Z"),
              dict(trade, id="future", time="2021-11-06T00:10:00Z")]
    proof = timing_evidence(events, rows, "2021-11-05T00:40:00Z")
    assert proof["matched_trades"] == 2 and proof["eligible_trades"] == 3
    by_id = {e["id"]: e for e in proof["matches"]}
    assert by_id["later"]["quote_source"] == "subnet_snapshot"
    assert by_id["later"]["price_tao"] == 2 and by_id["later"][
        "favorable_move_pct"] == -100
    assert by_id["flat"]["market_move"] == "flat"
    assert proof["market_moves"]["sell"] == {"flat": 1, "rise": 1}
