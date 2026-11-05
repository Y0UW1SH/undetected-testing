"""One browser pass for subnet prices, wallet discovery and trading history."""
import argparse
import json
import math
import os
import sqlite3
from collections import defaultdict, deque
from datetime import timedelta
from itertools import zip_longest
from pathlib import Path

from seo import cli, tao_ledger, tao_wallets as wallets
from seo.models import ChallengeError
from seo.sources import taostats, taostats_wallets as site, tao_history


def _number(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("choose a nonnegative number")
    return n


def _nonnegative(value):
    n = float(value)
    if n < 0 or not math.isfinite(n):
        raise argparse.ArgumentTypeError("choose a finite nonnegative number")
    return n


def add_parser(sub):
    p = sub.add_parser("collect", help="collect subnet prices, wallet "
                       "positions, transactions and transfers in one session")
    wallets._browser_flags(p)
    p.add_argument("--netuids", help="comma-separated subnet IDs to scan")
    p.add_argument("--subnets", type=_number, default=0,
                   help="number of top subnets (0 = every subnet)")
    p.add_argument("--top", type=_number, default=20,
                   help="candidate wallets to inspect (0 = all discovered)")
    from seo.sources import tao_extrinsic
    p.add_argument("--extrinsic", type=tao_extrinsic.parse_target,
                   action="append", default=[], metavar="ID[:EVENT,...]",
                   help="also verify a known native Taostats detail record "
                   "(repeatable); independent of wallet table coverage")
    p.add_argument("--address", action="append", type=wallets._check_address,
                   default=[], help="also inspect this wallet (repeatable)")
    p.add_argument("--addresses-only", action="store_true",
                   help="skip market discovery and inspect supplied or "
                   "followed addresses and their transfer peers")
    p.add_argument("--new-wallets", type=_number, default=10,
                   help="maximum extra recipient accounts per run")
    p.add_argument("--link-depth", type=_number, default=2,
                   help="maximum transfer hops for discovered wallets")
    p.add_argument("--min-sales", type=_number, default=2)
    p.add_argument("--min-timing", type=_number, default=3)
    p.add_argument("--timing-move-pct", type=_nonnegative, default=5.)
    p.add_argument("--max-balance-tao", type=_nonnegative, default=100000.,
                   help="screen larger wallets from rankings and further "
                   "link discovery (0 disables)")
    p.add_argument("--max-activity-24h", type=_number, default=100,
                   help="screen wallets above this observed daily event count "
                   "(0 disables)")
    p.add_argument("--price-history", help="prior subnet SQLite database "
                   "for timing (default: data/tao.sqlite when available)")
    p.add_argument("--history-pages", type=_number, default=3,
                   help="maximum pages per wallet tab; first page = newest")
    p.add_argument("--follow-top", type=_number, default=0,
                   help="follow up to N profitable wallets with 2+ priced "
                   "sales and a win rate over 50 percent")
    p.add_argument("--out", default=os.path.join("data", "tao_collect.json"))
    p.set_defaults(func=collect)


def _schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS wallet_events "
               "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS wallet_inventory "
               "(address TEXT, time TEXT, batch TEXT, payload TEXT NOT NULL, "
               "PRIMARY KEY(address, time))")
    db.execute("CREATE TABLE IF NOT EXISTS wallet_follow_links "
               "(src TEXT, dst TEXT, PRIMARY KEY(src, dst))")
    db.commit()


def _read(store):
    events = [json.loads(r[0]) for r in store.conn.execute(
        "SELECT payload FROM wallet_events")]
    checkpoints = []
    for r in store.conn.execute("SELECT payload FROM wallet_inventory"):
        p = json.loads(r[0])
        p["units"] = {int(n): qty for n, qty in p["units"].items()}
        checkpoints.append(p)
    return tao_ledger.net_transfers(events), checkpoints


def _save_events(db, events):
    db.executemany("INSERT INTO wallet_events VALUES (?,?) ON CONFLICT(id) "
                   "DO UPDATE SET payload=excluded.payload "
                   "WHERE wallet_events.payload <> excluded.payload",
                   [(e["id"], json.dumps(e)) for e in events])


def _discover(args, session, store, stamp, coverage):
    prices = taostats.fetch_subnets(session)
    if not prices:
        raise ValueError("no subnet prices returned")
    store.save(prices, stamp=stamp)
    if args.addresses_only:
        return {}, "skipped"
    if args.netuids:
        netuids = list(dict.fromkeys(int(x) for x in args.netuids.split(",")))
    else:
        prices.sort(key=lambda p: p.market_cap_tao or 0, reverse=True)
        netuids = [p.netuid for p in prices if p.netuid != 0]
        netuids = netuids[:args.subnets or None]
    neurons = []
    for n in netuids:
        session.pause()
        try:
            found = site.fetch_metagraph(session, n)
        except ChallengeError:
            found = []
        coverage[n] = len(found)
        neurons.extend(found)
        print("SN%d: %d neurons" % (n, len(found)), flush=True)
    earners = site.earners(neurons)
    ranked = wallets.pick_candidates(earners, args.top or len(earners))
    session.pause()
    recent, status = tao_history.fetch_recent_trades(session)
    recent = [e for e in recent if e["netuid"] in netuids]
    with store.conn:
        _save_events(store.conn, recent)
    active = list(dict.fromkeys(e["owner"] for e in recent if e.get("owner")))
    # Reserve room for both active traders and high-earning operators.
    candidates = {}
    for pair in zip_longest(active, [e["coldkey"] for e in ranked]):
        for a in pair:
            if a and wallets.ADDRESS_RE.fullmatch(a):
                candidates[a] = earners.get(a)
    selected = list(candidates)[:args.top or None]
    return {a: candidates[a] for a in selected}, status


