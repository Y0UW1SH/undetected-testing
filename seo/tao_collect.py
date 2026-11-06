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
from typing import NamedTuple

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
    p.add_argument("--watch-address", action="append",
                   type=wallets._check_address, default=[],
                   help="monitor this wallet across runs (repeatable)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--addresses-only", action="store_true",
                      help="skip market discovery and inspect supplied or "
                      "followed addresses and their transfer peers")
    mode.add_argument("--holders-only", action="store_true",
                      help="sample subnet holders without rescanning unrelated"
                      " traders, metagraphs or the existing watchlist")
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
    p.add_argument("--history-charts", type=_number, default=8,
                   help="maximum subnet charts for older trade timing (0–12)")
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


def _cache_call(db, saved, detail):
    # Partial means a separately requested event page was unavailable. The
    # extrinsic page itself still contained the typed native call and signer.
    if detail.get("status") not in ("observed", "partial"):
        return
    xid = detail["extrinsic_id"]
    if (detail["status"] == "partial" and saved.get(xid, {}).get("status")
            == "observed"):
        return
    saved[xid] = detail
    with db:
        db.execute("INSERT OR REPLACE INTO wallet_move_evidence VALUES (?,?)",
                   (xid, json.dumps(detail)))


class _Flow(NamedTuple):
    wallet: str
    netuid: int
    before: int
    alpha: float
    tao: float


def _donors(incoming, wallet, downstream=None):
    """Largest corroborated incoming flows per donor/subnet, capped per hop."""
    best = {}
    for index, e in incoming.get(wallet, ()):
        if downstream and (index >= downstream.before
                           or e["netuid"] != downstream.netuid):
            continue
        units = e["alpha"]
        if downstream:
            units = min(units, downstream.alpha)
        value = (e.get("tao") or 0.) * units / e["alpha"]
        if downstream and downstream.tao:
            cap = downstream.tao * units / downstream.alpha
            value = min(value, cap) if value else cap
        flow = _Flow(e["src"], e["netuid"], index, units, value)
        key = flow.wallet, flow.netuid
        weight = flow.tao, flow.alpha, flow.before
        if key not in best or weight > best[key][0]:
            best[key] = weight, flow
    return [flow for _, flow in sorted(best.values(), reverse=True)[:6]]


def _receipt_order(events, missing, watched, limit=24):
    """Order move receipts by stake that could reach a followed wallet."""
    if not missing:
        return []
    incoming, moves = defaultdict(list), defaultdict(list)
    direct, upstream, newest, owners = {}, {}, {}, {}
    # net_transfers returns a stable chain order. An index permits a strict
    # cutoff even for two events in the same block or extrinsic.
    for index, e in enumerate(events):
        xid = e.get("extrinsic")
        if e["kind"] == "stake_reallocation" and xid in missing:
            newest[xid] = index
            owners[xid] = e["src"]
            if (e.get("alpha") or 0.) <= 0:
                continue
            moves[e["src"], e["netuid"]].append((index, e))
            if e["src"] in watched:
                direct[xid] = max(direct.get(xid, 0.), e.get("tao") or 0.)
        elif (e["kind"] == "stake_transfer" and e["evidence"] == "transaction"
              and e["src"] and e["dst"] and e["src"] != e["dst"]
              and e.get("transfer_side") not in {"out", "in", "unknown"}
              and e.get("dst_netuid") in (None, e.get("netuid"))
              and e.get("netuid") and (e.get("alpha") or 0.) > 0):
            incoming[e["dst"]].append((index, e))

    flows = [flow for wallet in sorted(watched)
             for flow in _donors(incoming, wallet)]
    for hop in range(2):
        # One busy recipient cannot make the collector walk an entire graph.
        # Keep earlier ancestry separate by subnet, time and amount.
        paths = sorted(flows, key=lambda f: (f.tao, f.alpha, f.before),
                       reverse=True)[:limit]
        flows = []
        for path in paths:
            for index, e in moves.get((path.wallet, path.netuid), ()):
                if index >= path.before:
                    continue
                units = min(e["alpha"], path.alpha)
                value = (path.tao * units / path.alpha if path.tao else
                         (e.get("tao") or 0.) * units / e["alpha"])
                xid = e["extrinsic"]
                upstream[xid] = max(upstream.get(xid, 0.), value)
            if hop == 0:
                flows.extend(_donors(incoming, path.wallet, path))

    def order(scores):
        return sorted(scores, key=lambda x: (scores[x], newest[x], x),
                      reverse=True)

    # Reserve room for both inherited cost and the watched wallets' own moves.
    half = limit // 2
    ancestry = []
    per_donor = defaultdict(int)
    for xid in order(upstream):
        donor = owners[xid]
        if per_donor[donor] < 4:
            ancestry.append(xid)
            per_donor[donor] += 1
    watched_moves = order(direct)
    remaining = sorted(missing, key=lambda x: (newest.get(x, -1), x),
                       reverse=True)
    preferred = ancestry[:half] + watched_moves[:limit - half]
    return list(dict.fromkeys(preferred + ancestry + watched_moves
                              + remaining))[:limit]


