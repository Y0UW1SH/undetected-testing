"""Wallet collection and accounting regressions, using offline page data."""
import json

from seo import storage, tao_cli
from seo.sources import tao_history
from seo.sources import taostats_wallets as site
from tests import fakes

A = "5" + "A" * 47
B = "5" + "B" * 47
C = "5" + "C" * 47
DAY = "2026-10-01T00:00:00Z"


def flight(name, rows):
    query = {"dehydratedAt": 1, "queryKey": [name],
             "state": {"status": "success", "data": {"data": rows}}}
    return "self.__next_f.push([1,%s])" % json.dumps(json.dumps(query))


def account(owner, amount=10, swap=None):
    return flight("account", [{"address": {"ss58": owner},
                               "balance_total": "3000000000",
                               "timestamp": DAY, "coldkey_swap": swap,
                               "alpha_balances": [{
                                   "netuid": 8, "hotkey": "hk",
                                   "balance": str(amount * 10**9),
                                   "balance_as_tao": "1000000000"}]}])


def test_wallets_then_track_stores_positions(fake_env, monkeypatch):
    def pages(url):
        if url.endswith("/metagraph"):
            return "", flight("metagraph", [{"coldkey": {"ss58": A},
                                             "daily_total_rewards_as_tao":
                                             "1000000000", "netuid": 8}])
        return "", account(A)

    monkeypatch.setattr(fakes, "google_router", pages)
    argv = ["--db", fake_env["db"]]
    assert tao_cli.main(argv + ["wallets", "--netuids", "8",
                                "--follow-top", "1"]) == 0
    assert tao_cli.main(argv + ["track"]) == 0
    store = storage.Storage(fake_env["db"])
    assert [p["alpha"] for p in store.rows("wallet_positions")] == [10, 10]
    store.close()


def test_transfer_carries_partial_basis_across_two_wallets():
    from seo.tao_ledger import portfolio

    def event(n, kind, src, dst, alpha, tao=None):
        return dict(time="2026-09-%02dT00:00:00+00:00" % n,
                    id=str(n), kind=kind, src=src, dst=dst,
                    netuid=8, alpha=alpha, tao=tao, evidence="transaction")

    events = [event(1, "buy", None, A, 10, 2),
              event(2, "buy", None, B, 4, 1.6),
              event(3, "stake_transfer", A, B, 4),
              event(4, "stake_transfer", B, C, 4),
              event(5, "sell", C, None, 2, 1),
              event(6, "tao_transfer", A, B, None, 5),
              event(7, "sell", "unknown", None, 3, 10)]
    positions, stats = portfolio(events, [])
    assert positions[B, 8]["buy_price_tao"] == 0.3
    assert positions[C, 8]["buy_price_tao"] == 0.3
    assert stats[C]["realized_tao"] == 0.4
    assert stats[C]["wins"] == stats[C]["priced_sales"] == 1
    assert stats["unknown"]["unpriced_sales"] == 1
    assert stats.get(B, {}).get("priced_sales", 0) == 0


def test_collect_reads_each_wallet_once_and_reports_unavailable_history(
        fake_env, monkeypatch):
    original = fakes.google_router

    def pages(url):
        if url.endswith("/metagraph"):
            return "", flight("metagraph", [{"coldkey": {"ss58": A},
                                             "daily_total_rewards_as_tao":
                                             "1000000000", "netuid": 8}])
        if "/account/" in url:
            return "", account(A)
        return original(url)

    monkeypatch.setattr(fakes, "google_router", pages)
    monkeypatch.setattr(tao_history, "fetch_recent_trades",
                        lambda s: ([], "unavailable"), raising=False)
    monkeypatch.setattr(tao_history, "fetch_history",
                        lambda *args: ([], "unavailable"), raising=False)
    argv = ["--db", fake_env["db"]]
    tao_cli.main(argv + ["follow", A])
    assert tao_cli.main(argv + ["collect", "--netuids", "8", "--top", "1",
                                "--delay", "0"]) == 0
    browsers = fakes.RecordingSession.created
    assert len(browsers) == 1
    assert browsers[0].fake.visited.count(site.account_url(A)) == 1
    with open("data/tao_collect.json", encoding="utf8") as f:
        report = json.load(f)
    assert report["coverage"][A]["transactions"] == "unavailable"
    assert report["positions"][0]["buy_price_tao"] is None
    assert report["traders"] == []


def test_collect_replay_preserves_transferred_cost(fake_env, monkeypatch):
    original = fakes.google_router

    def pages(url):
        if url.endswith("/metagraph"):
            return "", flight("metagraph", [{"coldkey": A, "netuid": 8,
                                             "daily_total_rewards_as_tao":
                                             "5000000000"}])
        if "/account/" in url:
            addr = url.rsplit("/", 1)[1]
            return "", account(addr, 6 if addr == A else 2)
        return original(url)

    def trade(day, owner, action, qty, amount, peer=None):
        return dict(id=str(day) + owner, action=action,
                    nominator={"ss58": owner}, delegate={"ss58": C},
                    netuid=8, extrinsic_id="%d-1" % day,
                    timestamp="2026-09-%02dT00:00:00Z" % day,
                    alpha=str(qty * 10**9), amount=str(amount * 10**9),
                    is_transfer=bool(peer), transfer_address=peer)

    rows = {A: [trade(1, A, "DELEGATE", 10, 2),
                trade(2, A, "UNDELEGATE", 4, 1, B)],
            B: [trade(2, B, "DELEGATE", 4, 1, A),
                trade(3, B, "UNDELEGATE", 2, 1)]}

    def history(session, addr, tab, pages):
        if tab == "transfers":
            return [], "complete"
        return [tao_history.normalize(r, tab, addr)
                for r in rows[addr]], "complete"

    monkeypatch.setattr(fakes, "google_router", pages)
    monkeypatch.setattr(tao_history, "fetch_history", history)
    monkeypatch.setattr(tao_history, "fetch_recent_trades",
                        lambda s: ([], "unavailable"), raising=False)
    argv = ["--db", fake_env["db"]]
    tao_cli.main(argv + ["follow", A])
    for _ in range(2):
        assert tao_cli.main(argv + ["collect", "--netuids", "8",
                                    "--delay", "0"]) == 0
        with open("data/tao_collect.json", encoding="utf8") as f:
            report = json.load(f)
        dest = next(p for p in report["positions"] if p["address"] == B)
        assert dest["buy_price_tao"] == 0.2
        assert report["traders"][0]["realized_tao"] == 0.6
        assert len(report["transfers"]) == 1
        assert B in report["following"]


def test_delay_setting_priority(monkeypatch):
    monkeypatch.setenv("TAO_DELAY", "2-4")
    parser = tao_cli.build_parser()
    assert parser.parse_args(["collect"]).pauses == (2, 4)
    assert parser.parse_args(["--delay", "1", "collect"]).pauses == (1, 1)
    assert parser.parse_args(["collect", "--delay", "0"]).pauses == (0, 0)
