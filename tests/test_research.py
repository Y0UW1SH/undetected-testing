"""Product research tool: Amazon/eBay parsers, alerts, and the CLI."""
import json
import os

from seo import research_cli, storage
from seo.sources import amazon, ebay
from tests.fakes import RecordingSession


def run(fake_env, *argv):
    return research_cli.main(["--db", fake_env["db"]] + list(argv))


# ---- parsers ---------------------------------------------------------------
def test_amazon_parse(fixture):
    items = amazon.parse(fixture("amazon_search.html"), "wireless mouse")
    assert [i.position for i in items] == [1, 2, 3, 4]     # empty ASIN skipped
    assert [i.item_id for i in items] == ["B0SPONSOR1", "B0ORGANIC1",
                                          "B0NOPRICE1", "B0CHEAP001"]
    first = items[0]
    assert first.source == "amazon" and first.query == "wireless mouse"
    assert first.title.startswith("Logitech M510")
    assert (first.price, first.currency, first.price_text) == (
        24.99, "USD", "$24.99")
    assert (first.rating, first.reviews) == (4.5, 12345)
    assert first.is_sponsored is True and first.prime is True
    assert first.url == "https://www.amazon.com/dp/B0SPONSOR1"
    second = items[1]
    assert second.is_sponsored is False and second.prime is False
    assert second.price == 19.99                 # not the struck-through one
    assert (second.rating, second.reviews) == (4.3, 1234)
    assert items[2].price is None and items[2].rating is None
    assert items[3].reviews == 876 and items[3].price == 9.49
    assert all(i.page == 1 for i in items)


def test_amazon_build_url_and_blocked():
    assert amazon.build_url("wireless mouse") == (
        "https://www.amazon.com/s?k=wireless+mouse")
    assert amazon.build_url("x", page=2, domain="amazon.co.uk") == (
        "https://www.amazon.co.uk/s?k=x&page=2")
    assert amazon.is_blocked("u", "Robot Check", "")
    assert amazon.is_blocked("u", "Amazon.com : x",
                             '<form action="/errors/validateCaptcha">')
    assert amazon.is_blocked("u", "Amazon.com : x", "<html>ok</html>") is None


def test_ebay_parse(fixture):
    items = ebay.parse(fixture("ebay_search.html"), "wireless mouse")
    # placeholder skipped, duplicate item id skipped
    assert [i.item_id for i in items] == ["123456789012", "234567890123",
                                          "345678901234"]
    assert [i.position for i in items] == [1, 2, 3]
    first = items[0]
    assert first.title == "Logitech M510 Wireless Laser Mouse - Black"
    assert (first.price, first.currency) == (18.49, "USD")
    assert (first.rating, first.reviews) == (4.5, 1234)
    assert first.condition == "Brand New" and first.shipping == "Free shipping"
    assert first.is_sponsored is True
    assert first.url == "https://www.ebay.com/itm/123456789012"
    second = items[1]
    assert second.price == 12.0 and second.price_text == "$12.00 to $15.00"
    assert second.condition == "Pre-Owned"
    assert second.shipping == "+$4.99 shipping"
    assert second.is_sponsored is False and second.rating is None
    assert items[2].price is None and items[2].condition == "Parts Only"


def test_ebay_build_url_and_blocked():
    assert ebay.build_url("wireless mouse") == (
        "https://www.ebay.com/sch/i.html?_nkw=wireless+mouse")
    assert ebay.build_url("x", page=3, site="www.ebay.co.uk") == (
        "https://www.ebay.co.uk/sch/i.html?_nkw=x&_pgn=3")
    assert ebay.is_blocked("u", "Pardon Our Interruption", "")
    assert ebay.is_blocked("https://www.ebay.com/splashui/challenge?x",
                           "eBay", "")
    assert ebay.is_blocked("u", "mouse | eBay", "ok") is None


# ---- alerts ----------------------------------------------------------------
def _row(item_id, price, position=1, title=None):
    return {"source": "amazon", "query": "q", "item_id": item_id,
            "title": title or "Item " + item_id, "price": price,
            "position": position}


