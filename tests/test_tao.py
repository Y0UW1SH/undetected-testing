"""taostats.io parser, alerts and the `tao` CLI (offline)."""
import json
import os

from seo import storage
from seo import tao_cli
from seo.sources import taostats
from tests import fakes
from tests.fakes import RecordingSession


def run(fake_env, *argv):
    return tao_cli.main(["--db", fake_env["db"]] + list(argv))


def test_parse_subnets_columns(fixture):
    rows = taostats.parse_subnets(fixture("taostats_subnets.html"))
    assert [r.netuid for r in rows] == [1, 3, 8, 19, 64]     # sorted, deduped
    by = {r.netuid: r for r in rows}
    apex = by[1]
    assert apex.name == "Apex"
    assert apex.price_tao == 0.0423
    assert apex.change_24h_pct == 12.4
    assert apex.change_7d_pct == -3.1                        # unicode minus
    assert apex.market_cap_tao == 1_200_000.0
    assert apex.emission_pct == 4.81
    assert apex.volume_24h_tao == 15_300.0
    assert apex.tao_in_pool == 48_210.0
    assert apex.source_url == taostats.SUBNETS_URL
    assert json.loads(apex.raw)["Price"] == "τ0.0423"
    # netuid embedded in the name, with and without a netuid cell
    assert by[8].name == "Proprietary Trading Network"
    assert by[19].name == "Nineteen" and by[19].price_tao == 0.0151
    # dashes are None, first duplicate wins
    assert by[3].change_24h_pct is None and by[3].volume_24h_tao is None
    assert by[64].name == "Chutes" and by[64].price_tao == 0.221


def test_parse_handles_role_grid_and_price_usd():
    html = """<div role="table">
      <div role="row"><span role="columnheader">#</span>
        <span role="columnheader">Subnet</span>
        <span role="columnheader">Price (USD)</span>
        <span role="columnheader">Price</span>
        <span role="columnheader">1d %</span></div>
      <div role="row"><span role="cell">5</span>
        <span role="cell">Open Kaito</span>
        <span role="cell">$12.50</span><span role="cell">τ0.05</span>
        <span role="cell">-4%</span></div>
    </div>"""
    rows = taostats.parse_subnets(html, "u")
    assert len(rows) == 1
    r = rows[0]
    assert (r.netuid, r.name) == (5, "Open Kaito")
    assert r.price_usd == 12.5 and r.price_tao == 0.05
    assert r.change_24h_pct == -4.0


def test_parse_ignores_pages_without_a_price_table():
    assert taostats.parse_subnets("<table><tr><th>a</th></tr></table>") == []
    assert taostats.parse_subnets("<html></html>") == []


def test_parse_number():
    p = taostats.parse_number
    assert p("τ0.0423") == 0.0423 and p("+12.4%") == 12.4
    assert p("−3.1%") == -3.1 and p("τ1.2M") == 1_200_000.0
    assert p("$4,201") == 4201.0 and p("0.5B") == 500_000_000.0
    assert p("-") is None and p(None) is None and p("n/a") is None


def test_is_blocked():
    assert taostats.is_blocked("u", "Just a moment...", "") is not None
    assert taostats.is_blocked("u", "Subnets | Taostats", "<html>ok") is None
    assert taostats.is_blocked("u", "", "<div id='cf-chl-widget'>") is not None


def test_build_url():
    assert taostats.build_url() == "https://taostats.io/subnets"
    assert taostats.build_url(8) == "https://taostats.io/subnets/8"


def _row(netuid, price, mcap=1000.0, name="x"):
    return {"netuid": netuid, "name": name, "price_tao": price,
            "market_cap_tao": mcap}


def test_price_alerts():
    prev = [_row(1, 1.0), _row(2, 1.0), _row(3, 1.0, mcap=1000.0),
            _row(4, 1.0)]
    latest = [_row(1, 1.2), _row(2, 0.85), _row(3, 1.0, mcap=1300.0),
              _row(5, 2.0)]
    alerts = tao_cli.price_alerts(latest, prev, threshold=10)
    kinds = {(a["netuid"], a["kind"]) for a in alerts}
    assert kinds == {(1, "price_up"), (2, "price_down"), (3, "mcap_up"),
                     (4, "gone"), (5, "new")}
    up = [a for a in alerts if a["kind"] == "price_up"][0]
    assert up["change_pct"] == 20.0 and up["old"] == 1.0 and up["new"] == 1.2
    # below threshold -> nothing; netuid filter
    assert tao_cli.price_alerts([_row(1, 1.05)], [_row(1, 1.0)], 10) == []
    only = tao_cli.price_alerts(latest, prev, 10, netuids=[2])
    assert [a["netuid"] for a in only] == [2]
    # first run: no "new" alerts for everything
    assert tao_cli.price_alerts(latest, [], 10) == []
    assert "up 20.0%" in tao_cli.format_alert(up)


def test_fetch_subnets_merges_scroll_rounds(fake_env):
    with RecordingSession() as s:
        rows = taostats.fetch_subnets(s, max_rounds=2)
    assert [r.netuid for r in rows] == [1, 3, 8, 19, 64]
    assert s.fake.visited == [taostats.SUBNETS_URL]


def test_subnets_command_and_cache(fake_env, capsys):
    assert run(fake_env, "subnets") == 0
    out = capsys.readouterr().out
    assert "Saved 5 subnets." in out and "Proprietary Trading Network" in out
    assert run(fake_env, "subnets") == 0           # fetched today -> cached
    assert "Already fetched today" in capsys.readouterr().out
    assert len(RecordingSession.created) == 1
    store = storage.Storage(fake_env["db"])
    rows = store.rows("subnet_stats")
    assert len(rows) == 5 and len({r["fetched_at"] for r in rows}) == 1
    store.close()
    assert run(fake_env, "subnet", "8", "--history", "3") == 0
    out = capsys.readouterr().out
    assert "SN8 Proprietary Trading Network" in out and "0.0987" in out
    assert run(fake_env, "subnet", "77") == 1