def _inspect(args, session, store, address, earner, stamp, coverage):
    session.pause()
    try:
        snap, positions = site.fetch_account(session, address, "collect")
    except ChallengeError:
        snap, positions = None, []
    if not snap or snap.address != address:
        coverage["account"] = "unavailable"
        return None, []
    if earner:
        snap.rewards_per_day = earner["rewards_per_day"]
        snap.subnets = ",".join(map(str, sorted(earner["subnets"])))
    coverage["account"] = "complete"
    coverage["balance_total"] = snap.balance_total
    coverage["holds_subnets"] = any(
        p.netuid and p.alpha and p.alpha > 0 for p in positions)
    rows = []
    for tab in ("transactions", "transfers"):
        session.pause()
        window = {}
        events, status = tao_history.fetch_history(
            session, address, tab, args.history_pages, window=window)
        if "since" in window:
            coverage[tab + "_since"] = window["since"]
        diagnostic = window.get("diagnostic", {})
        if diagnostic and (status != "complete"
                           or diagnostic.get("verification_control")):
            coverage[tab + "_diagnostic"] = diagnostic
        coverage[tab] = status
        coverage[tab + "_rows"] = len(events)
        rows.extend(events)
    when = tao_history.timestamp(snap.snapshot_at)
    units = defaultdict(float)
    for p in positions:
        if p.netuid is not None and p.alpha is not None:
            units[p.netuid] += p.alpha
    with store.conn:
        _save_events(store.conn, rows)
        if when and snap.positions_complete:
            checkpoint = dict(address=address, time=when, batch=stamp,
                              units=units, swap=snap.swapped_to)
            store.conn.execute(
                "INSERT OR IGNORE INTO wallet_inventory VALUES (?,?,?,?)",
                (address, when, stamp, json.dumps(checkpoint)))
        store.conn.execute("UPDATE followed_wallets SET balance_at_follow=? "
                           "WHERE address=? AND balance_at_follow IS NULL",
                           (snap.balance_total, address))
        store.save([snap, *positions], stamp=stamp)
    return snap, rows


