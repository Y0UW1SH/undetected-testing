"""Offline CLI/Actions presentation for a saved wallet research run."""
import argparse
from io import StringIO
import json

from seo import storage
from tests.test_tao_ledger import day, event
from tests.test_tao_performance import COMPLETE, price, record


def run(store, *args):
    from seo import tao_performance_cli as view
    parser = argparse.ArgumentParser()
    view.add_parser(parser.add_subparsers(required=True))
    arguments = parser.parse_args(["performance", *args])
    return arguments.func(arguments, store)


def test_empty_performance_output_requires_no_browser(tmp_path, capsys):
    from seo.tao_performance_cli import append_summary
    store = storage.Storage(str(tmp_path / "empty.sqlite"))
    try:
        assert run(store) == 0
        assert "tao collect" in capsys.readouterr().out
        out = StringIO()
        append_summary({}, out)
        assert "No saved performance" in out.getvalue()
    finally:
        store.close()


def test_wallet_detail_shows_prices_gains_transfers_and_rank_history(
        tmp_path, capsys):
    from seo.tao_performance_cli import append_summary
    a = "5" + "A" * 47
    b = "5" + "B" * 47
    events = [event(1, "buy", None, a, 10, 5),
              event(2, "sell", a, None, 4, 3),
              event(3, "sell", a, None, 1, 1.5),
              event(3, "stake_transfer", a, b, 1, id="3b",
                    extrinsic="1234-0003")]
    prices = [price(8, 1, 4), price(8, 1.5, 5)]
    for n in (4, 5):
        report = record(tmp_path, events, prices[:n - 3], day(n),
                        {a: COMPLETE, b: COMPLETE})
    store = storage.Storage(str(tmp_path / "wallets.sqlite"))
    try:
        assert run(store, a, "--history", "1") == 0
        text = capsys.readouterr().out
        assert "SN8" in text and a in text
        assert "entry 0.5" in text and "mark 1.5" in text
        assert "realized +2" in text and "unrealized +4" in text
        assert "unknown 0" in text and "estimated 0" in text
        assert "https://taostats.io/extrinsic/1234-0003" in text
        history = text.split("Rank history", 1)[1]
        assert day(5) in history and day(4) not in history

        assert run(store) == 0
        assert a in capsys.readouterr().out
        out = StringIO()
        append_summary({"performance": report}, out)
        assert "+2 TAO" in out.getvalue()
        assert a in out.getvalue()
    finally:
        store.close()


def test_reports_unknown_when_no_sale_can_be_priced(tmp_path, capsys):
    address = "5" + "C" * 47
    record(tmp_path, [event(1, "sell", address, None, 2, 3)],
           [price(8, 1.5, 4)], day(4), {address: COMPLETE})
    store = storage.Storage(str(tmp_path / "wallets.sqlite"))
    try:
        assert run(store, address) == 0
        assert "Realized P/L unknown" in capsys.readouterr().out
    finally:
        store.close()


def test_summary_distinguishes_unsold_gains_from_partial_and_realized(
        tmp_path, capsys):
    from seo.tao_performance_cli import append_summary

    a = "5" + "A" * 47
    b = "5" + "B" * 47
    events = [event(1, "buy", None, a, 2, 1),
              event(1, "buy", None, b, 2, 1, id="b-buy"),
              event(2, "stake_transfer", None, b, 2)]
    report = record(tmp_path, events, [price(8, 1, 4)], day(4),
                    {a: COMPLETE, b: COMPLETE})
    out = StringIO()
    append_summary({"performance": report}, out)
    text = out.getvalue()
    assert "Open gains on fully costed positions" in text and a in text
    assert "unrealized (+100%)" in text and "0 priced sales" in text
    assert "basis confirmed for 50.0% of marked value" in text and b in text
    assert "total open P/L unknown" in text
    store = storage.Storage(str(tmp_path / "wallets.sqlite"))
    try:
        assert run(store, b) == 0
        detail = capsys.readouterr().out
        assert "Confirmed open portion +1 TAO" in detail
        assert "Gross total P/L unknown" in detail
    finally:
        store.close()


def test_partial_realized_sales_show_known_portion_only(tmp_path, capsys):
    a = "5" + "A" * 47
    events = [event(1, "buy", None, a, 2, 1),
              event(2, "stake_transfer", None, a, 2),
              event(3, "sell", a, None, 4, 4)]
    record(tmp_path, events, [price(8, 1., 4)], day(4), {a: COMPLETE})
    store = storage.Storage(str(tmp_path / "wallets.sqlite"))
    try:
        assert run(store, a) == 0
        text = capsys.readouterr().out
        assert "Confirmed sold units: realized +1 TAO" in text
        assert "2 TAO of sale proceeds have unresolved cost" in text
        assert "Realized P/L unknown" in text
    finally:
        store.close()


def test_older_performance_runs_have_no_fabricated_new_rank(tmp_path, capsys):
    from seo.tao_performance import load_run

    record(tmp_path, [event(1, "buy", None, "A", 2, 1)],
           [price(8, 1, 4)], day(4), {"A": COMPLETE})
    store = storage.Storage(str(tmp_path / "wallets.sqlite"))
    try:
        wallet = load_run(store.conn)["wallets"][0]
        wallet.pop("open")
        wallet["ranks"] = dict(profit=None, buy=None, sell=None)
        with store.conn:
            store.conn.execute("UPDATE wallet_performance SET payload=?",
                               (json.dumps(wallet),))
        assert run(store) == 0
        text = capsys.readouterr().out
        assert "This rank was not measured for this run" in text
    finally:
        store.close()
