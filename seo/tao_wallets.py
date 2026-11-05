"""Wallet commands for `tao`: find winning wallets and follow them.

    tao wallets                        # scan the 8 biggest subnets, rank
                                       # the top 20 earners
    tao wallets --netuids 8,64 --by return --follow-top 5
    tao follow 5Gx... --label whale    # add a wallet to the watchlist
    tao following                      # watchlist with gains since follow
    tao track --threshold 5            # refresh followed wallets, warn on
                                       # moves beyond 5% since last check
    tao unfollow 5Gx...

Everything comes from taostats.io pages loaded in the stealth browser
(see seo/sources/taostats_wallets.py). A wallet is "winning" when it
earns a lot of TAO per day from mining or validating on the scanned
subnets; --by picks the ranking:

    earnings  TAO earned per day (default)
    return    TAO per day as a % of the wallet's balance
    growth    balance change over the last 24 hours
    window    balance change over --days, from this tool's own history
              (needs earlier runs)

Balance growth includes transfers in and out, and the subnet scan only
sees wallets that run miners or validators, not pure stakers."""
import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from seo import cli as seo_cli
from seo.models import utcnow
from seo.sources import taostats, taostats_wallets

TABLE = "wallet_snapshots"
ADDRESS_RE = re.compile(r"^(5[1-9A-HJ-NP-Za-km-z]{45,47}|0x[0-9a-fA-F]{64})$")
SORT_KEYS = {"earnings": "rewards_per_day", "return": "return_pct_day",
             "growth": "change_24h_pct", "window": "window_pct"}


# ---- scoring (pure) ---------------------------------------------------------
def _parse_ts(text):
    if not text:
        return None
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _get(row, key):
    return row.get(key) if isinstance(row, dict) else getattr(row, key)


def _pct(old, new):
    if old in (None, 0) or new is None:
        return None
    return 100.0 * (new - old) / old


def _round(v, digits=3):
    return None if v is None else round(v, digits)


def score_window(snapshots, days=7):
    """Balance growth of one wallet across stored snapshots (any order).
    The start point is the newest snapshot at least `days` before the
    newest one, or the oldest when history is shorter. Returns
    (change_pct, span_days) or (None, None)."""
    points = sorted(
        ((t, s) for s in snapshots
         for t in [_parse_ts(_get(s, "snapshot_at") or _get(s, "fetched_at"))]
         if t is not None and _get(s, "balance_total") is not None),
        key=lambda p: p[0])
    if len(points) < 2:
        return None, None
    end_t, end = points[-1]
    older = [p for p in points if p[0] <= end_t - timedelta(days=days)]
    start_t, start = older[-1] if older else points[0]
    if start_t == end_t:
        return None, None
    return (_round(_pct(_get(start, "balance_total"),
                        _get(end, "balance_total"))),
            round((end_t - start_t).total_seconds() / 86400, 2))


def score(snap, earner=None, history=(), days=7):
    """One ranked row from a fresh account snapshot."""
    window_pct, span = score_window(list(history) + [snap], days)
    rewards = snap.rewards_per_day
    return dict(
        address=snap.address, rank=snap.rank,
        name=", ".join(sorted(earner["names"]))[:40] if earner else None,
        subnets=snap.subnets, balance_total=snap.balance_total,
        rewards_per_day=_round(rewards, 4),
        return_pct_day=_round(_pct(snap.balance_total,
                                   (snap.balance_total or 0) + rewards)
                              if rewards is not None else None, 4),
        change_24h_tao=_round(snap.balance_total - snap.balance_total_24h_ago
                              if None not in (snap.balance_total,
                                              snap.balance_total_24h_ago)
                              else None, 4),
        change_24h_pct=_round(_pct(snap.balance_total_24h_ago,
                                   snap.balance_total)),
        window_pct=window_pct, window_days=span,
        url=snap.source_url)


def sort_rows(rows, by="earnings"):
    key = SORT_KEYS[by]
    return sorted(rows, key=lambda r: (r[key] is not None, r[key] or 0),
                  reverse=True)


def pick_candidates(earner_map, top=20, min_rewards=0.0):
    ranked = sorted(earner_map.values(),
                    key=lambda e: e["rewards_per_day"], reverse=True)
    return [e for e in ranked if e["rewards_per_day"] > min_rewards][:top]