def test_watch_alerts_between_runs(fake_env, monkeypatch, capsys):
    assert run(fake_env, "watch", "--threshold", "10") == 0
    out = capsys.readouterr().out
    assert "First run recorded" in out
    original = fakes.load_fixture

    def moved(name):
        html = original(name)
        return (html.replace("τ0.0423", "τ0.0600")      # SN1 +41.8%
                    .replace("τ0.0987", "τ0.0950")      # SN8 -3.7% (quiet)
                    .replace("τ3.4M", "τ2.0M"))         # SN8 mcap -41%
    monkeypatch.setattr(fakes, "load_fixture", moved)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    code = run(fake_env, "watch", "--threshold", "10", "--fail-on-alert")
    out = capsys.readouterr().out
    assert code == 2
    assert "WARNING SN1 Apex price up 41.8%" in out
    assert "WARNING SN8 Proprietary Trading Network market cap down 41.2%" \
        in out
    assert "::warning::SN1 Apex price up" in out
    assert "SN8 Proprietary Trading Network price" not in out
    alerts = json.load(open(os.path.join("data", "tao_alerts.json")))
    assert alerts["threshold_pct"] == 10.0
    assert {a["kind"] for a in alerts["alerts"]} == {"price_up", "mcap_down"}
    # netuid filter via --netuids
    code = run(fake_env, "watch", "--threshold", "10", "--netuids", "8")
    out = capsys.readouterr().out
    assert code == 0 and "No moves beyond" in out


def test_watch_reads_config(fake_env, capsys):
    assert run(fake_env, "init", "--config", "tao.json", "--netuids", "1,64",
               "--threshold", "5") == 0
    cfg = json.load(open("tao.json"))
    assert cfg == {"netuids": [1, 64], "alert_threshold_pct": 5.0}
    assert run(fake_env, "init", "--config", "tao.json") == 1
    assert run(fake_env, "watch", "--config", "tao.json") == 0
    out = capsys.readouterr().out
    assert "Apex" in out and "Chutes" in out and "Templar" not in out
    assert json.load(open("data/tao_alerts.json"))["threshold_pct"] == 5.0


def test_report_and_export(fake_env, monkeypatch, capsys):
    assert run(fake_env, "report", "--out", "data/r.html") == 0
    assert "No data yet" in open("data/r.html", encoding="utf-8").read()
    run(fake_env, "watch")
    original = fakes.load_fixture
    monkeypatch.setattr(fakes, "load_fixture",
                        lambda n: original(n).replace("τ0.0423", "τ0.0300"))
    run(fake_env, "watch")
    capsys.readouterr()
    assert run(fake_env, "report", "--out", "data/r.html",
               "--threshold", "10") == 0
    html = open("data/r.html", encoding="utf-8").read()
    assert "<title>Bittensor subnets</title>" in html
    assert "Apex price down 29.1%" in html
    assert "class='down'>-29.1%" in html
    assert "Proprietary Trading Network" in html
    assert run(fake_env, "export", "--format", "json", "--dir", "out") == 0
    assert os.listdir("out") == ["subnet_stats.json"]


def test_live_taostats_fixture(fixture):
    """Captured from taostats.io/subnets via GitHub Actions: an AG Grid
    with pinned-left name cells ("lium.io SN51") and col-id data cells."""
    from seo.sources import taostats
    rows = taostats.parse_subnets(fixture("live_taostats_subnets.html"))
    assert len(rows) == 25                       # one virtualised page
    assert len({r.netuid for r in rows}) == 25
    by = {r.netuid: r for r in rows}
    sn51 = by[51]
    assert sn51.name == "lium.io"
    assert sn51.price_tao == 0.118788
    assert sn51.change_24h_pct == 2.71 and sn51.change_7d_pct == -4.88
    assert sn51.market_cap_tao == 603050
    assert sn51.emission_pct == 16.13
    assert sn51.volume_24h_tao == 6550
    assert sn51.tao_in_pool == 376820
    assert '"1H"' in sn51.raw and "0.12%" in sn51.raw
    assert by[64].name == "Chutes" and by[4].name == "Targon"
    assert all(r.price_tao is not None for r in rows)


def test_fetch_subnets_pages_through_the_grid(fixture):
    """Two fake pages: the Next Page button is 'clicked' once, then
    reported 'disabled'; rows from both pages are merged by netuid."""
    from seo.browser import Session
    from seo.sources import taostats
    from tests.fakes import FakeBrowser

    page1 = fixture("live_taostats_subnets.html")
    page2 = page1.replace('row-id="row-51"', 'row-id="row-999"').replace(
        "lium.io SN51", "Testnet SN999")

    class PagingBrowser(FakeBrowser):
        def __init__(self):
            super().__init__()
            self.clicks = 0

        def get_page_source(self):
            return page2 if self.clicks else page1

        def evaluate(self, js):
            if "Next" in js:
                if self.clicks == 0:
                    self.clicks += 1
                    return "clicked"
                return "disabled"
            return 0

    browser = PagingBrowser()
    with Session(browser=browser, min_pause=0, max_pause=0) as s:
        s.pause = lambda scale=1.0: None
        rows = taostats.fetch_subnets(s)
    assert browser.clicks == 1
    netuids = [r.netuid for r in rows]
    assert 51 in netuids and 999 in netuids
    assert len(rows) == 26                 # 25 from page 1 + 1 new row
    assert netuids == sorted(netuids)