def _read(store, session=None, explicit=()):
    events = [json.loads(r[0]) for r in store.conn.execute(
        "SELECT payload FROM wallet_events")]
    checkpoints = []
    for r in store.conn.execute("SELECT payload FROM wallet_inventory"):
        p = json.loads(r[0])
        p["units"] = {int(n): qty for n, qty in p["units"].items()}
        checkpoints.append(p)
    from seo.sources import tao_extrinsic
    db = store.conn
    db.execute("CREATE TABLE IF NOT EXISTS wallet_move_evidence "
               "(extrinsic TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    saved = {r[0]: json.loads(r[1]) for r in db.execute(
        "SELECT extrinsic,payload FROM wallet_move_evidence")}
    requested = set()
    for detail in explicit:
        requested.add(detail["extrinsic_id"])
        _cache_call(db, saved, detail)
    normalized = tao_ledger.net_transfers(events)
    candidates = {e["extrinsic"] for e in normalized
                  if e.get("evidence") == "matching_trade_legs"}
    if session:
        watched = {r["address"] for r in store.followed()}
        missing = candidates - set(saved) - requested
        for xid in _receipt_order(normalized, missing, watched):
            try:
                item = tao_extrinsic.fetch_evidence(session, xid)
            except (ValueError, ChallengeError):
                continue
            _cache_call(db, saved, item)
    verified = {xid for xid in candidates & set(saved)
                if tao_extrinsic.confirms_reallocation(
                    saved[xid], [e for e in events if e["kind"] == "sell"
                                 and e.get("extrinsic") == xid])}
    if verified:
        normalized = tao_ledger.net_transfers(events, verified_moves=verified)
    return normalized, checkpoints


def _save_events(db, events):
    db.executemany("INSERT INTO wallet_events VALUES (?,?) ON CONFLICT(id) "
                   "DO UPDATE SET payload=excluded.payload "
                   "WHERE wallet_events.payload <> excluded.payload",
                   [(e["id"], json.dumps(e)) for e in events])