# ---- formatting -------------------------------------------------------------
def _fmt(value, digits=2):
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return "{:,.0f}".format(value)
    return ("{:.%df}" % digits).format(value)


def _signed(value, digits=2, suffix=""):
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return "{:+,.0f}{}".format(value, suffix)
    return ("{:+.%df}{}" % digits).format(value, suffix)


def short(address):
    if not address or len(address) <= 14:
        return address or ""
    return "%s...%s" % (address[:6], address[-4:])


def _print_ranking(rows, followed, by, days):
    print("Wallets ranked by %s:" % by)
    print("%3s  %-14s %11s %13s %9s %8s %8s  %-12s %s" % (
        "#", "wallet", "earns τ/day", "balance τ", "ret/day", "24h",
        "%dd" % days, "subnets", "name"))
    for i, r in enumerate(rows, 1):
        print("%3d  %-14s %11s %13s %9s %8s %8s  %-12s %s%s" % (
            i, short(r["address"]), _fmt(r["rewards_per_day"], 3),
            _fmt(r["balance_total"]), _signed(r["return_pct_day"], 3, "%"),
            _signed(r["change_24h_pct"], 1, "%"),
            _signed(r["window_pct"], 1, "%"), (r["subnets"] or "")[:12],
            r["name"] or "", "  *followed" if r["address"] in followed
            else ""))


# ---- storage helpers --------------------------------------------------------
def _new_stamp(store):
    stamp = utcnow()
    row = store.conn.execute(
        "SELECT MAX(fetched_at) AS m FROM %s" % TABLE).fetchone()
    last = row["m"] if row else None
    if last and stamp <= last:
        stamp = (datetime.fromisoformat(last)
                 + timedelta(seconds=1)).isoformat()
    return stamp


def _latest(store, address, before=None):
    sql = "SELECT * FROM %s WHERE address = ?" % TABLE
    params = [address]
    if before:
        sql += " AND fetched_at < ?"
        params.append(before)
    sql += " ORDER BY fetched_at DESC, id DESC LIMIT 1"
    row = store.conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _check_address(address):
    address = (address or "").strip()
    if not ADDRESS_RE.match(address):
        raise SystemExit("Not a Bittensor address: %r (expected an SS58 "
                         "address starting with 5, or 0x + 64 hex)."
                         % address)
    return address


def _netuids(args, store, session):
    """--netuids, else the N biggest subnets by market cap from the
    stored subnet table (scraping it first if there is none)."""
    if args.netuids:
        return [int(x) for x in str(args.netuids).replace(";", ",")
                .split(",") if x.strip()]
    latest, _ = store.latest_runs("subnet_stats", ("netuid",),
                                  source_url=taostats.SUBNETS_URL)
    if not latest:
        print("No subnet table stored yet; fetching %s first."
              % taostats.SUBNETS_URL)
        from seo import tao_cli          # tao_cli imports this module
        rows = taostats.fetch_subnets(session)
        store.save(rows, stamp=tao_cli._new_stamp(store))
        latest = [r.to_dict() for r in rows]
    latest = [r for r in latest if r["netuid"] != 0]   # root has no miners
    latest.sort(key=lambda r: r.get("market_cap_tao") or 0, reverse=True)
    return [r["netuid"] for r in latest[:args.subnets]]


