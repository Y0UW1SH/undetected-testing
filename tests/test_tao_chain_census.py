import hashlib
import sqlite3

import pytest

from seo import tao_chain_census as census


def test_resume_keeps_exact_finalized_head_and_waits_for_empty_page():
    db = sqlite3.connect(":memory:")
    keys = sorted(census.PREFIX + hashlib.blake2b(
        bytes([v]) * 32, digest_size=16).hexdigest() + f"{v:02x}" * 32
        for v in (1, 2, 3))
    block = "0x" + "ad" * 32

    def rpc(method, args):
        if method == "chain_getFinalizedHead":
            return block
        if method == "chain_getHeader":
            return {"number": "0x21"}
        prefix, count, cursor, pinned = args
        assert prefix == census.PREFIX and pinned == block
        return [k for k in keys if cursor is None or k > cursor][:count]

    assert census.scan(db, rpc, 2, 1)["complete"] is False
    final = census.scan(db, rpc, 2, 2)
    assert final["complete"] and final["accounts"] == 3
    assert final["finalized_block"] == 33
    assert census.scan(db, lambda *a: pytest.fail("re-read"), 2, 2) == dict(
        final, page_reads_this_run=0)
    with pytest.raises(ValueError):
        census.address(census.PREFIX + "0" * 96)


def test_cannot_use_dwellir_census_in_ci(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    db = tmp_path / "wallets.sqlite"
    assert census.main(["--db", str(db), "--out", str(tmp_path / "r")]) == 2
    assert not db.exists()


def test_switch_ordered_checkpoint_to_disjoint_prefixes_without_duplicates():
    db = sqlite3.connect(":memory:")
    block = "0x" + "a1" * 32
    keys = sorted(census.PREFIX + hashlib.blake2b(
        bytes([v]) * 32, digest_size=16).hexdigest() + f"{v:02x}" * 32
        for v in range(200))

    def rpc(method, args):
        if method == "chain_getFinalizedHead":
            return block
        if method == "chain_getHeader":
            return {"number": "0x12"}
        prefix, limit, after, pinned = args
        assert pinned == block
        return [k for k in keys if k.startswith(prefix)
                and (after is None or k > after)][:limit]

    census.scan(db, rpc, 8, 3)
    partial = census.ranged_scan(db, rpc, 8, 20, 3)
    assert not partial["complete"]
    done = census.ranged_scan(db, rpc, 8, 700, 4)
    assert done["complete"] and done["accounts"] == len(keys)
    stored = [r[0] for r in db.execute("SELECT storage_key FROM "
                                       "native_accounts ORDER BY storage_key")]
    assert stored == keys
