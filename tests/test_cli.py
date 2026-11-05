"""CLI wiring tests with a fake browser; no Chrome, no network."""
import os
import subprocess
import sys

from seo import canary, cli, storage
from tests.fakes import RecordingSession, run, write_config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_resolve_location_uses_geocoder_canonical_and_cache(fake_env):
    store = storage.Storage(fake_env["db"])
    lat, lng, canonical, label = cli._resolve_location(store, "Austin, TX")
    assert (lat, lng) == (30.27, -97.74)
    assert canonical == "Austin,Texas,United States"   # not "Austin,TX,..."
    assert label == "Austin, TX"
    cli._resolve_location(store, "Austin, TX")
    assert fake_env["geocode_calls"] == ["Austin, TX"]  # second hit cached
    _, _, canonical, _ = cli._resolve_location(store, "Paris, France")
    assert canonical == "Paris,Ile-de-France,France"
    assert cli._resolve_location(store, "", 1.0, 2.0) == (
        1.0, 2.0, None, "1.0,2.0")
    store.close()


def test_serp_command_localises_and_caches(fake_env):
    assert run(fake_env, "serp", "emergency plumber",
               "--location", "Austin, TX") == 0
    s = RecordingSession.created[0]
    assert s.geoloc == (30.27, -97.74)
    assert "uule=w+CAIQICI" in s.fake.visited[0]
    assert s.fake.quit_called
    assert run(fake_env, "serp", "emergency plumber",
               "--location", "Austin, TX") == 0
    assert len(RecordingSession.created) == 1      # fetched today: no browser


def test_batch_groups_sessions_by_location_and_device(fake_env):
    write_config([
        {"location": "Austin, TX", "device": "desktop",
         "serp": ["plumber", "electrician"], "local": ["plumber"]},
        {"location": "Austin, TX", "device": "mobile", "serp": ["plumber"]},
        {"location": "Paris, France", "serp": ["plombier"]},
    ])
    assert run(fake_env, "batch", "--config", "cfg.json",
               "--per-session", "5") == 0
    sessions = RecordingSession.created
    # one session per (location, device) group, never shared across them
    assert [(s.mobile, s.geoloc) for s in sessions] == [
        (False, (30.27, -97.74)), (True, (30.27, -97.74)),
        (False, (48.85, 2.35))]
    mobile_urls = sessions[1].fake.visited
    assert len(mobile_urls) == 1 and "q=plumber" in mobile_urls[0]
    store = storage.Storage(fake_env["db"])
    rows = store.rows("serp_results", query="plumber")
    devices = {r["device"] for r in rows}
    assert devices == {"desktop", "mobile"}     # mobile job was not skipped
    assert store.rows("serp_results", query="plombier")
    store.close()
    # a second run skips everything: same (query, location, device) today
    RecordingSession.created = []
    assert run(fake_env, "batch", "--config", "cfg.json") == 0
    assert RecordingSession.created == []


def test_batch_rotates_sessions_within_a_group(fake_env):
    write_config({"location": "Austin, TX",
                  "serp": ["a", "b", "c", "d", "e"]})
    assert run(fake_env, "batch", "--config", "cfg.json",
               "--per-session", "2") == 0
    assert [len(s.fake.visited) for s in RecordingSession.created] == [
        2, 2, 1]


def test_batch_skip_retries_remaining_jobs_in_a_new_session(fake_env,
                                                            monkeypatch):
    from seo.models import ChallengeError
    from seo.serp import google

    real_fetch = google.fetch_serp

    def challenged_fetch(session, query, *a, **k):
        if "pizza" in query:
            raise ChallengeError("sorry page")
        return real_fetch(session, query, *a, **k)

    monkeypatch.setattr(google, "fetch_serp", challenged_fetch)
    write_config({"location": "Austin, TX",
                  "serp": ["a", "best pizza", "c", "d"]})
    assert run(fake_env, "batch", "--config", "cfg.json",
               "--per-session", "4", "--on-challenge", "skip") == 0
    # session 1: "a" loaded, "pizza" challenged before any page load;
    # session 2: c, d
    assert [len(s.fake.visited) for s in RecordingSession.created] == [1, 2]
    store = storage.Storage(fake_env["db"])
    done = {r["query"] for r in store.rows("serp_results")}
    assert done == {"a", "c", "d"}
    store.close()
    # default --on-challenge stop: nothing after the challenge runs
    RecordingSession.created = []
    assert run(fake_env, "batch", "--config", "cfg.json", "--force",
               "--per-session", "4") == 0        # 'a' succeeded
    assert len(RecordingSession.created) == 1


