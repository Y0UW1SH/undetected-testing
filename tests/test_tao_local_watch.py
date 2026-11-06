"""Local-only chain watcher: offline transport, no credentials or RPC."""
import json
from types import SimpleNamespace

import pytest

A = "5" + "A" * 47
B = "5" + "B" * 47
HOT = "5" + "H" * 47


def test_local_endpoint_file_reads_matching_pair_and_rejects_other_hosts(
        tmp_path):
    from seo import tao_local_watch as watcher
    location = tmp_path / "private.txt"
    base = "api-bittensor-mainnet.n.dwellir.com/test-only"
    location.write_text("https://" + base + "\nwss://" + base,
                        encoding="utf-8-sig")
    assert watcher._file_endpoint(location) == "wss://" + base
    for bad in ("wss://example.org/test-only", "wss://" + base + "?k=bad",
                "https://" + base + "\nwss://" + base + "-different"):
        location.write_text(bad, encoding="utf8")
        with pytest.raises(watcher.WatchError) as error:
            watcher._file_endpoint(location)
        assert "test-only" not in str(error.value)


def evt(name, attrs, module="SubtensorModule", ex=2):
    return SimpleNamespace(value=dict(module_id=module, event_id=name,
                                      attributes=attrs, extrinsic_idx=ex))


class Chain:
    head = 50
    requests = 0

    def __init__(self):
        self.blocks = []

    def get_block_hash(self, n):
        return "hash-" + str(n)

    def get_chain_finalised_head(self):
        return self.get_block_hash(self.head)

    def get_block_number(self, block_hash):
        return int(block_hash.split("-")[1])

    def get_events(self, block_hash):
        n = self.get_block_number(block_hash)
        self.blocks.append(n)
        if n == 50:
            return [evt("Transfer", {"from": A, "to": B,
                                     "amount": 1000000000}, "Balances"),
                    evt("StakeRemoved", [A, HOT, 1500000000,
                                         6000000000, 8, 0]),
                    evt("StakeAdded", [B, HOT, 1500000000, 6000000000, 8, 0]),
                    evt("StakeTransferred", [A, B, HOT, 8, 8, 1500000000]),
                    evt("ExtrinsicSuccess", None, "System")]
        if n == 51:
            return [evt("StakeAdded",
                        [A, HOT, 1000000000, 2000000000, 8, 0]),
                    evt("StakeRemoved",
                        [B, HOT, 1000000000, 3000000000, 9, 0], ex=4),
                    evt("ExtrinsicFailed", {}, "System", ex=4)]
        return []

    def query(self, module, name, *, block_hash):
        assert (module, name) == ("Timestamp", "Now")
        return SimpleNamespace(value=1700000000000)

    def close(self):
        pass


def test_watcher_reads_finalized_movements_resumes_and_does_not_replay():
    from seo.tao_local_watch import poll
    chain = Chain()
    feed = poll(chain, {}, [B], max_blocks=2)
    assert feed["last_scanned_block"] == 50 and feed["lag_blocks"] == 0
    assert {e["kind"] for e in feed["events"]} == {
        "stake_transfer", "tao_transfer"}
    stake = next(e for e in feed["events"] if e["kind"] == "stake_transfer")
    assert (stake["src"], stake["dst"], stake["alpha"]) == (A, B, 6)
    assert stake["tao"] == 1.5 and stake["extrinsic"] == "50-0002"
    assert stake["id"] == "finney:50:3"
    chain.head = 53
    feed = poll(chain, feed, [A, B], max_blocks=2)
    assert feed["last_scanned_block"] == 52 and feed["lag_blocks"] == 1
    assert feed["address_coverage_start"][A] == 51
    assert feed["events"][0]["kind"] == "stake_increase"
    assert len(feed["events"]) == 3
    feed = poll(chain, feed, [A, B], max_blocks=2)
    assert chain.blocks == [50, 51, 52, 53]
    assert poll(chain, feed, [A, B], max_blocks=2)["events"] == feed["events"]
    assert chain.blocks == [50, 51, 52, 53]


