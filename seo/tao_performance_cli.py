"""Read previously saved TAO wallet performance from SQLite."""
import argparse
import re

from seo import tao_performance
from seo.sources.taostats_wallets import account_url
from seo.tao_wallets import short


BOARDS = {"profit": "Realized profit",
          "open": "Open gains on fully costed positions",
          "profit_partial": "Realized gains on the confirmed portion only",
          "open_partial": "Open gains with incomplete wallet P/L",
          "buy": "Buys before rises",
          "sell": "Sales before falls"}


def _history_count(value):
    count = int(value)
    if count < 0:
        raise argparse.ArgumentTypeError("use a nonnegative number of runs")
    return count


def add_parser(sub):
    p = sub.add_parser("performance", help="review saved wallet rankings, "
                       "per-subnet P/L and rank history")
    p.add_argument("address", nargs="?", help="exact wallet address")
    p.add_argument("--history", type=_history_count, default=5,
                   metavar="N", help="wallet rank history length (default 5)")
    p.set_defaults(func=cmd_performance)


def _amount(value, signed=False):
    if value is None:
        return "unknown"
    number = f"{value:,.9f}".rstrip("0").rstrip(".")
    return "+" + number if signed and value > 0 else number


def _metric(wallet, board):
    if board == "profit":
        return (f"{_amount(wallet['realized_tao'], True)} TAO, "
                f"{wallet['wins']}/{wallet['priced_sales']} priced sales won; "
                f"open P/L "
                f"{_amount(wallet.get('gross_unrealized_tao'), True)} TAO")
    if board in ("open", "open_partial"):
        m = wallet["open"]
        if board == "open":
            days = m.get("days_since_addition")
            age = (f", last observed addition {days:g} days ago"
                   if days is not None else "")
            return (f"{_amount(wallet['gross_unrealized_tao'], True)} TAO "
                    f"unrealized ({_amount(m['return_pct'], True)}%); "
                    f"realized {_amount(wallet['realized_tao'], True)} TAO, "
                    f"{wallet['priced_sales']} priced sales{age}")
        share = 100 * m["known_value_fraction"]
        unresolved = ("total open P/L unknown" if m["return_pct"] is None
                      else "open P/L fully priced, total wallet P/L unknown")
        return (f"{_amount(m['known_pnl_tao'], True)} TAO unrealized on "
                f"known units ({_amount(m['known_return_pct'], True)}%); "
                f"basis confirmed for {share:.1f}% of marked value, "
                + unresolved)
    if board == "profit_partial":
        covered = 100 * wallet["known_sale_value_fraction"]
        return (f"{_amount(wallet['known_realized_tao'], True)} TAO realized "
                f"on confirmed units; cost traced for {covered:.1f}% of sale "
                "proceeds, total realized P/L unknown")
    side = wallet["timing"][board]
    if "mature_trades" in side:
        return (f"{side['favorable']}/{side['evaluated']} favorable across "
                f"{side['favorable_days']} favorable UTC days; quotes "
                f"evaluated {side['evaluated']}/{side['mature_trades']} "
                "mature trades")
    return (f"{side['favorable']}/{side['evaluated']} favorable across "
            f"{side['trade_days']} trade days")


def _headings(settings):
    move = _amount(settings.get("move_pct"))
    return dict(profit=BOARDS["profit"], open=BOARDS["open"],
                profit_partial=BOARDS["profit_partial"],
                open_partial=BOARDS["open_partial"],
                buy=f"{BOARDS['buy']} of at least {move}% at 24-30h",
                sell=f"{BOARDS['sell']} of at least {move}% at 24-30h")


def _print_boards(run):
    by_address = {w["address"]: w for w in run["wallets"]}
    print(f"Wallet performance at {run['checked_at']}")
    for board, heading in _headings(run["settings"]).items():
        print(f"\n{heading}")
        if not _board_measured(run, board):
            print("This rank was not measured for this run.")
            continue
        addresses = run["rankings"].get(board, [])
        if not addresses:
            print("No qualifying wallets.")
        for rank, addr in enumerate(addresses, 1):
            print(f"{rank:>3}. {addr}  {_metric(by_address[addr], board)}")


