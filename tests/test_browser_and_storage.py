import os

from seo import canary, models, storage
from seo.browser import Session, is_challenge_page
from seo.models import ChallengeError
from tests.fakes import FakeBrowser


def test_is_challenge_page(fixture):
    assert is_challenge_page("https://www.google.com/sorry/index?x", "", "")
    assert is_challenge_page("https://www.google.com/search?q=a", "",
                             fixture("google_sorry.html"))
    assert not is_challenge_page("https://www.google.com/search?q=a",
                                 "a - Google Search", "<html>ok</html>")
    assert not is_challenge_page(
        "https://consent.google.com/m?continue=x", "Before you continue",
        "<html></html>")
    # the real thing, captured from a datacenter IP
    assert is_challenge_page("https://www.google.com/search?q=a", "",
                             fixture("live_google_sorry.html"))


def test_session_raises_on_persistent_challenge(fixture):
    sorry = fixture("google_sorry.html")
    fake = FakeBrowser({"https://www.google.com/search?q=x": ("", sorry)})
    s = Session(browser=fake, min_pause=0, max_pause=0)
    s.pause = lambda scale=1.0: None
    try:
        s.goto("https://www.google.com/search?q=x")
    except ChallengeError:
        pass
    else:
        raise AssertionError("expected ChallengeError")
    assert fake.solved == 1
    s.close()
    assert fake.quit_called


def test_session_turns_sys_exit_into_setup_error(monkeypatch):
    import sys
    import types

    from seo.models import MissingDependency

    def chrome(**kw):
        raise SystemExit("NOTE: You must install tkinter on Linux")

    fake_pkg = types.ModuleType("seleniumbase")
    fake_pkg.sb_cdp = types.SimpleNamespace(Chrome=chrome)
    monkeypatch.setitem(sys.modules, "seleniumbase", fake_pkg)
    try:
        Session().open()
    except MissingDependency as e:
        assert "python3-tk" in str(e) and "--headless" in str(e)
    else:
        raise AssertionError("expected MissingDependency")

    def boom(**kw):
        raise RuntimeError("no display")

    fake_pkg.sb_cdp = types.SimpleNamespace(Chrome=boom)
    try:
        Session().open()
    except MissingDependency as e:
        assert "no display" in str(e)
    else:
        raise AssertionError("expected MissingDependency")


def test_session_passes_clean_page():
    fake = FakeBrowser({"https://www.google.com/search?q=x": (
        "x - Google Search", "<html>results</html>")})
    with Session(browser=fake, min_pause=0, max_pause=0) as s:
        s.goto("https://www.google.com/search?q=x")
        assert s.pages_loaded == 1
        assert s.html() == "<html>results</html>"


def test_storage_roundtrip_and_export(tmp_path):
    db = os.path.join(tmp_path, "t.sqlite")
    store = storage.Storage(db)
    rec = models.SerpResult("q", "Austin, TX", "desktop", 1, "organic",
                            "https://a.example/", "a.example", "A",
                            snippet="s")
    assert store.save([rec]) == 1
    assert store.fetched_today("serp_results", query="q",
                               location="Austin, TX", device="desktop")
    assert not store.fetched_today("serp_results", query="other")
    rows = store.rows("serp_results", query="q")
    assert rows[0]["domain"] == "a.example"
    assert store.cached_geocode("Austin, TX") is None
    store.cache_geocode("Austin, TX", 30.2, -97.7, "Austin",
                        "Austin,Texas,United States")
    assert store.cached_geocode("austin, tx") == (
        30.2, -97.7, "Austin", "Austin,Texas,United States")
    csv_path = store.export("serp_results", str(tmp_path), "csv")
    assert open(csv_path).read().splitlines()[0].startswith("id,query,")
    json_path = store.export("serp_results", str(tmp_path), "json")
    assert '"domain": "a.example"' in open(json_path).read()
    # bool coercion
    store.save([models.LocalPackEntry("q", "", "desktop", 1, "n",
                                      is_ad=True)])
    assert store.rows("local_pack")[0]["is_ad"] == 1
    store.close()


def test_latest_runs_diff(tmp_path):
    store = storage.Storage(os.path.join(tmp_path, "t.sqlite"))
    old = models.Business("q", "L", 1, "Joe", reviews=10,
                          fetched_at="2026-01-01T00:00:00+00:00")
    new = models.Business("q", "L", 1, "Joe", reviews=12,
                          fetched_at="2026-01-02T00:00:00+00:00")
    store.save([old, new])
    latest, previous = store.latest_runs("businesses", ("name",), query="q")
    assert latest[0]["reviews"] == 12 and previous[0]["reviews"] == 10
    # A stamped save overrides per-record timestamps so one run = one batch
    a = models.Business("q", "L", 1, "A", fetched_at="2026-01-03T00:00:00")
    b = models.Business("q", "L", 2, "B", fetched_at="2026-01-03T00:00:09")
    store.save([a, b], stamp="2026-01-03T00:00:00+00:00")
    latest, previous = store.latest_runs("businesses", ("name",), query="q")
    assert sorted(r["name"] for r in latest) == ["A", "B"]
    assert previous[0]["reviews"] == 12
    store.close()


def test_canary_summary_and_run_with_fake_sessions():
    class FakeSession:
        calls = []

        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def goto(self, url):
            FakeSession.calls.append(url)
            if "pizza" in url:
                raise ChallengeError("blocked")

        def title(self):
            return "ok - Google Search"

    records = canary.run(["a", "best pizza", "c"], per_session=1,
                         session_factory=FakeSession)
    assert [r.outcome for r in records] == ["ok", "challenge", "ok"]
    assert canary.summarize(records) == {
        "total": 3, "attempted": 3, "challenge_rate_pct": 33.3,
        "ok": 2, "challenge": 1}
    # A challenge abandons the rest of a multi-query session: those
    # queries are recorded as skipped, not silently dropped, and the
    # rate is computed over attempted queries only.
    FakeSession.calls = []
    records = canary.run(["a", "best pizza", "c", "d", "e"],
                         per_session=5, session_factory=FakeSession)
    assert [r.outcome for r in records] == [
        "ok", "challenge", "skipped", "skipped", "skipped"]
    assert len(FakeSession.calls) == 2
    summary = canary.summarize(records)
    assert summary["total"] == 5 and summary["attempted"] == 2
    assert summary["challenge_rate_pct"] == 50.0

    class BrokenSession(FakeSession):
        def __enter__(self):
            raise RuntimeError("chrome did not start")

    records = canary.run(["a", "b", "c"], per_session=3,
                         session_factory=BrokenSession)
    assert [r.outcome for r in records] == ["error", "skipped", "skipped"]
    assert canary.summarize(records)["attempted"] == 1


def test_session_retries_a_transient_browser_start(monkeypatch):
    import sys
    import types

    from seo import browser as browser_mod

    calls = {"n": 0}

    def flaky_chrome(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Failed to connect to the browser")
        return FakeBrowser()

    fake_pkg = types.ModuleType("seleniumbase")
    fake_pkg.sb_cdp = types.SimpleNamespace(Chrome=flaky_chrome)
    monkeypatch.setitem(sys.modules, "seleniumbase", fake_pkg)
    monkeypatch.setattr(browser_mod.time, "sleep", lambda s: None)
    s = Session().open()
    assert calls["n"] == 2 and isinstance(s.sb, FakeBrowser)
    s.close()