def test_watcher_preserves_last_complete_block_on_network_failure():
    from seo.tao_local_watch import poll
    chain = Chain()
    feed = poll(chain, {}, [A], max_blocks=2)
    chain.head = 54
    previous = chain.get_events

    def fail(hash_):
        if hash_ == "hash-52":
            raise RuntimeError("private-endpoint-example")
        return previous(hash_)

    chain.get_events = fail
    result = poll(chain, feed, [A], max_blocks=3)
    assert result["last_scanned_block"] == 51
    assert result["status"] == "read_error"
    assert "private-endpoint-example" not in json.dumps(result)
    chain.get_events = previous
    result = poll(chain, result, [A], max_blocks=3)
    assert result["last_scanned_block"] == 54
    assert len(set(e["id"] for e in result["events"])) == len(result["events"])


@pytest.mark.parametrize("ci", ["CI", "GITHUB_ACTIONS"])
def test_watcher_refuses_ci_before_credentials(ci, tmp_path, monkeypatch):
    from seo import tao_local_watch as watcher
    monkeypatch.setenv(ci, "true")
    monkeypatch.setattr(watcher, "_endpoint", lambda prompt: pytest.fail(
        "CI must not read or prompt for credentials"))
    assert watcher.main(["--once", "--address", A, "--out",
                         str(tmp_path / "feed.json")]) == 2
    assert not (tmp_path / "feed.json").exists()


def test_one_approved_local_pass_writes_feed_and_refreshable_view(
        tmp_path, monkeypatch, capsys):
    from seo import tao_local_watch as watcher
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(watcher, "_endpoint", lambda prompt: "local-test")
    monkeypatch.setattr(watcher, "_connect", lambda url, budget: Chain())
    src = tmp_path / "report.json"
    src.write_text(json.dumps(dict(following=[B], performance={
        "wallets": [], "rankings": {}})))
    path = tmp_path / "feed.json"
    page = tmp_path / "viewer.html"
    args = ["--report", str(src), "--out", str(path),
            "--view", str(page), "--once"]
    assert watcher.main(args) == 0
    feed = json.loads(path.read_text())
    assert len(feed["events"]) == 2 and feed["addresses"] == [B]
    assert "finney:50:3" in page.read_text(encoding="utf8")
    assert "local-test" not in path.read_text()
    printed = capsys.readouterr().out
    assert printed.count("Block 50") == 2
    assert "6 alpha" in printed and "1 TAO" in printed
    assert watcher.main(args) == 0
    assert "Block 50" not in capsys.readouterr().out


def test_initial_budget_stop_keeps_oldest_unscanned_height():
    from seo.tao_local_watch import RequestBudget, poll
    chain = Chain()
    original = chain.get_events

    def paused(block_hash):
        raise RequestBudget

    chain.get_events = paused
    feed = poll(chain, {}, [A], max_blocks=1)
    assert feed["status"] == "rpc_budget"
    assert feed["last_scanned_block"] is None
    chain.get_events = original
    chain.head = 51
    resumed = poll(chain, feed, [A], max_blocks=2)
    assert [r["id"] for r in resumed["events"]] == [
        "finney:51:0", "finney:50:3", "finney:50:0"]


def test_retry_resumes_the_failed_block_and_preserves_cycle_block_limit(
        monkeypatch):
    from seo import tao_local_watch as watch
    monkeypatch.setattr(watch.time, "sleep", lambda _: None)
    clients = []

    def connect(url, meter):
        broken = not clients

        class Flaky(Chain):
            head = 54

            @property
            def requests(self):
                return meter.calls

            def get_events(self, block_hash):
                meter.take()
                if broken and block_hash == "hash-52":
                    raise ConnectionResetError("private-endpoint-example")
                return super().get_events(block_hash)

        clients.append(Flaky())
        return clients[-1]

    monkeypatch.setattr(watch, "_connect", connect)
    notices = []
    feed = watch._poll_cycle("test", dict(last_scanned_block=50), [A], 3,
                             180, notices)
    assert feed["status"] == "catching_up" and feed["last_scanned_block"] == 53
    assert [c.blocks for c in clients] == [[51], [52, 53]]
    assert (feed["rpc_attempts"], feed["rpc_calls"]) == (2, 4)
    assert [e["id"] for e in notices] == ["finney:51:0"]
    assert feed["rpc_retry"] == {"stage": "block_events", "type": "connection"}
    assert "private-endpoint-example" not in json.dumps(feed)