def _board_measured(run, board):
    return not run["wallets"] or any(
        board in w["ranks"] for w in run["wallets"])


def _partial_sales(row):
    if row.get("partial_sales"):
        gain = _amount(row["known_realized_tao"], True)
        unresolved = _amount(row["unresolved_sale_proceeds_tao"])
        print(f"  Confirmed sold units: realized {gain} TAO across fully "
              f"and partially priced sales. {unresolved} TAO of sale "
              "proceeds have unresolved cost.")


def _print_position(p):
    quote = p.get("quote_at") or "unknown time"
    freshness = "current quote" if p.get("current_quote") else "older quote"
    if not p.get("quote_at"):
        freshness = "no quote"
    if p.get("current_inventory") is False:
        freshness += ", inventory unavailable this run"
    entry, mark = _amount(p["buy_price_tao"]), _amount(p["price_tao"])
    print(f"SN{p['netuid']}  {_amount(p['alpha'])} alpha, "
          f"entry {entry}, mark {mark}"
          f" TAO/alpha ({freshness}, {quote})")
    realized = (p["realized_tao"] if p["priced_sales"]
                or not p["unpriced_sales"] else None)
    print(f"  marked value {_amount(p['marked_value_tao'])} TAO, "
          f"realized {_amount(realized, True)} TAO "
          f"({p['priced_sales']} priced, {p['unpriced_sales']} unpriced), "
          f"unrealized {_amount(p['gross_unrealized_tao'], True)} TAO")
    _partial_sales(p)
    print(f"  known {_amount(p['known_alpha'])} alpha "
          f"(cost {_amount(p['known_cost_tao'])} TAO), "
          f"estimated {_amount(p['probable_alpha'])} alpha "
          f"(cost {_amount(p['probable_cost_tao'])} TAO), "
          f"unknown {_amount(p['unknown_alpha'])} alpha")
    if p["probable_alpha"] or p["unknown_alpha"]:
        known_entry = _amount(p.get("known_buy_price_tao"))
        known_pnl = _amount(p["known_unrealized_tao"], True)
        estimated_pnl = _amount(p["probable_unrealized_tao"], True)
        print(f"  entry on known units {known_entry} TAO/alpha, unrealized "
              f"on known units {known_pnl} TAO, on estimated units "
              f"{estimated_pnl} TAO")


def _print_links(wallet):
    links = wallet["transfer_links"]
    if not links:
        return
    print("\nRecent transfer links")
    address = wallet["address"]
    for link in links[:5]:
        outgoing = link["src"] == address
        peer = link["dst"] if outgoing else link["src"]
        asset = ("TAO" if link["netuid"] is None
                 else f"SN{link['netuid']} alpha")
        evidence = link["evidence"].replace("_", " ")
        print(f"  {link['last_seen']}  {asset} "
              f"{'to' if outgoing else 'from'} {peer} ({evidence})")
        xid = str(link.get("extrinsic") or "")
        if re.fullmatch(r"[0-9]+-[0-9]+", xid):
            print(f"    https://taostats.io/extrinsic/{xid}")
        elif re.fullmatch(r"[A-Za-z0-9]+", peer):
            print(f"    {account_url(peer)}")