# ---- commands ---------------------------------------------------------------
def cmd_wallets(args, store):
    followed = {w["address"] for w in store.followed()}
    stamp = _new_stamp(store)
    with seo_cli._session(args) as s:
        netuids = _netuids(args, store, s)
        print("Scanning metagraphs of subnets %s ..."
              % ",".join(str(n) for n in netuids))
        neurons = []
        for netuid in netuids:
            found = taostats_wallets.fetch_metagraph(s, netuid)
            print("  SN%d: %d neurons" % (netuid, len(found)))
            neurons.extend(found)
            s.pause()
        if not neurons:
            print("No metagraph data found; taostats may have changed its "
                  "pages. Capture one with `seo capture taostats` and "
                  "check seo/sources/taostats_wallets.py.")
            return 1
        candidates = pick_candidates(taostats_wallets.earners(neurons),
                                     args.top)
        rows, snaps = [], []
        for i, e in enumerate(candidates, 1):
            sys.stdout.write("\r  account pages %d/%d" % (i, len(candidates)))
            sys.stdout.flush()
            snap, positions = taostats_wallets.fetch_account(
                s, e["coldkey"], "scan")
            s.pause()
            if snap is None:
                continue
            snap.rewards_per_day = round(e["rewards_per_day"], 6)
            snap.subnets = ",".join(str(n) for n in sorted(e["subnets"]))
            history = store.rows(TABLE, address=snap.address)
            rows.append(score(snap, e, history, args.days))
            snaps.extend([snap, *positions])
        print("\r  account pages %d/%d done." % (len(candidates),
                                                 len(candidates)))
    store.save(snaps, stamp=stamp)
    rows = [r for r in rows if (r["balance_total"] or 0) >= args.min_balance]
    rows = sort_rows(rows, args.by)
    _print_ranking(rows[:args.show], followed, args.by, args.days)
    if args.by == "window" and all(r["window_pct"] is None for r in rows):
        print("(No earlier runs to compare with yet; the %dd column fills "
              "in from the next run.)" % args.days)
    if args.follow_top:
        added = 0
        for r in rows[:args.follow_top]:
            if store.follow(r["address"], balance=r["balance_total"]):
                added += 1
        print("Followed %d new wallet(s); see `tao following`." % added)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"by": args.by, "days": args.days, "netuids": netuids,
                       "checked_at": stamp, "wallets": rows}, fh, indent=2)
        print("Ranking written to %s" % args.out)
    return 0


def cmd_follow(args, store):
    address = _check_address(args.address)
    latest = _latest(store, address)
    balance = latest["balance_total"] if latest else None
    if store.follow(address, args.label, balance):
        print("Following %s%s%s." % (
            address, " (%s)" % args.label if args.label else "",
            ", balance τ%s" % _fmt(balance) if balance is not None else
            "; `tao track` records its balance"))
    else:
        print("Already following %s%s." % (
            address, "; label updated" if args.label else ""))
    print(taostats_wallets.account_url(address))
    return 0


def cmd_unfollow(args, store):
    address = args.address.strip()
    if store.unfollow(address):
        print("Stopped following %s." % address)
        return 0
    print("Not following %s." % address)
    return 1


def _following_rows(store):
    rows = []
    for w in store.followed():
        latest = _latest(store, w["address"])
        previous = _latest(store, w["address"], before=latest["fetched_at"]) \
            if latest else None
        now = latest["balance_total"] if latest else None
        start = w["balance_at_follow"]
        rows.append(dict(
            address=w["address"], label=w["label"], balance=now,
            since_follow_tao=None if None in (now, start) else now - start,
            since_follow_pct=_pct(start, now),
            change_24h_pct=_pct(latest and latest["balance_total_24h_ago"],
                                now),
            since_last_pct=_pct(previous and previous["balance_total"], now),
            as_of=latest and latest["fetched_at"]))
    return rows


def _print_following(rows):
    if not rows:
        print("Not following any wallets. Try `tao wallets --follow-top 5` "
              "or `tao follow <address>`.")
        return
    print("%-14s %-14s %13s %13s %9s %8s %9s  %s" % (
        "wallet", "label", "balance τ", "since follow", "growth", "24h",
        "last run", "checked"))
    for r in rows:
        print("%-14s %-14s %13s %13s %9s %8s %9s  %s" % (
            short(r["address"]), (r["label"] or "")[:14], _fmt(r["balance"]),
            _signed(r["since_follow_tao"]),
            _signed(r["since_follow_pct"], 2, "%"),
            _signed(r["change_24h_pct"], 1, "%"),
            _signed(r["since_last_pct"], 2, "%"), (r["as_of"] or "-")[:16]))


def cmd_following(args, store):
    _print_following(_following_rows(store))
    return 0