def _prices(store, path):
    """Reuse the scheduled subnet workflow's actual observation timestamps."""
    stored = store.rows("subnet_stats")
    other = Path(path or "data/tao.sqlite").resolve()
    if other == Path(store.path).resolve() or not (path or other.exists()):
        return stored
    if not other.is_file():
        raise SystemExit("Price history database does not exist: %s" % other)
    conn = sqlite3.connect(other.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        combined = {(r["netuid"], r["fetched_at"]): dict(r)
                    for r in conn.execute("SELECT netuid, fetched_at, "
                                          "price_tao FROM subnet_stats")}
    finally:
        conn.close()
    combined.update({(r["netuid"], r["fetched_at"]): r for r in stored})
    return sorted(combined.values(), key=lambda r: r["fetched_at"])


def _link_candidates(address, snap, rows, followed):
    """Trace incoming stake to its cost origin before exploring other peers."""
    incoming, outgoing, cash = set(), set(), set()
    for e in rows:
        src, dst = e["src"], e["dst"]
        if e["kind"] == "stake_transfer":
            if dst == address:
                incoming.add(src)
            if src == address:
                outgoing.add(dst)
        elif address in followed and e["kind"] == "tao_transfer":
            if src == address:
                cash.add(dst)
    peers = [snap.swapped_to] + sorted(a for a in incoming if a)
    peers += sorted(a for a in outgoing | cash if a)
    return [a for a in dict.fromkeys(peers) if a and a != address
            and wallets.ADDRESS_RE.fullmatch(a)]


def collect(args, store):
    if args.history_pages < 1:
        raise SystemExit("--history-pages must be at least 1")
    if min(args.min_sales, args.min_timing) < 1:
        raise SystemExit("--min-sales and --min-timing must be at least 1")
    if len(args.extrinsic) > 5:
        raise SystemExit("Choose at most five extrinsics per collection")
    _schema(store.conn)
    stamp = wallets._new_stamp(store)
    previous_price = store.conn.execute(
        "SELECT MAX(fetched_at) FROM subnet_stats").fetchone()[0]
    if previous_price and stamp <= previous_price:
        stamp = (wallets._parse_ts(previous_price)
                 + timedelta(seconds=1)).isoformat()
    coverage, graphs, swaps = {}, {}, []
    followed = {r["address"] for r in store.followed()}
    if args.addresses_only and not (args.address or followed):
        raise SystemExit("--addresses-only needs --address or a watchlist")
    session = cli._session(args)
    # Allow Taostats to load the verification widget used by its history tabs.
    session.ad_block = False
    with session:
        try:
            earners, market_status = _discover(
                args, session, store, stamp, graphs)
        except ValueError as exc:
            print("Collection failed: %s" % exc)
            return 1
        roots = list(dict.fromkeys([*sorted(followed), *args.address,
                                    *earners]))
        queue = deque((a, 0) for a in roots)
        queued = set(roots)
        deferred = set()
        while queue:
            address, depth = queue.popleft()
            if address in coverage:
                continue
            status = coverage[address] = {}
            snap, rows = _inspect(args, session, store, address,
                                  earners.get(address), stamp, status)
            print("%s %s" % (address, status), flush=True)
            if not snap:
                continue
            if snap.swapped_to and wallets.ADDRESS_RE.fullmatch(
                    snap.swapped_to) and snap.swapped_to != address:
                swaps.append((address, snap.swapped_to))
            if depth >= args.link_depth:
                continue
            since = (wallets._parse_ts(stamp) - timedelta(days=1)).isoformat()
            activity = len({e["id"] for e in rows if since <= e["time"]})
            if ((args.max_balance_tao and snap.balance_total is not None
                 and snap.balance_total > args.max_balance_tao)
                    or (args.max_activity_24h
                        and activity > args.max_activity_24h)):
                status["link_expansion"] = "screened"
                continue
            for peer in _link_candidates(address, snap, rows, followed):
                if peer in queued:
                    continue
                if len(queued) - len(roots) >= args.new_wallets:
                    deferred.add(peer)
                else:
                    queue.append((peer, depth + 1))
                    queued.add(peer)
        details = []
        if args.extrinsic:
            from seo.sources.tao_extrinsic import fetch_evidence
            for ident, indices in dict.fromkeys(args.extrinsic):
                session.pause()
                details.append(fetch_evidence(session, ident, indices))
    events, checkpoints = _read(store)
    inferred = tao_ledger.inferred_links(checkpoints, events)
    subnet_stats = {}
    positions, stats = tao_ledger.portfolio(events + inferred, checkpoints,
                                            subnet_stats=subnet_stats)
    prices = _prices(store, args.price_history)
    timing = tao_ledger.timing(events, prices)
    from seo import tao_performance
    performance = tao_performance.record_run(
        store, stamp, positions, stats, subnet_stats, events + inferred,
        prices, coverage, min_sales=args.min_sales, min_timing=args.min_timing,
        move_pct=args.timing_move_pct, max_balance_tao=args.max_balance_tao,
        max_activity_24h=args.max_activity_24h)
    traders = []
    for a, s in stats.items():
        if s["priced_sales"]:
            traders.append(dict(address=a, **s,
                                win_rate=s["wins"] / s["priced_sales"],
                                timing=timing.get(a, dict(evaluated=0,
                                                          favorable=0))))
    traders.sort(key=lambda s: (s["realized_tao"], s["priced_sales"]),
                 reverse=True)
    for a in performance["rankings"]["profit"][:args.follow_top]:
        store.follow(a)
    screened = {w["address"] for w in performance["wallets"] if w["screens"]}
    watched = {r["address"] for r in store.followed()}
    transfers = [e for e in events + inferred if e["kind"] in (
        "stake_transfer", "tao_transfer")]
    reachable = swaps + [(e["src"], e["dst"]) for e in transfers
                         if e["kind"] == "stake_transfer" or coverage.get(
                             e["dst"], {}).get("holds_subnets")]
    # Record processed links so explicit unfollows survive a later replay.
    for src, dst in reachable:
        if src not in watched or not dst or src == dst or dst in screened:
            continue
        with store.conn:
            new = store.conn.execute(
                "INSERT OR IGNORE INTO wallet_follow_links VALUES (?,?)",
                (src, dst)).rowcount
            if new:
                store.follow(dst)
    report = dict(checked_at=stamp, metagraph_neurons=graphs,
                  coverage=coverage, candidates=list(earners),
                  recent_trades_status=market_status,
                  deferred_recipients=sorted(deferred),
                  positions=list(positions.values()), traders=traders,
                  performance=performance, extrinsics=details,
                  transfers=transfers, swaps=swaps, events=events,
                  unpriced_sales={a: s["unpriced_sales"] for a, s
                                  in stats.items() if s["unpriced_sales"]},
                  following=[r["address"] for r in store.followed()])
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf8") as f:
        json.dump(report, f, indent=2)
    print("Collected %d account(s); stored %d unique movements; "
          "%d wallet(s) with priced sales. Report: %s" % (
              len(coverage), len(events), len(traders), args.out))
    missing = sum(c.get(t) == "unavailable" for c in coverage.values()
                  for t in ("transactions", "transfers"))
    if missing:
        print("WARNING: %d wallet history tabs unavailable." % missing)
        if os.environ.get("GITHUB_ACTIONS"):
            print("::warning::%d wallet history tabs unavailable." % missing)
    have_accounts = any(c.get("account") == "complete"
                        for c in coverage.values())
    return 0 if have_accounts else 1
