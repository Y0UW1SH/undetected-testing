"""Checkpointed coverage of the public Accounts directory."""
import sqlite3

from seo.sources import tao_census as census


def page(n, total, addresses, *, size=2, block=100):
    last = max(1, (total + size - 1) // size)
    return dict(pagination=dict(current_page=n, total_items=total,
                                total_pages=last, per_page=size,
                                next_page=n + 1 if n < last else None),
                data=[dict(address={"ss58": "5" + a * 47}, network="finney",
                           block_number=block, rank=(n - 1) * size + i + 1,
                           balance_total="1000000000", balance_staked="900",
                           balance_staked_alpha_as_tao="500")
                      for i, a in enumerate(addresses)])


def test_resume_does_not_skip_holes_after_sample_of_last_page(tmp_path):
    path = tmp_path / "accounts.sqlite"
    pages = {1: page(1, 5, "AB"), 2: page(2, 5, "CD"), 3: page(3, 5, "E")}
    with sqlite3.connect(path) as db:
        result = census.scan(db, pages.__getitem__, 1, sample_last=True)
    assert result["status"] == "partial"
    assert result["next_page"] == 2 and result["unique_coldkeys"] == 3
    assert result["last_page_sample"]["last_rank"] == 5
    assert result["wallet_histories_checked"] == 0
    assert result["transfer_ties_checked"] == 0
    with sqlite3.connect(path) as db:
        result = census.scan(db, pages.__getitem__, 1)
    assert result["next_page"] is None
    assert result["status"] == "complete_single_block"
    assert result["unique_coldkeys"] == 5 and result["pages_stored"] == 3


def test_moving_rank_duplicate_is_visible_and_not_an_exact_snapshot(tmp_path):
    with sqlite3.connect(tmp_path / "census.sqlite") as db:
        pages = {1: page(1, 4, "AB"), 2: page(2, 4, "BC", block=101)}
        result = census.scan(db, pages.__getitem__, 2)
        assert result["status"] == "traversed_live"
        assert (result["unique_coldkeys"], result["total_reported"]) == (3, 4)
        row = db.execute("SELECT first_page, last_page, rank "
                         "FROM census_accounts WHERE address=?",
                         ("5" + "B" * 47,)).fetchone()
        assert tuple(row) == (1, 2, 3)
        pages[1] = page(1, 6, "AB", block=102)
        pages[3] = page(3, 6, "DE", block=102)
        result = census.scan(db, pages.__getitem__, 1)
        assert result["status"] == "traversed_live"
        assert result["unique_coldkeys"] == 5


def test_failed_page_is_retried_without_committing_partial_rows(tmp_path):
    pages = {1: page(1, 4, "AB"), 2: page(2, 4, "CD")}
    pages[2]["data"][1]["address"] = None
    with sqlite3.connect(tmp_path / "census.sqlite") as db:
        result = census.scan(db, pages.__getitem__, 2)
        assert result["error"] and result["next_page"] == 2
        assert result["unique_coldkeys"] == 2
        pages[2] = page(2, 4, "CD")
        result = census.scan(db, pages.__getitem__, 1)
        assert result["next_page"] is None
        assert result["unique_coldkeys"] == 4


def test_new_page_size_cannot_mix_or_overwrite_existing_checkpoints(tmp_path):
    with sqlite3.connect(tmp_path / "census.sqlite") as db:
        result = census.scan(db, lambda n: page(n, 4, "AB"), 1)
        assert result["next_page"] == 2
        result = census.scan(db, lambda n: page(n, 4, "A", size=1), 1)
        assert result["error"] and result["unique_coldkeys"] == 2
        assert result["page_size"] == 2 and result["next_page"] == 2


def test_finished_snapshot_is_not_exact_at_a_newer_head(tmp_path):
    with sqlite3.connect(tmp_path / "census.sqlite") as db:
        result = census.scan(db, lambda n: page(n, 2, "AB"), 1)
        assert result["status"] == "complete_single_block"
        result = census.scan(db, lambda n: page(n, 2, "AC", block=101), 1)
        assert result["status"] == "traversed_live"
        assert result["latest_anchor_block"] == 101
        assert result["max_row_block"] == 100
