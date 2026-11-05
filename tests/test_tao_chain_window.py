import json
from types import SimpleNamespace

import pytest

from seo import tao_chain_window as window
from seo import tao_local_watch as watch


A, B, HOT = ["5" + letter * 47 for letter in "ABH"]


def rec(name, attrs, ext=1, mod="SubtensorModule"):
    return SimpleNamespace(value=dict(module_id=mod, event_id=name,
                                      attributes=attrs, extrinsic_idx=ext))


class Chain:
    def __init__(self, limit=999):
        self.head = 100
        self.limit = limit
        self.requests = 0
        self.seen = []

    def consume(self):
        if self.requests == self.limit:
            raise watch.RequestBudget
        self.requests += 1

    def get_chain_finalised_head(self):
        self.consume()
        return "hash" + str(self.head)

    def get_block_number(self, block_hash):
        self.consume()
        return int(block_hash[4:])

    def get_block_hash(self, height):
        self.consume()
        return "hash" + str(height)

    def get_events(self, block_hash):
        self.consume()
        self.seen.append(block_hash)
        if block_hash != "hash99":
            return []
        return [rec("StakeRemoved", [A, HOT, 50, 100, 8, 0]),
                rec("StakeAdded", [B, HOT, 50, 80, 9, 0]),
                rec("StakeTransferred", [A, B, HOT, 8, 9, 50]),
                rec("Transfer", [A, B, 2_000_000_000], 2, "Balances"),
                rec("StakeMoved", [A, HOT, 1, HOT, 2, 15], 3),
                rec("StakeAdded", [A, HOT, 30, 40, 4], 4),
                rec("Transfer", [B, A, 1], 5, "Balances"),
                rec("ExtrinsicFailed", {}, 5, "System")]

    def close(self):
        pass


def test_scan_all_wallets_excludes_failed_and_separates_transfer_legs():
    chain = Chain()
    result = window.scan_window(chain, blocks=3)
    assert chain.seen == ["hash98", "hash99", "hash100"]
    assert result["status"] == "complete"
    assert (result["first_block"], result["last_scanned_block"]) == (98, 100)
    assert len(result["raw_asset_events"]) == 6
    assert len(result["movements"]) == 4
    assert result["counts"]["cross_wallet_edges"] == 2
    stake, tao = result["transfer_edges"]
    assert stake["extrinsic"] == "99-0001"
    assert (stake["src"], stake["dst"], stake["alpha_units"],
            stake["dst_alpha_units"]) == (A, B, "100", "80")
    assert tao["tao_rao"] == "2000000000"
    assert result["counts"]["same_wallet_stake_moves"] == 1
    assert "profit" not in result and "trades" not in result


def test_budget_checkpoint_resumes_original_window_across_new_heads():
    chain = Chain(limit=5)
    written = []
    old = window.scan_window(chain, blocks=3, checkpoint=lambda d: written
                             .append(d["last_scanned_block"]))
    assert old["status"] == "rpc_budget" and old["last_scanned_block"] == 98
    assert written[0] is None and 98 in written
    chain = Chain()
    chain.head = 103
    result = window.scan_window(chain, blocks=3, old=old)
    assert chain.seen == ["hash99", "hash100"]
    assert result["last_block"] == 100 and result["status"] == "complete"
    assert len({r["id"] for r in result["movements"]}) == 4


def test_fixed_finalized_end_and_unread_block_not_skipped():
    chain = Chain()
    with pytest.raises(watch.WatchError, match="finalized"):
        window.scan_window(chain, blocks=1, end_block=101)
    chain.get_events = lambda _: (_ for _ in ()).throw(
        RuntimeError("PRIVATE_TEST_SENTINEL"))
    result = window.scan_window(chain, blocks=2, end_block=96)
    assert result["status"] == "read_error"
    assert result["first_block"] == 95
    assert result["last_scanned_block"] is None
    assert "PRIVATE_TEST_SENTINEL" not in json.dumps(result)


def test_ci_bounds_and_transport_failure_never_expose_credential(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CI", "1")
    monkeypatch.setattr(watch, "_endpoint", lambda _: pytest.fail("CI read"))
    assert window.main([]) == 2
    monkeypatch.delenv("CI")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit):
        window.main(["--blocks", "61"])
    with pytest.raises(SystemExit):
        window.main(["--rpc-limit", "201"])
    monkeypatch.setattr(watch, "_endpoint", lambda _: "offline-test-fixture")
    monkeypatch.setattr(watch, "_connect", lambda *_: (_ for _ in ()).throw(
        RuntimeError("PRIVATE_TEST_SENTINEL")))
    assert window.main(["--out", str(tmp_path / "window.json")]) == 2
    output = capsys.readouterr()
    assert "PRIVATE_TEST_SENTINEL" not in (output.out + output.err)