def test_batch_grid_without_location_is_an_error_not_a_crash(fake_env,
                                                             capsys):
    write_config({"grid": [{"query": "plumber", "target": "Joe's"}],
                  "serp": ["plumber"]})
    assert run(fake_env, "batch", "--config", "cfg.json") == 0
    captured = capsys.readouterr()
    assert "needs a block 'location'" in captured.err
    assert '"errors": 1' in captured.out and '"done": 1' in captured.out


def test_batch_exit_codes_when_nothing_succeeds(fake_env):
    write_config({"grid": [{"query": "plumber", "target": "Joe's"}]})
    assert run(fake_env, "batch", "--config", "cfg.json") == 1


def test_grid_command_writes_heatmap_and_rows(fake_env):
    assert run(fake_env, "grid", "plumber", "--target", "Joe's Plumbing",
               "--lat", "30.27", "--lng", "-97.74", "--size", "3",
               "--out", "g.html") == 0
    assert os.path.exists("g.html")
    s = RecordingSession.created[0]
    assert len(s.fake.visited) == 9
    assert s.geoloc != (30.27, -97.74)      # moved to the last grid cell
    store = storage.Storage(fake_env["db"])
    rows = store.rows("grid_cells")
    assert len(rows) == 9
    assert len({r["fetched_at"] for r in rows}) == 1   # one run, one stamp
    store.close()


def test_grid_requires_a_location(fake_env):
    assert run(fake_env, "grid", "plumber", "--target", "x") == 2


def test_module_entry_point_propagates_exit_code(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "seo", "--db", str(tmp_path / "x.sqlite"),
         "grid", "plumber", "--target", "x"],
        cwd=REPO, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2, proc.stderr
    assert "needs --location" in proc.stderr


def test_canary_and_export_commands(fake_env):
    records = canary.run(["a", "b"], per_session=1,
                         session_factory=RecordingSession)
    assert [r.outcome for r in records] == ["ok", "ok"]
    assert run(fake_env, "export", "--format", "json", "--dir", "out") == 0
    assert sorted(os.listdir("out")) == [
        "businesses.json", "canary_runs.json", "grid_cells.json",
        "listings.json", "local_pack.json", "nap_records.json",
        "reddit_posts.json", "serp_results.json", "subnet_stats.json"]


def test_local_details_merges_place_page(fake_env):
    assert run(fake_env, "local", "plumber", "--lat", "30.27", "--lng",
               "-97.74", "--details", "--max", "2") == 0
    s = RecordingSession.created[0]
    # one feed load plus one place page per listing
    assert len(s.fake.visited) == 3
    assert sum("/maps/place/" in u for u in s.fake.visited) == 2
    store = storage.Storage(fake_env["db"])
    rows = store.rows("businesses")
    assert len(rows) == 2
    assert rows[0]["phone"] == "+15125550100"
    assert rows[0]["website"] == "https://www.joesplumbing.example/"
    assert rows[0]["hours"].startswith("Monday: 8 AM")
    # place page upgrades the feed address to the full one
    assert rows[0]["address"] == "123 Main St, Austin, TX 78701"
    store.close()


def test_snapshot_diffs_against_previous_run(fake_env, capsys):
    assert run(fake_env, "snapshot", "plumber", "--location", "Austin, TX",
               "--max", "3") == 0
    store = storage.Storage(fake_env["db"])
    # fake an older run with different review counts and ordering
    for row in store.rows("businesses"):
        store.conn.execute(
            "UPDATE businesses SET fetched_at = ?, reviews = ?, position = ?"
            " WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", (row["reviews"] or 0) - 5,
             4 - row["position"], row["id"]))
    store.conn.commit()
    store.close()
    capsys.readouterr()
    assert run(fake_env, "snapshot", "plumber", "--location", "Austin, TX",
               "--max", "3") == 0
    out = capsys.readouterr().out
    assert "(+5 reviews)" in out
    assert "was #3" in out


def test_nap_command_checks_directory_pages(fake_env, capsys):
    assert run(fake_env, "nap", "Joe's Plumbing LLC", "--phone",
               "512-555-0100", "--address", "123 Main St, Austin, TX 78701",
               "--city", "Austin, TX") == 0
    s = RecordingSession.created[0]
    assert any("yelp.com" in u for u in s.fake.visited)
    out = capsys.readouterr().out
    assert "Yelp" in out and "phone=True address=True" in out
    store = storage.Storage(fake_env["db"])
    rows = store.rows("nap_records")
    assert len(rows) == 1 and rows[0]["source"] == "Yelp"
    assert rows[0]["phone_match"] == 1 and rows[0]["address_match"] == 1
    store.close()