def _print_detail(wallet, stamp, past):
    print(f"{wallet['address']} at {stamp}")
    print(account_url(wallet["address"]))
    coverage = wallet["coverage"]
    print("Coverage  " + ", ".join(
        f"{label} {coverage.get(label, 'unavailable')}"
        for label in ("account", "transactions", "transfers")))
    if wallet.get("account_as_of"):
        print(f"Account balances observed at {wallet['account_as_of']}")
    reasons = wallet.get("screens", []) + wallet.get("evidence_gaps", [])
    if reasons:
        print("Ranking limits  " + ", ".join(
            r.replace("_", " ") for r in reasons))
    if wallet["priced_sales"]:
        print(f"Gross realized on {wallet['priced_sales']} priced sales "
              f"{_amount(wallet['realized_tao'], True)} TAO "
              f"({wallet['wins']} wins, {wallet['unpriced_sales']} unpriced).")
    elif wallet["unpriced_sales"]:
        print(f"Realized P/L unknown. All {wallet['unpriced_sales']} sales "
              "include stake with unverified cost.")
    else:
        print("No sales observed.")
    _partial_sales(wallet)
    if "gross_unrealized_tao" in wallet:
        unrealized = _amount(wallet["gross_unrealized_tao"], True)
        total = _amount(wallet.get("gross_total_pnl_tao"), True)
        print(f"Gross unrealized {unrealized} TAO on open positions. "
              f"Gross total P/L {total} TAO.")
    opened = wallet.get("open", {})
    if opened.get("known_pnl_tao") is not None:
        known = _amount(opened["known_pnl_tao"], True)
        rate = _amount(opened["known_return_pct"], True)
        share = opened["known_value_fraction"]
        print(f"Confirmed open portion {known} TAO ({rate}%); known basis "
              f"covers {share:.1%} of marked subnet value.")
        if opened.get("last_observed_addition_at"):
            print("Latest observed buy or stake received on these subnets "
                  + opened["last_observed_addition_at"])
    if any(s.get("mature_trades") for s in wallet["timing"].values()):
        print("\nTiming on observed mature trades")
        for side in ("buy", "sell"):
            print(f"  {side}: {_metric(wallet, side)}")
    print("\nSubnet positions and gross P/L")
    if not wallet["positions"]:
        print("No subnet positions or sales recorded.")
    for p in wallet["positions"]:
        _print_position(p)
    _print_links(wallet)
    if past:
        print("\nRank history (newest first)")
        print("Run                      Profit Open PartR PartO Buy Sell   "
              "Realized TAO  Unrealized TAO")
        for row in past:
            ranks = "  ".join(str(row["ranks"].get(k) or "-").rjust(4)
                              for k in BOARDS)
            realized = _amount(row["realized_tao"] if row["priced_sales"]
                               or not row["unpriced_sales"] else None, True)
            unrealized = _amount(row.get("gross_unrealized_tao"), True)
            print(f"{row['checked_at']}  {ranks}  "
                  f"{realized:>12}  {unrealized:>14}")


def cmd_performance(args, store):
    if args.address:
        past = tao_performance.history(store.conn, args.address,
                                       limit=max(args.history, 1))
        if past:
            _print_detail(past[0], past[0]["checked_at"], past[:args.history])
            return 0
    run = tao_performance.load_run(store.conn)
    if run is None:
        print("No saved performance runs. Use tao collect on this database.")
        return 0
    if not args.address:
        _print_boards(run)
        return 0
    print(f"No saved performance for {args.address}.")
    return 1


def _account_link(address):
    if not re.fullmatch(r"[A-Za-z0-9]+", address):
        return "Unknown wallet"
    return f"[{short(address)}]({account_url(address)})"


def append_summary(report, out):
    """Show screened rankings and keep partial gains separately labeled."""
    performance = report.get("performance", report)
    print("\n### Wallet performance\n", file=out)
    if not performance or "rankings" not in performance:
        print("No saved performance rankings.", file=out)
        return
    wallets = {w["address"]: w for w in performance["wallets"]}
    headings = _headings(performance["settings"])
    rankings = performance["rankings"]
    for board in BOARDS:
        print(f"**{headings[board]}**\n", file=out)
        if not _board_measured(performance, board):
            print("This rank was not measured in this run.\n", file=out)
            continue
        if not rankings.get(board):
            print("No qualifying wallets.\n", file=out)
            continue
        limit = 10 if board == "profit" else 3
        for rank, addr in enumerate(rankings[board][:limit], 1):
            print(f"{rank}. {_account_link(addr)}  "
                  f"{_metric(wallets[addr], board)}", file=out)
        print(file=out)
