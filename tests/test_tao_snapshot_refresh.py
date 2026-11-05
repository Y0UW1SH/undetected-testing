"""Read-only GitHub refresh, atomic promotion and local archive navigation."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from seo import tao_view
from seo import tao_local_watch as watch

A = "5" + "A" * 47


def report(day, mode="discovery"):
    return dict(checked_at="2026-11-%02dT12:00:00Z" % day, mode=mode,
                following=[A], performance={"wallets": [], "rankings": {}})


def cached(cache, run, data):
    p = cache / str(run) / "tao-wallet-report" / "tao_collect.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf8")
    return p


def github(monkeypatch, runs, artifacts, download):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs["timeout"] <= 45
        assert kwargs["env"]["GH_PROMPT_DISABLED"] == "1"
        assert not {"DWELLIR_API_KEY", "DWELLIR_WS_URL", "GH_TOKEN",
                    "GITHUB_TOKEN", "SSH_AUTH_SOCK", "SOMETHING_PRIVATE"
                    }.intersection(kwargs["env"])
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert argv[0] == "gh"
        if argv[1:3] == ["run", "list"]:
            assert "completed" in argv and "tao-collect.yml" in argv
            return subprocess.CompletedProcess(argv, 0, json.dumps(runs))
        if argv[1] == "api":
            assert "actions/artifacts?name=wallet-collection" in argv[2]
            return subprocess.CompletedProcess(argv, 0, json.dumps(artifacts))
        assert argv[1:3] == ["run", "download"]
        dest = Path(argv[argv.index("-D") + 1])
        download(dest)
        return subprocess.CompletedProcess(argv, 0, "")

    monkeypatch.setattr(tao_view.subprocess, "run", run)
    for key in ("DWELLIR_API_KEY", "DWELLIR_WS_URL", "GH_TOKEN",
                "SOMETHING_PRIVATE", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(key, "test")
    return calls


def save_artifact(dest, data):
    p = dest / "tao-wallet-report" / "tao_collect.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf8")


def test_completed_run_with_an_artifact_is_downloaded_once(
        tmp_path, monkeypatch):
    old = cached(tmp_path, 80, report(1))
    calls = github(monkeypatch, [{"databaseId": 84}, {"databaseId": 83}],
                   [{"run": 83, "id": 917}],
                   lambda d: save_artifact(d, report(2, "watchlist")))
    path, error = tao_view.refresh_latest(tmp_path)
    assert path != old and error is None
    assert path.relative_to(tmp_path).parts[0] == "83"
    assert json.loads(path.read_text())["mode"] == "watchlist"
    assert tao_view.refresh_latest(tmp_path)[0] == path
    assert len([c for c, _ in calls if c[1:3] == ["run", "download"]]) == 1


def test_incomplete_artifact_keeps_newer_cache_even_on_higher_run_id(
        tmp_path, monkeypatch):
    latest = cached(tmp_path, 91, report(5))
    cached(tmp_path, 93, report(1))
    github(monkeypatch, [{"databaseId": 100}], [{"run": 100, "id": 900}],
           lambda d: save_artifact(d, {"checked_at": "tomorrow"}))
    path, err = tao_view.refresh_latest(tmp_path)
    assert err and path == latest
    assert not (tmp_path / "100").exists()
    assert not list(tmp_path.glob(".download-*"))


def test_older_remote_scan_and_offline_error_never_displace_newer_scan(
        tmp_path, monkeypatch):
    latest = cached(tmp_path, 98, report(4))
    github(monkeypatch, [{"databaseId": 110}], [{"run": 110, "id": 1}],
           lambda d: save_artifact(d, report(2)))
    assert tao_view.refresh_latest(tmp_path)[0] == latest

    def offline(*args, **kwargs):
        raise subprocess.TimeoutExpired("gh", 8, stderr="test-private")

    monkeypatch.setattr(tao_view.subprocess, "run", offline)
    path, err = tao_view.refresh_latest(tmp_path)
    assert path == latest and err and "test-private" not in err


def test_live_view_keeps_saved_snapshots_without_writing_feed_to_archive(
        tmp_path):
    older = cached(tmp_path, 81, report(1))
    newer = cached(tmp_path, 82, report(2, "watchlist"))
    tao_view.main(["--input", str(older), "--cache", str(tmp_path),
                   "--out", str(tmp_path / "wallets.html")])
    args = SimpleNamespace(report=newer, view=tmp_path / "wallets.html",
                           out=tmp_path / "feed.json")
    watch._persist(args, {"source": "local-feed-test"}, report(2, "watchlist"))
    for _ in range(2):
        assert "Saved snapshots" in args.view.read_text(encoding="utf8")
        assert 'href="81/wallets.html"' in args.view.read_text(encoding="utf8")
        assert "local-feed-test" in args.view.read_text(encoding="utf8")
        assert "local-feed-test" not in (tmp_path / "82/wallets.html"
                                         ).read_text(encoding="utf8")
        watch._persist(args, {"source": "local-feed-test"}, report(2,
                       "watchlist"))


def test_watch_checks_at_start_and_six_hours_even_after_timeout(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    first = cached(Path("github_out"), 101, report(1))
    elapsed, checks, polls = [0.], [], []

    def refresh(cache):
        checks.append(elapsed[0])
        if len(checks) == 1:
            return first, "GitHub snapshot check timed out."
        return cached(cache, 102, report(2)), None

    def sleep(seconds):
        elapsed[0] += seconds
        if elapsed[0] > watch.SNAPSHOT_INTERVAL:
            raise KeyboardInterrupt

    class Chain:
        def close(self):
            pass

    def poll(chain, feed, addresses, limit, notices):
        polls.append((elapsed[0], list(addresses)))
        return dict(feed, source=watch.SOURCE, status="current",
                    checked_at=watch._now(), rpc_calls=0)

    monkeypatch.setattr(tao_view, "refresh_latest", refresh)
    monkeypatch.setattr(watch, "_endpoint", lambda prompt: "test")
    monkeypatch.setattr(watch, "_connect", lambda url, limit: Chain())
    monkeypatch.setattr(watch, "poll", poll)
    monkeypatch.setattr(watch.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(watch.time, "sleep", sleep)
    assert watch.main(["--refresh-snapshots"]) == 130
    assert checks == [0., 6 * 3600]
    assert len(polls) == 73 and all(a == [A] for _, a in polls)
    assert report(2)["checked_at"] in Path("github_out/wallets.html"
                                           ).read_text(encoding="utf8")


def test_explicit_wallet_survives_absent_github_snapshot(tmp_path,
                                                         monkeypatch):
    from tests.test_tao_local_watch import Chain
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(tao_view, "refresh_latest", lambda _: (
        None, "GitHub snapshot unavailable."))
    monkeypatch.setattr(watch, "_endpoint", lambda prompt: "test")
    monkeypatch.setattr(watch, "_connect", lambda url, limit: Chain())
    assert watch.main(["--refresh-snapshots", "--address", A, "--once"]) == 0
    feed = json.loads(Path("try_tao_out/local_wallet_activity.json"
                           ).read_text(encoding="utf8"))
    assert feed["addresses"] == [A] and feed["snapshot_refresh"][
        "status"] == "unavailable"


def test_new_artifact_from_rerun_is_preferred_to_newer_workflow_run_id(
        tmp_path, monkeypatch):
    github(monkeypatch, [{"databaseId": 80}, {"databaseId": 72}],
           [{"run": 80, "id": 900, "created": "2026-11-01T12:00:00Z"},
            {"run": 72, "id": 920, "created": "2026-11-05T12:00:00Z"}],
           lambda d: save_artifact(d, report(5)))
    path, err = tao_view.refresh_latest(tmp_path)
    assert err is None and path.relative_to(tmp_path).parts[0] == "72"


def test_archive_index_is_recoverable_from_cached_snapshot_files(tmp_path):
    older = cached(tmp_path, 81, report(1))
    newer = cached(tmp_path, 82, report(2, "watchlist"))
    out = tmp_path / "wallets.html"
    tao_view.main(["--input", str(older), "--cache", str(tmp_path),
                   "--out", str(out)])
    (tmp_path / "wallet_views.json").write_text("{", encoding="utf8")
    tao_view.main(["--input", str(newer), "--cache", str(tmp_path),
                   "--out", str(out)])
    html = out.read_text(encoding="utf8")
    assert 'href="81/wallets.html"' in html
    assert 'href="82/wallets.html"' in html


def test_same_run_rerun_publishes_json_sqlite_and_diagnostics_together(
        tmp_path, monkeypatch):
    old = cached(tmp_path, 81, report(1))
    binary = Path("tao-wallet-state/wallets.sqlite")
    (tmp_path / "81" / binary).parent.mkdir()
    (tmp_path / "81" / binary).write_bytes(b"old database")

    def upload(dest):
        save_artifact(dest, report(5))
        (dest / binary).parent.mkdir()
        (dest / binary).write_bytes(b"new database")
        (dest / "diagnostic.txt").write_text("same generation")

    github(monkeypatch, [{"databaseId": 81}], [{"run": 81, "id": 916}],
           upload)
    new, warning = tao_view.refresh_latest(tmp_path)
    assert warning is None and new != old
    assert json.loads(old.read_text())["checked_at"] == report(1)["checked_at"]
    assert json.loads(new.read_text())["checked_at"] == report(5)["checked_at"]
    root = new.parent.parent
    assert (root / binary).read_bytes() == b"new database"
    assert (root / "diagnostic.txt").read_text() == "same generation"
    assert tao_view.run_snapshot(tmp_path / "81") == new


def test_old_view_with_local_activity_is_rebuilt_as_scan_only(tmp_path):
    path = cached(tmp_path, 88, report(3))
    archive = tmp_path / "88/wallets.html"
    archive.write_text(tao_view.render(report(3), {"source": "private-feed"}),
                       encoding="utf8")
    index = tmp_path / "wallet_views.json"
    index.write_text(json.dumps({"88": [report(3)["checked_at"],
                                        "discovery"]}))
    tao_view.main(["--input", str(path), "--cache", str(tmp_path),
                   "--out", str(tmp_path / "wallets.html")])
    assert "private-feed" not in archive.read_text(encoding="utf8")


def test_removed_input_keeps_existing_view_live_without_recreating_archive(
        tmp_path):
    source = cached(tmp_path, 88, report(3))
    args = SimpleNamespace(report=source, out=tmp_path / "feed.json",
                           view=tmp_path / "wallets.html")
    watch._persist(args, {"status": "current"}, report(3))
    source.unlink()
    watch._persist(args, {"status": "rpc_budget"}, report(3))
    assert "rpc_budget" in args.view.read_text(encoding="utf8")
