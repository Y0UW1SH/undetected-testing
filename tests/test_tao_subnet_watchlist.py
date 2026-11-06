from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
from http.client import HTTPConnection
import json
import threading

from seo import tao_watchlist as wl


A = "5EspQViAKxkKGD6xdRwdMju3SFHSQwKhbtEWhTxUoMsMVQzE"
OLD = "2026-11-01T00:00:00Z"
NOW = "2026-11-02T00:00:00Z"


def test_risk_inputs_use_full_attributed_native_profile_and_events():
    buys = [dict(id=str(i), kind="buy", dst=A, netuid=24,
                 extrinsic=f"{100 + i}-1", tao=2, alpha=3)
            for i in range(8)]
    hosted = dict(checked_at=NOW, coverage={A: {}}, events=buys,
                  performance=dict(wallets=[dict(address=A, marker="hosted")]))
    native = dict(address=A, marker="native", positions=[
        dict(netuid=24, known_cost_tao=None)])
    replay = dict(hosted, local_evidence={"receipt_count": 1},
                  performance=dict(wallets=[native]))
    passive = dict(checked_at="2026-11-03T00:00:00Z", coverage={},
                   performance=dict(wallets=[dict(address=A, marker="stale")]))
    reports = [passive, replay, hosted]
    original = deepcopy(reports)
    wallets, events = wl._risk_inputs(reports)
    wallet, = wallets
    assert wallet["marker"] == "native" and wallet["stamp"] == NOW
    assert wallet["positions"][0]["known_cost_tao"] is None
    assert {e["extrinsic"] for e in events} == {
        f"{100 + i}-1" for i in range(8)}
    assert reports == original


def test_wallet_risk_suppresses_current_headline_not_dated_realized():
    wallets = {A: dict(stamp=OLD, known_realized=5, known_open=9,
                       ranks={"profit": 1, "open": 1})}
    risk = dict(invalid_current_valuation=True, provisional=True,
                wallet_as_of=OLD, assessed_at=NOW,
                flags=["generation_changed"])
    before = deepcopy(wallets)
    row = wl.Model._wallet(A, wallets, {}, {}, {A: risk})
    assert row["known_realized"] == 5 and row["profit_rank"] == 1
    assert row["known_open"] is None and row["open_rank"] is None
    assert row["saved_known_open"] == 9
    assert row["checked_at"] == OLD
    assert row["subnet_risk"] == risk
    assert wallets == before


def test_risk_inputs_reclassify_buys_and_keep_matching_transfer_origins():
    buy = dict(id="move", kind="buy", src="old", dst=A, netuid=24,
               extrinsic="100-1")
    move = dict(buy, kind="stake_transfer", src="donor")
    origin = dict(address="donor", event_id="old-buy", extrinsic="50-1")
    receipt = dict(move, origins=[origin])
    old = dict(checked_at=OLD, events=[buy])
    new = dict(checked_at=NOW, events=[move], transfer_details=[receipt])
    _, events = wl._risk_inputs([new, old])
    assert events == [dict(move, origins=[origin])]
    new["events"] = [dict(move, src="different-donor")]
    _, events = wl._risk_inputs([new, old])
    assert "origins" not in events[0]


def test_context_from_current_minute_is_usable_immediately(
        tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 2, 0, 0, 45, tzinfo=timezone.utc)

    monkeypatch.setattr(wl, "datetime", Clock)
    path = tmp_path / "github_out/10/tao_collect.json"
    path.parent.mkdir(parents=True)
    wallet = dict(address=A, current_inventory=True,
                  positions=[dict(netuid=24, alpha=10)])
    report = dict(checked_at=OLD, coverage={A: {}},
                  performance=dict(wallets=[wallet]))
    path.write_text(json.dumps(report), encoding="utf8")
    context_time = "2026-11-02T00:00:30Z"
    context = dict(checked_at=context_time, block_time=context_time,
                   finalized_block=200, subnets=[dict(netuid=24, active=True)])
    context_path = path.parents[1] / "subnet-context.json"
    context_path.write_text(json.dumps(context), encoding="utf8")
    risks, info = wl.Model(tmp_path).risks()
    assert risks[A]["subnets"][0]["context_checked_at"] == context_time
    assert "context_missing" not in risks[A]["flags"]
    assert datetime.fromisoformat(info["assessed_at"]).second == 45


def test_local_api_refreshes_context_without_changing_wallet_time_or_choice(
        tmp_path, monkeypatch):
    cache = tmp_path / "github_out"
    report_path = cache / "10/tao_collect.json"
    report_path.parent.mkdir(parents=True)
    position = dict(netuid=24, alpha=10, known_alpha=0, known_cost_tao=None,
                    current_quote=True, price_tao=2, marked_value_tao=20,
                    quote_at=OLD)
    wallet = dict(address=A, current_inventory=True, known_realized_tao=5,
                  open=dict(known_pnl_tao=9), ranks=dict(profit=1, open=1),
                  positions=[position])
    report = dict(checked_at=OLD, coverage={A: dict(account="complete")},
                  performance=dict(wallets=[wallet]), events=[dict(
                      id="buy", kind="buy", dst=A, netuid=24, alpha=10,
                      tao=2, evidence="transaction", extrinsic="150-1",
                      time=OLD)])
    report_path.write_text(json.dumps(report), encoding="utf8")
    context = dict(checked_at=NOW, block_time=NOW, finalized_block=200,
                   source="local-finney-subnet-context", subnets=[dict(
                       netuid=24, active=True, registration_block=100,
                       generation_id="1:100", subnet_immune=True,
                       subnet_immunity_remaining_blocks=50,
                       liquidity=dict(tao_reserve_tao="500"))])
    context_path = cache / "subnet-context.json"
    context_path.write_text(json.dumps(context), encoding="utf8")
    with wl.server(tmp_path, 0) as server:
        wl.save(server.model.prefs, dict(address=A, choice="mute",
                                         note="Keep"))
        before = wl.preferences(server.model.prefs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def fetch(path):
                with closing(HTTPConnection("127.0.0.1",
                                            server.server_port)) as client:
                    client.request("GET", path)
                    response = client.getresponse()
                    assert response.status == 200
                    return json.loads(response.read())

            first = fetch("/api/watchlist")
            row, = first["wallets"]
            assert row["known_realized"] == 5 and row["known_open"] == 9
            assert row["subnet_risk"]["provisional"] is True
            assert row["checked_at"] == OLD
            assert row["subnet_risk"]["wallet_as_of"] == OLD
            assert first["subnet_context"]["checked_at"] == NOW
            with monkeypatch.context() as patch:
                def unexpected_parse(*args, **kwargs):
                    raise AssertionError("Unchanged context was parsed again")
                patch.setattr(wl.json, "load", unexpected_parse)
                cached = fetch("/api/watchlist")
                assert cached["wallets"][0]["known_open"] == 9
            context["subnets"][0]["active"] = False
            context_path.write_text(json.dumps(context), encoding="utf8")
            updated = fetch("/api/watchlist")["wallets"][0]
            assert updated["known_open"] is None
            assert updated["known_realized"] == 5
            assert updated["saved_known_open"] == 9
            assert updated["subnet_risk"]["invalid_current_valuation"] is True
            assert updated["checked_at"] == OLD
            assert fetch("/api/search?q=" + A[:8])["wallets"][0][
                "known_open"] is None
            assert wl.preferences(server.model.prefs) == before
        finally:
            server.shutdown()
            thread.join()