def test_failed_connections_share_the_cap_without_leaking_the_endpoint(
        monkeypatch):
    from seo import tao_local_watch as watch
    monkeypatch.setattr(watch.time, "sleep", lambda _: None)
    calls = []

    def connect(url, meter):
        calls.append(meter.calls)
        for _ in range(7):
            meter.take()
        raise TimeoutError("private-endpoint-example")

    monkeypatch.setattr(watch, "_connect", connect)
    old = dict(rpc_calls=99, last_scanned_block=49)
    feed = watch._poll_cycle("test", old, [A], 2, 10, [])
    assert calls == [0, 7] and feed["rpc_calls"] == 10
    assert feed["status"] == "rpc_budget" and feed["last_scanned_block"] == 49
    assert feed["rpc_retry"] == {"stage": "connect", "type": "timeout"}
    assert "private-endpoint-example" not in json.dumps(feed)


def test_failure_before_an_rpc_reports_zero_current_calls(monkeypatch):
    from seo import tao_local_watch as watch
    monkeypatch.setattr(watch.time, "sleep", lambda _: None)

    def connect(url, meter):
        raise ConnectionError("private-endpoint-example")

    monkeypatch.setattr(watch, "_connect", connect)
    feed = watch._poll_cycle("test", dict(rpc_calls=177), [A], 1, 180, [])
    assert feed["status"] == "read_error"
    assert (feed["rpc_attempts"], feed["rpc_calls"]) == (2, 0)
    assert feed["rpc_error"] == {"stage": "connect", "type": "connection"}
    assert "private-endpoint-example" not in json.dumps(feed)


def test_client_setup_failure_meters_initial_rpc_and_closes_transport(
        monkeypatch):
    import sys

    from seo import tao_local_watch as watch
    monkeypatch.setattr(watch.time, "sleep", lambda _: None)
    closed = []

    class FailingClient:
        def __init__(self, url, **options):
            self.rpc_request("system_chain", [])
            raise ConnectionError(url)

        def rpc_request(self, method, params, callback):
            return {"result": "Finney"}

        def close(self):
            closed.append(True)

    monkeypatch.setitem(sys.modules, "substrateinterface", SimpleNamespace(
        SubstrateInterface=FailingClient))
    feed = watch._poll_cycle("private-endpoint-example", {}, [A], 1, 180, [])
    assert (feed["rpc_calls"], feed["rpc_attempts"]) == (2, 2)
    assert len(closed) == 2
    assert "private-endpoint-example" not in json.dumps(feed)


def test_public_screens_apply_only_to_report_following():
    from seo.tao_local_watch import _addresses
    report = {"following": [A, B], "performance": {"wallets": [
        {"address": A, "screens": ["activity_above_limit"]}]}}
    assert _addresses(report, []) == [B]
    assert _addresses(report, [A]) == sorted([A, B])


def test_local_watch_includes_screened_timing_ranks_without_promoting_profit():
    from seo.tao_local_watch import _address_reasons, _addresses

    a, b, c, d, e, f, g, h = ["5" + char * 47 for char in "ABCDEFGH"]
    report = dict(following=[a, b], performance={
        "wallets": [{"address": b, "screens": ["balance_above_limit"]}],
        "rankings": {"buy": [b, c, c, a, d, e], "sell": [e, c, f, g],
                     "profit_partial": [h], "open_partial": [h]}})
    reasons = _address_reasons(report, [b, b])
    assert reasons == {a: ["followed", "buy_timing"], b: ["manual"],
                       c: ["buy_timing", "sell_timing"], d: ["buy_timing"],
                       e: ["sell_timing"], f: ["sell_timing"]}
    assert _addresses(report, [b]) == sorted(reasons)
    assert report["following"] == [a, b]