def test_price_alerts():
    previous = [_row("A", 10.0, 1), _row("B", 10.5, 2), _row("C", 5.0, 3),
                _row("E", None, 4)]
    latest = [_row("A", 20.0, 1), _row("B", 10.0, 2), _row("D", 7.0, 3),
              _row("E", 3.0, 4)]
    alerts = research_cli.price_alerts(latest, previous, threshold=10)
    kinds = [(a["kind"], a["item_id"]) for a in alerts]
    assert ("price_up", "A") in kinds
    assert ("price_down", "B") not in kinds        # -4.8% is under 10%
    assert ("gone", "C") in kinds and ("new", "D") in kinds
    assert not any(a["item_id"] == "E" for a in alerts)   # no old price
    up = [a for a in alerts if a["item_id"] == "A"][0]
    assert (up["old_price"], up["new_price"], up["change_pct"]) == (
        10.0, 20.0, 100.0)
    assert up["source"] == "amazon" and up["query"] == "q"
    # lower threshold catches B going down
    alerts = research_cli.price_alerts(latest, previous, threshold=2)
    assert ("price_down", "B") in [(a["kind"], a["item_id"]) for a in alerts]
    assert alerts[0]["kind"] == "price_down"       # sorted: drops first
    # first ever run: nothing to compare, nothing is "new"
    assert research_cli.price_alerts(latest, [], threshold=10) == []
    assert research_cli.price_alerts([], [], threshold=10) == []
    assert "up A" in research_cli.format_alert(up)


# ---- CLI --------------------------------------------------------------------
def test_amazon_and_ebay_commands(fake_env, capsys):
    assert run(fake_env, "amazon", "wireless mouse") == 0
    assert run(fake_env, "ebay", "wireless mouse") == 0
    urls = [u for s in RecordingSession.created for u in s.fake.visited]
    assert any("amazon.com/s?k=wireless+mouse" in u for u in urls)
    assert any("ebay.com/sch/i.html?_nkw=wireless+mouse" in u for u in urls)
    out = capsys.readouterr().out
    assert "Amazon results" in out and "[sponsored]" in out
    assert "Ebay results" in out and "$18.49" in out
    store = storage.Storage(fake_env["db"])
    rows = store.rows("listings")
    assert {r["source"] for r in rows} == {"amazon", "ebay"}
    assert len(rows) == 7
    store.close()
    # already fetched today: no new browser
    RecordingSession.created = []
    assert run(fake_env, "amazon", "wireless mouse") == 0
    assert RecordingSession.created == []
    assert "already fetched today" in capsys.readouterr().out
    assert run(fake_env, "amazon", "wireless mouse", "--force") == 0
    assert len(RecordingSession.created) == 1