def cmd_track(args, store):
    wallets = store.followed()
    if not wallets:
        _print_following([])
        return 0
    stamp = _new_stamp(store)
    alerts = []
    with seo_cli._session(args) as s:
        for w in wallets:
            previous = _latest(store, w["address"], before=stamp)
            snap, positions = taostats_wallets.fetch_account(
                s, w["address"], "track")
            s.pause()
            if snap is None:
                print("WARNING %s: no account data on taostats"
                      % short(w["address"]))
                continue
            snap.label = w["label"]
            store.save([snap, *positions], stamp=stamp)
            if w["balance_at_follow"] is None:
                store.conn.execute(
                    "UPDATE followed_wallets SET balance_at_follow = ? "
                    "WHERE address = ?", (snap.balance_total, w["address"]))
                store.conn.commit()
            change = _pct(previous and previous["balance_total"],
                          snap.balance_total)
            if change is not None and abs(change) >= args.threshold:
                alerts.append(dict(
                    address=w["address"], label=w["label"],
                    old=previous["balance_total"], new=snap.balance_total,
                    change_pct=round(change, 2),
                    since=previous["fetched_at"]))
    _print_following(_following_rows(store))
    for a in alerts:
        line = "WARNING %s%s balance %s %.1f%% (τ%s -> τ%s)" % (
            short(a["address"]), " (%s)" % a["label"] if a["label"] else "",
            "up" if a["change_pct"] > 0 else "down", abs(a["change_pct"]),
            _fmt(a["old"]), _fmt(a["new"]))
        print(line)
        if os.environ.get("GITHUB_ACTIONS"):
            print("::warning::%s" % line[len("WARNING "):])
    if not alerts:
        print("No balance moves beyond %.1f%% since the last check."
              % args.threshold)
    os.makedirs(os.path.dirname(args.alerts_out) or ".", exist_ok=True)
    with open(args.alerts_out, "w", encoding="utf-8") as fh:
        json.dump({"threshold_pct": args.threshold, "checked_at": stamp,
                   "alerts": alerts}, fh, indent=2)
    return 2 if alerts and args.fail_on_alert else 0


# ---- parser -----------------------------------------------------------------
def delay_range(value):
    try:
        parts = [float(x) for x in value.split("-")]
        if (1 <= len(parts) <= 2 and all(math.isfinite(x) and x >= 0
                                         for x in parts)
                and parts[0] <= parts[-1]):
            return parts[0], parts[-1]
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("use seconds or MIN-MAX, e.g. 2-5")


def add_delay(sp, default=argparse.SUPPRESS):
    sp.add_argument("--delay", dest="pauses", type=delay_range,
                    default=default, help="pause between page loads in "
                    "seconds, e.g. 2 or 2-5 (default: TAO_DELAY)")


def _browser_flags(sp):
    add_delay(sp)
    sp.add_argument("--headless", action="store_true",
                    default=argparse.SUPPRESS,
                    help="same as the global --headless")
    sp.add_argument("--proxy", default=argparse.SUPPRESS,
                    help="same as the global --proxy")


def add_parsers(sub):
    sp = sub.add_parser("wallets", help="find the wallets earning the most "
                        "on the biggest subnets")
    _browser_flags(sp)
    sp.add_argument("--netuids", help="subnets to scan, e.g. 8,64 "
                    "(default: the --subnets biggest by market cap)")
    sp.add_argument("--subnets", type=int, default=8,
                    help="how many of the biggest subnets to scan "
                    "(default 8; one page each)")
    sp.add_argument("--top", type=int, default=20,
                    help="how many top earners to look up (default 20; "
                    "one account page each)")
    sp.add_argument("--by", choices=tuple(SORT_KEYS), default="earnings")
    sp.add_argument("--days", type=int, default=7,
                    help="window for --by window (default 7)")
    sp.add_argument("--min-balance", type=float, default=0.0,
                    help="ignore wallets under this many TAO")
    sp.add_argument("--show", type=int, default=20)
    sp.add_argument("--follow-top", type=int, default=0, metavar="N",
                    help="follow the N best wallets")
    sp.add_argument("--out", default=os.path.join("data", "tao_wallets.json"))
    sp.set_defaults(func=cmd_wallets)

    sp = sub.add_parser("follow", help="add a wallet to the watchlist")
    sp.add_argument("address")
    sp.add_argument("--label")
    sp.set_defaults(func=cmd_follow)

    sp = sub.add_parser("unfollow", help="remove a wallet from the watchlist")
    sp.add_argument("address")
    sp.set_defaults(func=cmd_unfollow)

    sp = sub.add_parser("following", help="show the watchlist")
    sp.set_defaults(func=cmd_following)

    sp = sub.add_parser("track", help="refresh followed wallets and warn on "
                        "big balance moves")
    _browser_flags(sp)
    sp.add_argument("--threshold", type=float, default=5.0,
                    help="percent (default 5)")
    sp.add_argument("--fail-on-alert", action="store_true",
                    help="exit 2 when any alert fires")
    sp.add_argument("--alerts-out",
                    default=os.path.join("data", "tao_wallet_alerts.json"))
    sp.set_defaults(func=cmd_track)