def test_watchlist_reports_keep_discovery_picks_until_revoked_or_replaced():
    from seo.tao_local_watch import _selection

    discovery = dict(mode="discovery", checked_at="2026-11-01T00:00:00Z",
                     performance={"wallets": [], "rankings": {"buy": [A, B]}})
    first, state = _selection(discovery, [])
    assert first == {A: ["buy_timing"], B: ["buy_timing"]}
    watch = dict(mode="watchlist", checked_at="2026-11-02T00:00:00Z",
                 performance={"wallets": [{"address": A, "screens": ["high"]}],
                              "rankings": {"buy": [A]}})
    reasons, state = _selection(watch, [], state)
    assert reasons == {B: ["buy_timing"]}
    watch["performance"]["wallets"] = []
    reasons, state = _selection(watch, [], state, discovery)
    assert reasons == {B: ["buy_timing"]}
    manual, preserved = _selection({}, [A], state)
    assert manual == {A: ["manual"]} and preserved == state
    supplement = dict(watch, mode="holder_supplement")
    reasons, state = _selection(supplement, [], state, discovery)
    assert reasons == {B: ["buy_timing"]}
    next_run = dict(discovery, checked_at="2026-11-03T00:00:00Z",
                    performance={"wallets": [], "rankings": {"sell": [A]}})
    reasons, state = _selection(next_run, [], state)
    assert reasons == {A: ["sell_timing"]}


def test_cached_discovery_respects_intervening_watchlist_screens(tmp_path):
    from seo.tao_local_watch import _cached_discovery, _selection

    blocked = [{"address": B, "screens": ["high"]}]
    for n, mode, wallets in [(1, "discovery", []), (2, "watchlist", blocked),
                             (3, "watchlist", [])]:
        # A rerun can have an older run ID than the prior discovery.
        folder = tmp_path / str(100 - n)
        folder.mkdir()
        report = dict(mode=mode, checked_at=f"2026-11-0{n}T00:00:00Z",
                      performance={"wallets": wallets,
                                   "rankings": {"buy": [A, B]}})
        (folder / "tao_collect.json").write_text(json.dumps(report))
    older = _cached_discovery(tmp_path)
    picked, state = _selection(report, [], None, older)
    assert picked == {A: ["buy_timing"]}
    picked, _ = _selection(report, [], state, _cached_discovery(
        tmp_path, after=state["discovery_at"]))
    assert picked == {A: ["buy_timing"]}


def test_hex_alias_cannot_bypass_a_public_screen_or_use_two_slots():
    from seo.tao_local_watch import _addr, _address_reasons

    raw = "0x" + "11" * 32
    canonical = _addr(raw)
    report = dict(following=[raw], performance={
        "rankings": {"buy": [raw, canonical, A, B]},
        "wallets": [{"address": canonical, "screens": ["high"]}]})
    assert _address_reasons(report, []) == {
        A: ["buy_timing"], B: ["buy_timing"]}
    report["performance"]["wallets"] = []
    picked = _address_reasons(report, [])
    assert list(picked) == [canonical, A, B]
    assert picked[canonical] == ["followed", "buy_timing"]


def test_qualified_holder_supplements_existing_discovery_watch():
    from seo.tao_local_watch import _selection
    discovery = dict(mode="discovery", checked_at="2026-11-04T00:00:00Z",
                     performance={"rankings": {"buy": [A]}})
    _, state = _selection(discovery, [])
    report = dict(mode="holder_supplement", checked_at="2026-11-05T00:00:00Z",
                  performance={"rankings": {"buy": [B], "sell": [B]}})
    reasons, state = _selection(report, [], state)
    assert reasons == {A: ["buy_timing"], B: ["buy_timing", "sell_timing"]}
    reasons, _ = _selection(dict(mode="watchlist"), [], state)
    assert B in reasons


def test_same_wallet_hotkey_switch_is_one_movement_not_two_alerts():
    from seo.tao_local_watch import block_events
    other_hot = "5" + "Z" * 47
    rows = [evt("StakeRemoved", [A, HOT, 1500000000, 6000000000, 8, 0]),
            evt("StakeAdded", [A, other_hot, 1500000000, 6000000000, 8, 0]),
            evt("StakeMoved", [A, HOT, 8, other_hot, 8, 1500000000])]
    moves = block_events(777, rows, [A])
    assert len(moves) == 1
    e, = moves
    assert e["event_name"] == "SubtensorModule.StakeMoved"
    assert e["src"] == e["dst"] == A
    assert e["alpha"] == e["dst_alpha"] == 6