def test_init_track_alerts_report_export(fake_env, capsys):
    assert run(fake_env, "track", "--config", "cfg.json") == 2   # no config
    assert "research init" in capsys.readouterr().err
    assert run(fake_env, "init", "--config", "cfg.json") == 0     # example
    with open("cfg.json") as fh:
        assert json.load(fh)["amazon"]
    assert run(fake_env, "init", "--config", "cfg.json") == 1     # exists
    assert run(fake_env, "init", "--config", "cfg.json", "--force",
               "--amazon", "wireless mouse", "--amazon", "usb hub",
               "--ebay", "wireless mouse", "--threshold", "5") == 0
    with open("cfg.json") as fh:
        cfg = json.load(fh)
    assert cfg == {"amazon": ["wireless mouse", "usb hub"],
                   "ebay": ["wireless mouse"], "pages": 1,
                   "alert_threshold_pct": 5.0}

    assert run(fake_env, "track", "--config", "cfg.json",
               "--per-session", "5") == 0
    # one session per source, 2 amazon queries in one, 1 ebay in the other
    visited = [len(s.fake.visited) for s in RecordingSession.created]
    assert visited == [2, 1]
    out = capsys.readouterr().out
    assert "No price alerts" in out and '"done": 3' in out
    assert os.path.exists("data/research_alerts.json")

    # second run today is skipped entirely
    RecordingSession.created = []
    assert run(fake_env, "track", "--config", "cfg.json") == 0
    assert RecordingSession.created == []
    assert '"skipped": 3' in capsys.readouterr().out

    # fake an older run with different prices, then alerts fire
    store = storage.Storage(fake_env["db"])
    for row in store.rows("listings", source="amazon",
                          query="wireless mouse"):
        old_price = (row["price"] or 0) * 2 if row["price"] else None
        store.conn.execute(
            "INSERT INTO listings (source, query, position, title, url, "
            "item_id, price, currency, price_text, is_sponsored, page, "
            "fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["source"], row["query"], row["position"], row["title"],
             row["url"], row["item_id"], old_price, row["currency"],
             row["price_text"], row["is_sponsored"], 1,
             "2026-01-01T00:00:00+00:00"))
    store.conn.execute(
        "INSERT INTO listings (source, query, position, title, item_id, "
        "price, is_sponsored, page, fetched_at) VALUES "
        "('amazon','wireless mouse',9,'Gone Mouse','B0GONE0001',5.0,0,1,"
        "'2026-01-01T00:00:00+00:00')")
    store.conn.commit()
    store.close()
    assert run(fake_env, "alerts", "--threshold", "10") == 0
    out = capsys.readouterr().out
    assert "price_down" not in out            # formatted as 'down'
    assert "down B0SPONSOR1" in out and "(-50.0%)" in out
    assert "gone B0GONE0001" in out
    with open("data/research_alerts.json") as fh:
        payload = json.load(fh)
    kinds = {a["kind"] for a in payload["alerts"]}
    assert kinds == {"price_down", "gone"}

    assert run(fake_env, "report", "--out", "data/research_report.html") == 0
    html = open("data/research_report.html", encoding="utf-8").read()
    assert "<title>Product research</title>" in html
    assert "Amazon: wireless mouse" in html and "Ebay: wireless mouse" in html
    assert "Price alerts" in html and "price_down" in html
    assert "Logitech M510" in html and "(sponsored)" in html
    assert "-50.0%" in html

    assert run(fake_env, "export", "--format", "csv", "--dir", "out") == 0
    assert os.path.exists("out/research_listings.csv")
    assert run(fake_env, "export", "--format", "json", "--dir", "out") == 0
    assert os.path.exists("out/research_listings.json")


def test_track_skip_mode_survives_a_challenge(fake_env, monkeypatch, capsys):
    from seo.models import ChallengeError

    def blocked(session, query, pages=1, domain="amazon.com"):
        raise ChallengeError("Robot Check")

    monkeypatch.setattr(amazon, "fetch", blocked)
    with open("cfg.json", "w") as fh:
        json.dump({"amazon": ["a", "b"], "ebay": ["c"]}, fh)
    assert run(fake_env, "track", "--config", "cfg.json",
               "--on-challenge", "skip") == 0        # ebay still ran
    out = capsys.readouterr()
    assert '"challenged_jobs": 2' in out.out and '"done": 1' in out.out
    assert run(fake_env, "track", "--config", "cfg.json", "--force") == 3
    assert "CHALLENGED" in capsys.readouterr().err


def test_github_actions_warning_lines(fake_env, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    research_cli.print_alerts([{
        "kind": "price_up", "source": "ebay", "query": "q", "item_id": "1",
        "title": "T", "position": 1, "old_price": 1.0, "new_price": 2.0,
        "change_pct": 100.0}])
    out = capsys.readouterr().out
    assert "::warning::ebay 'q': up 1 1.00 -> 2.00 (+100.0%) T" in out


def test_live_amazon_fixture(fixture):
    """Captured from amazon.com via GitHub Actions (result blocks only)."""
    from seo.sources import amazon
    items = amazon.parse(fixture("live_amazon_search.html"), "wireless mouse")
    assert len(items) == 16
    first = items[0]
    assert first.item_id == "B004YAVF8I"
    assert first.title.startswith("Logitech M185")
    assert (first.price, first.currency) == (12.99, "USD")
    assert (first.rating, first.reviews) == (4.4, 45671)
    assert first.url == "https://www.amazon.com/dp/B004YAVF8I"
    assert all(i.price is not None for i in items)
    assert all(i.rating is not None for i in items)
    assert [i.position for i in items] == list(range(1, 17))
    assert amazon.is_blocked("", "Amazon.com : wireless mouse", "") is None
