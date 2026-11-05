"""Offline CLI/Actions presentation for a saved wallet research run."""
import argparse
from io import StringIO

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
