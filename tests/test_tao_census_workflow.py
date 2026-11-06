"""Offline checks for portable census checkpoints and finite run chains."""
from argparse import Namespace
import json
import sqlite3

import pytest

from seo.sources import tao_census
from tests.test_tao_census import page
from tools import tao_census_workflow as flow


def scan(path, pages, count):
    with sqlite3.connect(path) as db:
        return tao_census.scan(db, pages.__getitem__, count)


def rows(path, table):
    with sqlite3.connect(path) as db:
        return sorted(db.execute("SELECT * FROM " + table))


def test_incremental_state_roundtrips_legacy_repeated_keys_and_all_generations(
        tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    legacy = state / "accounts.sqlite"
    pages = {1: page(1, 5, "AB"), 2: page(2, 5, "BC"), 3: page(3, 5, "D")}
    scan(legacy, pages, 1)
    work = tmp_path / "work.sqlite"
    flow.restore(state, work)
    prior = flow.page_versions(work)
    scan(work, pages, 1)
    gen = flow.pack(state, work, prior, part_size=120)
    assert gen == 1
    assert len(list((state / "census-data").glob("000001-*"))) > 1
    prior = flow.page_versions(work)
    restored = tmp_path / "gen1.sqlite"
    flow.restore(state, restored, through=1)
    assert rows(restored, "census_accounts") == rows(work, "census_accounts")
    scan(work, pages, 1)
    assert flow.pack(state, work, prior, part_size=120) == 2
    latest = tmp_path / "latest.sqlite"
    flow.restore(state, latest)
    for table in flow.TABLES:
        assert rows(latest, table) == rows(work, table)
    with sqlite3.connect(latest) as db:
        assert db.execute("SELECT COUNT(*) FROM census_accounts").fetchone(
        )[0] == 4
    with pytest.raises(FileExistsError):
        flow.restore(state, latest)
    old = tmp_path / "old.sqlite"
    flow.restore(state, old, through=1)
    assert rows(old, "census_accounts") == rows(restored, "census_accounts")


def test_completed_directory_persists_new_anchor_even_without_page_changes(
        tmp_path):
    path, state = tmp_path / "work.sqlite", tmp_path / "state"
    scan(path, {1: page(1, 2, "AB")}, 1)
    flow.pack(state, path, {})
    before = flow.page_versions(path)
    result = scan(path, {1: page(1, 2, "AB", block=108)}, 1)
    assert result["status"] == "traversed_live"
    assert flow.pack(state, path, before) == 2
    copy = tmp_path / "restored.sqlite"
    flow.restore(state, copy)
    assert rows(copy, "census_state") == rows(path, "census_state")
    assert flow.pack(state, path, before) == 2


def test_corrupt_or_missing_delta_never_replaces_a_local_database(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    src = tmp_path / "source.sqlite"
    scan(src, {1: page(1, 2, "AB")}, 1)
    flow.pack(state, src, {})
    parts = list((state / "census-data").rglob("*.jsonl.gz"))
    assert parts
    parts[0].write_bytes(b"broken")
    dest = tmp_path / "existing.sqlite"
    dest.write_bytes(b"preserve existing contents")
    with pytest.raises((OSError, ValueError)):
        flow.restore(state, dest, replace=True)
    assert dest.read_bytes() == b"preserve existing contents"


def test_full_continuation_requires_a_forward_cursor_and_remaining_budget():
    partial = {"status": "partial", "next_page": 12, "error": None,
               "verified_pages_this_run": [10, 11]}
    assert flow.continuation(partial, 10, 3) == "continue"
    assert flow.continuation(partial, 12, 3) == "stalled"
    assert flow.continuation(partial, 10, 0) == "limit"
    assert flow.continuation(dict(partial, verified_pages_this_run=[]),
                             10, 3) == "stalled"
    assert flow.continuation(dict(partial, error="timeout"), 10, 3) == "failed"
    for status in ("traversed_live", "complete_single_block"):
        assert flow.continuation(dict(partial, status=status, next_page=None),
                                 10, 0) == "complete"


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    origin = tmp_path / "remote"
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    flow._git("init", "--bare", str(origin))
    flow._git("init", str(repo))
    monkeypatch.chdir(repo)
    flow._git("config", "user.name", "Census Test")
    flow._git("config", "user.email", "census@example.invalid")
    flow._git("remote", "add", "origin", str(origin))
    flow._git("commit", "--allow-empty", "-m", "code")
    flow._git("worktree", "add", "--detach", "--no-checkout", str(state))
    flow._git("switch", "--orphan", "state", cwd=state)
    branch = "tao-account-registry"
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    args = Namespace(state=state, db=tmp_path / "accounts.sqlite",
                     out=tmp_path / "summary.json", branch=branch,
                     mode="full", pages=8, delay="2", expected="", remaining=6)
    symbols = ["AB", "CD", "EF", "GH", "JK", "LM", "NP"]
    pages = {p: page(p, 14, pair) for p, pair in enumerate(symbols, 1)}
    scan(state / "accounts.sqlite", pages, 1)
    flow._git("add", "--", "accounts.sqlite", cwd=state)
    flow._git("commit", "-m", "legacy checkpoint", cwd=state)
    flow._git("push", "origin", f"HEAD:refs/heads/{branch}", cwd=state)
    return args, pages, outputs, origin


def test_campaign_saves_each_chunk_and_resumes_after_legacy_migration(
        campaign, monkeypatch):
    args, pages, outputs, origin = campaign
    chunks = []

    def sample(db, out, limit, delay, timeout):
        chunks.append(limit)
        assert delay == "2" and 0 < timeout <= flow.CHUNK_TIMEOUT
        report = scan(db, pages, 1)
        tao_census._write_report(out, report)
        return report

    monkeypatch.setattr(flow, "_scan", sample)
    assert flow.run(args) == 0
    first = dict(s.split("=", 1) for s in outputs.read_text().splitlines())
    assert first["continue"] == "true" and first["remaining"] == "2"
    assert chunks == [100] * 4
    ref = "refs/heads/" + args.branch
    assert first["commit"] == flow._git("rev-parse", ref, cwd=origin)
    files = flow._git("ls-tree", "--name-only", ref, cwd=origin).splitlines()
    assert set(files) == {"census-data", flow.MANIFEST}
    # A new runner restores from the committed parts.
    args.db = args.db.with_name("next.sqlite")
    args.remaining = int(first["remaining"])
    args.expected = first["commit"]
    outputs.write_text("")
    assert flow.run(args) == 0
    assert "complete=true" in outputs.read_text()
    assert "continue=false" in outputs.read_text()
    assert chunks == [100] * 6
    assert len(flow._manifest(args.state)["generations"]) == 6
    assert flow._git("rev-parse", ref, cwd=origin) != first["commit"]
    with pytest.raises(ValueError, match="differs from"):
        flow.run(args)
    assert len(chunks) == 6


def test_timeout_publishes_partial_work_then_stops(campaign, monkeypatch):
    args, pages, outputs, _ = campaign

    def interrupted(db, out, *_):
        scan(db, pages, 1)
        with sqlite3.connect(db) as conn:
            tao_census._schema(conn)
            return tao_census._report(conn, [], "Census process stopped")

    monkeypatch.setattr(flow, "_scan", interrupted)
    assert flow.run(args) == 1
    assert "continue=false" in outputs.read_text()
    report = json.loads(args.out.read_text(encoding="utf8"))
    assert report["next_page"] == 3
    assert report["verified_pages_this_run"] == [2]
    recovered = args.db.with_name("restored.sqlite")
    flow.restore(args.state, recovered)
    assert rows(recovered, "census_pages") == rows(args.db, "census_pages")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    with pytest.raises(ValueError, match="new full dispatch"):
        flow.run(args)