def _discover(args, session, store, stamp, coverage, holders, origins):
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
    from seo.sources.tao_holders import fetch_holders
    held = []
    for n in netuids:
        if not args.holders_only:
            session.pause()
            try:
                found = site.fetch_metagraph(session, n)
            except ChallengeError:
                found = []
            coverage[n] = len(found)
            neurons.extend(found)
        investor, state = fetch_holders(session, n, limit=6, pages=2)
        holders[n] = dict(status=state, sampled_wallets=investor)
        # Don't spend the first holder slot of every subnet on its single
        # largest wallet. Each wallet's account balance is screened later.
        held.append(investor[1:] + investor[:1])
        for a in investor:
            origins[a].add("Taostats holders: SN%d" % n)
        graph = "%d neurons; " % coverage[n] if n in coverage else ""
        print("SN%d: %s%d sampled holders (%s)"
              % (n, graph, len(investor), state), flush=True)
    investors = iter(dict.fromkeys(a for group in zip_longest(*held)
                                   for a in group if a))
    if args.holders_only:
        selected = list(investors)[:args.top or None]
        return dict.fromkeys(selected), "skipped"
    earners = site.earners(neurons)
    ranked = wallets.pick_candidates(earners, args.top or len(earners))
    session.pause()
    recent, status = tao_history.fetch_recent_trades(session)
    recent = [e for e in recent if e["netuid"] in netuids]
    with store.conn:
        _save_events(store.conn, recent)
    by_subnet = defaultdict(list)
    for e in recent:
        if e.get("owner") and e["owner"] not in by_subnet[e["netuid"]]:
            by_subnet[e["netuid"]].append(e["owner"])
            origins[e["owner"]].add("Recent trade: SN%d" % e["netuid"])
    # Sample distinct subnets before another trader from the same subnet.
    active = [a for group in zip_longest(*by_subnet.values())
              for a in group if a]
    active = iter(dict.fromkeys(active))
    # Mix traders and actual alpha holders before another subnet operator.
    candidates = {}
    operators = iter(e["coldkey"] for e in ranked)
    while True:
        trader, holder = next(active, None), next(investors, None)
        operator = next(operators, None)
        if operator:
            origins[operator].add("Metagraph earner")
        group = (trader, holder, next(active, None),
                 next(investors, None), operator)
        if not any(group):
            break
        for a in group:
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
        if (args.max_balance_tao and snap.balance_total is not None
                and snap.balance_total > args.max_balance_tao):
            coverage[tab] = "skipped"
            coverage[tab + "_rows"] = 0
            continue
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
    if args.history_charts > 12:
        raise SystemExit("--history-charts must be between 0 and 12")
    if min(args.min_sales, args.min_timing) < 1:
        raise SystemExit("--min-sales and --min-timing must be at least 1")
    if len(args.extrinsic) > 5:
        raise SystemExit("Choose at most five extrinsics per collection")
    _schema(store.conn)
    for addr in args.watch_address:
        store.follow(addr)
    stamp = wallets._new_stamp(store)
    previous_price = store.conn.execute(
        "SELECT MAX(fetched_at) FROM subnet_stats").fetchone()[0]
    if previous_price and stamp <= previous_price:
        stamp = (wallets._parse_ts(previous_price)
                 + timedelta(seconds=1)).isoformat()
    coverage, graphs, swaps = {}, {}, []
    holders, origins = {}, defaultdict(set)
    followed = {r["address"] for r in store.followed()}
    if args.addresses_only and not (args.address or followed):
        raise SystemExit("--addresses-only needs --address or a watchlist")
    session = cli._session(args)
    # Allow Taostats to load the verification widget used by its history tabs.
    session.ad_block = False
    with session:
        try:
            earners, market_status = _discover(
                args, session, store, stamp, graphs, holders, origins)
        except ValueError as exc:
            print("Collection failed: %s" % exc)
            return 1
        refresh = sorted(followed) if not args.holders_only else []
        roots = list(dict.fromkeys([*refresh, *args.address, *earners]))
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
        events, checkpoints = _read(store, session, explicit=details)
        from seo.sources.tao_prices import refresh_history
        chart_prices, chart_info = refresh_history(
            store, session, events, chart_limit=args.history_charts)
    inferred = tao_ledger.inferred_links(checkpoints, events)
    subnet_stats = {}
    sale_details = []
    transfer_details = []
    positions, stats = tao_ledger.portfolio(events + inferred, checkpoints,
                                            subnet_stats=subnet_stats,
                                            sale_details=sale_details,
                                            transfer_details=transfer_details)
    prices = _prices(store, args.price_history)
    registered = {p["netuid"]: p["registered_at"] for p in chart_prices}
    prices = [dict(p, registered_at=registered.get(p["netuid"]))
              for p in prices if p["netuid"] not in registered
              or wallets._parse_ts(p["fetched_at"])
              >= wallets._parse_ts(registered[p["netuid"]])]
    # Keep run-time marks for current holdings. Archived points are used
    # only to evaluate trades, and never as a replacement current quote.
    existing = {(p["netuid"], p["fetched_at"]) for p in prices}
    prices.extend(p for p in chart_prices
                  if (p["netuid"], p["fetched_at"]) not in existing)
    prices = [p for p in prices if wallets._parse_ts(p["fetched_at"])
              <= wallets._parse_ts(stamp)]
    from seo.sources.tao_prices import timing_evidence
    proof = timing_evidence(events, prices, stamp)
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
    for a in tao_performance.follow_candidates(performance, args.follow_top):
        store.follow(a)
    screened = {w["address"] for w in performance["wallets"] if w["screens"]}
    verified_peers = {
        w["address"] for w in performance["wallets"] if not w["screens"]
        and w["coverage"].get("account") == "complete"
        and (not args.max_balance_tao or w["balance_tao"] is not None)
        and (not args.max_activity_24h or w["activity_24h_complete"])
    }
    watched = {r["address"] for r in store.followed()}
    transfers = [e for e in events + inferred if e["kind"] in (
        "stake_transfer", "tao_transfer")]
    reachable = swaps + [(e["src"], e["dst"]) for e in transfers
                         if e["kind"] == "stake_transfer" or coverage.get(
                             e["dst"], {}).get("holds_subnets")]
    # Record processed links so explicit unfollows survive a later replay.
    for src, dst in reachable:
        if (src not in watched or src in screened or src == dst
                or dst not in verified_peers):
            continue
        with store.conn:
            new = store.conn.execute(
                "INSERT OR IGNORE INTO wallet_follow_links VALUES (?,?)",
                (src, dst)).rowcount
            if new:
                store.follow(dst)
    from seo import tao_monitor
    following = [r["address"] for r in store.followed()]
    checked_watch = [a for a in following if a in coverage]
    monitor = tao_monitor.update(store.conn, stamp, checked_watch,
                                 events, coverage)
    mode = ("watchlist" if args.addresses_only else "holder_supplement"
            if args.holders_only else "discovery")
    report = dict(checked_at=stamp, metagraph_neurons=graphs,
                  mode=mode,
                  monitor=monitor, historical_prices=chart_info,
                  sale_details=sale_details, timing_evidence=proof,
                  transfer_details=transfer_details,
                  holders=holders, discovery_sources={
                      a: sorted(origins[a]) for a in earners},
                  coverage=coverage, candidates=list(earners),
                  recent_trades_status=market_status,
                  deferred_recipients=sorted(deferred),
                  positions=list(positions.values()), traders=traders,
                  performance=performance, extrinsics=details,
                  transfers=transfers, swaps=swaps, events=events,
                  unpriced_sales={a: s["unpriced_sales"] for a, s
                                  in stats.items() if s["unpriced_sales"]},
                  following=following)
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
