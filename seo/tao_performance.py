"""Wallet discovery from observed Taostats trades, inventory and quotes."""
from collections import Counter, defaultdict
from datetime import timedelta, timezone
from itertools import zip_longest
import json
import math

from seo.tao_ledger import timing_observations
from seo.tao_wallets import _parse_ts


_EMPTY_SALES = dict(wins=0, priced_sales=0, unpriced_sales=0, realized_tao=0.)
BOARDS = ("profit", "open", "profit_partial", "open_partial", "buy", "sell")


def _timing(events, prices, threshold, end):
    mature = [e for e in events if e["kind"] in ("buy", "sell")
              and _parse_ts(e["time"]) <= end - timedelta(hours=24)]
    totals = Counter((e["dst"] if e["kind"] == "buy" else e["src"], e["kind"])
                     for e in mature)
    matched = defaultdict(list)
    for e, quote, when in timing_observations(
            mature, prices, with_timestamp=True):
        buy = e["kind"] == "buy"
        key = e["dst"] if buy else e["src"], e["kind"]
        fill = e["tao"] / e["alpha"]
        move = (100 if buy else -100) * (quote / fill - 1)
        hit = move > 0 and (move >= threshold or math.isclose(move, threshold))
        matched[key].append(dict(
            trade_at=e["time"], fill_tao=fill, quote_at=when,
            quote_tao=quote, netuid=e["netuid"], extrinsic=e.get("extrinsic"),
            favorable=hit, directional_move_pct=move))
    result = {}
    for key, total in totals.items():
        rows = sorted(matched[key], key=lambda r: r["trade_at"], reverse=True)
        dates = set()
        win_dates = set()
        for row in rows:
            date = _parse_ts(row["trade_at"]).astimezone(timezone.utc).date()
            dates.add(date)
            if row["favorable"]:
                win_dates.add(date)
        n = len(rows)
        hits = sum(r["favorable"] for r in rows)
        result[key] = dict(mature_trades=total, evaluated=n, favorable=hits,
                           quote_coverage=n / total, examples=rows[:3],
                           trade_days=len(dates),
                           favorable_days=len(win_dates),
                           rate=hits / n if n else None,
                           mean_directional_move_pct=(sum(
                               r["directional_move_pct"] for r in rows) / n
                               if n else None))
    return result


def _marked_position(address, netuid, pos, sales, quote, stamp, inventory_ok):
    if not pos:
        pos = dict(address=address, netuid=netuid, alpha=0., known_alpha=0.,
                   known_cost_tao=0., probable_alpha=0., probable_cost_tao=0.,
                   unknown_alpha=0., buy_price_tao=None)
    qty = pos["alpha"]
    known, estimated = pos["known_alpha"], pos["probable_alpha"]
    growth = pos.get("growth_alpha", 0.)
    known_growth = pos.get("known_growth_alpha", 0.)
    estimated_growth = pos.get("probable_growth_alpha", 0.)
    price = quote.get("price_tao")
    fresh = price is not None and _parse_ts(quote["fetched_at"]) == stamp
    can_mark = fresh and inventory_ok
    value = price * qty if can_mark else None
    known_pnl = (price * known - pos["known_cost_tao"]
                 if can_mark and known else None)
    estimated_pnl = (price * estimated - pos["probable_cost_tao"]
                     if can_mark and estimated else None)
    growth_value = price * growth if can_mark else None
    known_growth_value = price * known_growth if can_mark and known else None
    estimated_growth_value = (price * estimated_growth
                              if can_mark and estimated else None)
    known_trading = (known_pnl - known_growth_value
                     if known_pnl is not None else None)
    estimated_trading = (estimated_pnl - estimated_growth_value
                         if estimated_pnl is not None else None)
    all_known = math.isclose(known, qty, abs_tol=1e-13, rel_tol=2e-15)
    purchased = pos.get("known_purchased_alpha", known - known_growth)
    known_entry = pos["known_cost_tao"] / purchased if purchased > 0 else None
    known_effective = pos["known_cost_tao"] / known if known else None
    total_pnl = known_pnl if all_known else None
    trading_pnl = known_trading if all_known else None
    if not qty and inventory_ok:
        total_pnl = trading_pnl = value = growth_value = 0.
    return dict(pos, **sales, quote_at=quote.get("fetched_at"),
                price_tao=price, current_quote=fresh,
                current_inventory=inventory_ok,
                known_buy_price_tao=known_entry, marked_value_tao=value,
                known_effective_cost_per_alpha_tao=known_effective,
                growth_value_tao=growth_value,
                known_growth_value_tao=known_growth_value,
                probable_growth_value_tao=estimated_growth_value,
                known_unrealized_tao=known_pnl,
                probable_unrealized_tao=estimated_pnl,
                known_trading_unrealized_tao=known_trading,
                probable_trading_unrealized_tao=estimated_trading,
                gross_trading_unrealized_tao=trading_pnl,
                gross_unrealized_tao=total_pnl)


def _observed_activity(events, end):
    """One count per normalized movement touching this wallet within 24h."""
    start = end - timedelta(hours=24)
    counts = defaultdict(lambda: dict(trades=0, transfers=0, reallocations=0))
    links = {}
    for e in events:
        trade = e["kind"] in ("buy", "sell")
        recent = start <= _parse_ts(e["time"]) <= end
        move = e["evidence"] in ("native_move", "matching_trade_legs")
        if recent and (e["evidence"] == "transaction" or move):
            group = "trades" if trade else "transfers"
            if move:
                group = "reallocations"
            for addr in {e["src"], e["dst"]} - {None}:
                counts[addr][group] += 1
        if trade or not e["src"] or not e["dst"] or e["src"] == e["dst"]:
            continue
        key = e["src"], e["dst"], e["kind"], e["netuid"], e["evidence"]
        link = links.setdefault(key, dict(
            src=e["src"], dst=e["dst"], kind=e["kind"], netuid=e["netuid"],
            evidence=e["evidence"], movements=0, first_seen=e["time"],
            last_seen=e["time"], extrinsic=None))
        link["movements"] += 1
        link["first_seen"] = min(link["first_seen"], e["time"])
        if ((e["time"], str(e.get("extrinsic") or ""))
                >= (link["last_seen"], str(link["extrinsic"] or ""))):
            link["last_seen"] = e["time"]
            link["extrinsic"] = e.get("extrinsic")
    by_address = defaultdict(list)
    ordered = sorted(links.values(), key=lambda row: (
        row["last_seen"], row["src"], row["dst"]), reverse=True)
    for link in ordered:
        by_address[link["src"]].append(link)
        by_address[link["dst"]].append(link)
    return counts, by_address


def _last_additions(events):
    """Latest observed entry into an asset, not the age of every unit."""
    last = {}
    for e in events:
        if e["kind"] not in ("buy", "stake_transfer") or not e.get("dst"):
            continue
        if (e["src"] == e["dst"] or e.get("transfer_side") in
                ("out", "unknown") or e.get("evidence") != "transaction"):
            continue
        key = e["dst"], e["netuid"]
        last[key] = max(last.get(key, e["time"]), e["time"])
    return last


def _open_evidence(w, additions, stamp):
    positions = [p for p in w["positions"] if p["alpha"] > 0]
    known = [p for p in positions if p["known_alpha"] > 0]
    marked = w["marked_value_tao"]
    cost = sum(p["known_cost_tao"] for p in known) if known else None
    confirmed = growth_value = trading_pnl = None
    share = None
    if w["current_inventory"] and marked and known:
        # marked_value_tao requires a fresh quote on every held subnet.
        confirmed = sum(p["known_unrealized_tao"] for p in known)
        growth_value = sum(p["known_growth_value_tao"] for p in known)
        trading_pnl = sum(p["known_trading_unrealized_tao"] for p in known)
        share = min(1., sum(p["known_alpha"] * p["price_tao"]
                            for p in known) / marked)
    known_return = (100 * confirmed / cost
                    if confirmed is not None and cost else None)
    last = max((additions[w["address"], p["netuid"]] for p in known
                if (w["address"], p["netuid"]) in additions), default=None)
    elapsed = (stamp - _parse_ts(last)).total_seconds() if last else None
    return dict(positions=len(positions), confirmed_positions=len(known),
                known_cost_tao=cost, known_pnl_tao=confirmed,
                known_growth_value_tao=growth_value,
                known_trading_pnl_tao=trading_pnl,
                known_value_fraction=share, known_return_pct=known_return,
                return_pct=(known_return if w["gross_unrealized_tao"]
                            is not None else None),
                last_observed_addition_at=last,
                days_since_addition=(round(elapsed / 86400, 2)
                                     if elapsed is not None and elapsed >= 0
                                     else None))


def _day_covered(coverage, tab, start):
    status = coverage.get(tab)
    since = _parse_ts(coverage.get(tab + "_since"))
    return (status == "complete"
            or (status == "partial" and since is not None and since <= start))


def _eligibility(w, stamp, settings):
    coverage = w["coverage"]
    gaps, screens = [], []
    if coverage.get("account") != "complete":
        gaps.append("account_unavailable")
    elif not w["snapshot_recent"]:
        gaps.append("snapshot_stale" if w["account_as_of"]
                    else "snapshot_date_unknown")
    if coverage.get("transactions") != "complete":
        status = coverage.get("transactions", "unavailable")
        gaps.append("transactions_" + status)
    day_start = stamp - timedelta(hours=24)
    day_covered = all(_day_covered(coverage, tab, day_start)
                      for tab in ("transactions", "transfers"))
    w["activity_24h_complete"] = day_covered
    if settings["max_balance_tao"]:
        current_balance = _parse_ts(w["balance_fetched_at"]) == stamp
        if w["balance_tao"] is None or not current_balance:
            gaps.append("current_balance_unknown")
        elif w["balance_tao"] > settings["max_balance_tao"]:
            screens.append("balance_above_limit")
    count = sum(w["activity_24h"].values())
    if settings["max_activity_24h"]:
        if count > settings["max_activity_24h"]:
            screens.append("activity_above_limit")
        elif not day_covered:
            gaps.append("activity_24h_incomplete")
    w["screens"], w["evidence_gaps"] = screens, gaps
    w["rankable_profit"] = bool(
        not (screens or gaps) and w["priced_sales"] >= settings["min_sales"]
        and not w["unpriced_sales"] and w["win_rate"] > .5
        and w["realized_tao"] > 0)
    known_proceeds = w.get("known_sale_proceeds_tao") or 0.
    proceeds = known_proceeds + (w.get("unresolved_sale_proceeds_tao") or 0.)
    w["known_sale_value_fraction"] = (known_proceeds / proceeds
                                      if proceeds else None)
    known_pnl = w.get("known_trading_realized_tao",
                      w.get("known_realized_tao"))
    known_cost = w.get("known_sale_cost_tao")
    w["known_sale_return_pct"] = (100 * known_pnl / known_cost
                                  if known_pnl is not None and known_cost
                                  else None)
    w["rankable_profit_partial"] = bool(
        not screens and not (set(gaps) - {"transactions_partial"})
        and w["unpriced_sales"] and w["known_sale_return_pct"] is not None
        and w["known_sale_return_pct"] > 0
        and w["priced_sales"] + (w.get("partial_sales") or 0)
        >= settings["min_sales"])
    opened = w["open"]
    known_return = opened["known_return_pct"]
    marked_gain = known_return is not None and known_return > 0
    w["rankable_open"] = bool(
        not (screens or gaps) and marked_gain
        and opened["return_pct"] is not None
        and w["gross_total_pnl_tao"] is not None
        and w["gross_total_pnl_tao"] > 0)
    w["rankable_open_partial"] = bool(
        not screens and not (set(gaps) - {"transactions_partial"})
        and marked_gain and w["gross_total_pnl_tao"] is None
        and opened["known_value_fraction"] is not None)
    timely = not screens and not (set(gaps) - {"transactions_partial"})
    for side in ("buy", "sell"):
        s = w["timing"][side]
        s["rankable"] = bool(
            timely and s["evaluated"] >= settings["min_timing"]
            and s["favorable_days"] >= 2 and s["quote_coverage"] >= .5
            and s["rate"] > .5)


def _rank(wallets):
    rankings = {}
    for name in BOARDS:
        if name == "profit":
            selected = [w for w in wallets if w["rankable_profit"]]
            selected.sort(key=lambda w: (
                -w["realized_tao"], -w["priced_sales"], w["address"]))
        elif name == "open":
            selected = [w for w in wallets if w["rankable_open"]]
            selected.sort(key=lambda w: (-w["open"]["return_pct"],
                                         -w["gross_unrealized_tao"],
                                         w["address"]))
        elif name == "profit_partial":
            selected = [w for w in wallets if w["rankable_profit_partial"]]
            selected.sort(key=lambda w: (-w["known_sale_value_fraction"],
                                         -w["known_sale_return_pct"],
                                         w["address"]))
        elif name == "open_partial":
            selected = [w for w in wallets if w["rankable_open_partial"]]
            selected.sort(key=lambda w: (-w["open"]["known_value_fraction"],
                                         -w["open"]["known_return_pct"],
                                         w["address"]))
        else:
            selected = [w for w in wallets if w["timing"][name]["rankable"]]
            selected.sort(key=lambda w: (-w["timing"][name]["rate"],
                                         -w["timing"][name]["evaluated"],
                                         w["address"]))
        rankings[name] = [w["address"] for w in selected]
        for rank, w in enumerate(selected, 1):
            w["ranks"][name] = rank
    return rankings


def follow_candidates(performance, count):
    """Interleave screened realized traders and fully costed open holders."""
    ranks = performance["rankings"]
    wallets = {w["address"]: w for w in performance["wallets"]}
    result = []
    for pair in zip_longest(ranks["profit"], ranks.get("open", [])):
        for address in pair:
            if len(result) >= count:
                return result
            if address and address not in result:
                combined = wallets[address]["gross_total_pnl_tao"]
                if combined is None or combined > 0:
                    result.append(address)
    return result


def _current_balances(store, stamp):
    rows = store.conn.execute(
        "SELECT address, snapshot_at, fetched_at, balance_total, "
        "positions_complete FROM (SELECT address, snapshot_at, fetched_at,"
        " balance_total, positions_complete, ROW_NUMBER() OVER ("
        " PARTITION BY address ORDER BY fetched_at DESC,id DESC) AS rn"
        " FROM wallet_snapshots WHERE fetched_at<=?) WHERE rn=1", (stamp,))
    return {r["address"]: dict(r) for r in rows}


def _whole_wallet(positions, field, inventory_ok):
    if not inventory_ok or any(p[field] is None for p in positions):
        return None
    return sum(p[field] for p in positions)


def economic_realized(row):
    """Include growth in sale P/L; missing acquisition cost stays unknown."""
    if row["unpriced_sales"]:
        return None
    for field in ("economic_realized_tao", "known_realized_tao"):
        if row.get(field) is not None:
            return row[field]
    return row["realized_tao"]  # Older runs and wallets with no sales.


def record_run(store, stamp, positions, stats, subnet_stats, events, prices,
               coverage, *, min_sales=2, min_timing=3, move_pct=5.0,
               max_balance_tao=100000.0, max_activity_24h=100):
    """Snapshot the evidence and ranks available to this collection."""
    settings = dict(min_sales=min_sales, min_timing=min_timing,
                    move_pct=move_pct, max_balance_tao=max_balance_tao,
                    max_activity_24h=max_activity_24h)
    if (min(min_sales, min_timing) < 1 or max_activity_24h < 0
            or not all(math.isfinite(x) and x >= 0
                       for x in (move_pct, max_balance_tao))):
        raise ValueError("invalid performance sample size or screen limit")
    end = _parse_ts(stamp)
    prices = [p for p in prices if p.get("price_tao")
              and math.isfinite(p["price_tao"]) and p["price_tao"] > 0
              and _parse_ts(p["fetched_at"]) <= end]
    quotes = {}
    for p in sorted(prices, key=lambda p: _parse_ts(p["fetched_at"])):
        quotes[p["netuid"]] = p
    balances = _current_balances(store, stamp)
    # The network timestamp may be minutes later than the collection start.
    recent = {a for a, b in balances.items() if _parse_ts(b["snapshot_at"])
              and _parse_ts(b["snapshot_at"]) >= end - timedelta(hours=24)}
    inventory = {a for a, b in balances.items()
                 if a in recent and b["positions_complete"]
                 and _parse_ts(b["fetched_at"]) == end
                 and coverage.get(a, {}).get("account") == "complete"}
    marked = defaultdict(list)
    for address, n in sorted(set(positions) | set(subnet_stats)):
        key = address, n
        marked[address].append(_marked_position(
            address, n, positions.get(key),
            subnet_stats.get(key, _EMPTY_SALES), quotes.get(n, {}),
            end, address in inventory))
    timing = _timing(events, prices, move_pct, end)
    activity, ties = _observed_activity(events, end)
    additions = _last_additions(events)
    addresses = sorted(set(coverage) | set(marked) | set(stats) | set(ties)
                       | {address for address, _ in timing})
    wallets = []
    for address in addresses:
        totals = stats.get(address, _EMPTY_SALES)
        bal = balances.get(address, {})
        sides = {side: timing.get((address, side), dict(
            favorable=0, evaluated=0, trade_days=0, rate=None, mature_trades=0,
            favorable_days=0, quote_coverage=None, examples=[],
            mean_directional_move_pct=None)) for side in ("buy", "sell")}
        rows = marked[address]
        sales = totals["priced_sales"]
        fresh = address in inventory
        unrealized = _whole_wallet(rows, "gross_unrealized_tao", fresh)
        economic = economic_realized(totals)
        combined = None
        account_coverage = coverage.get(address, {})
        if (unrealized is not None and not totals["unpriced_sales"]
                and account_coverage.get("transactions") == "complete"):
            combined = economic + unrealized
        w = dict(address=address, **totals, positions=rows,
                 win_rate=totals["wins"] / sales if sales else None,
                 balance_tao=bal.get("balance_total"),
                 balance_fetched_at=bal.get("fetched_at"),
                 snapshot_recent=address in recent,
                 account_as_of=bal.get("snapshot_at"), current_inventory=fresh,
                 marked_value_tao=_whole_wallet(
                     rows, "marked_value_tao", fresh),
                 gross_unrealized_tao=unrealized, gross_total_pnl_tao=combined,
                 gross_trading_unrealized_tao=_whole_wallet(
                     rows, "gross_trading_unrealized_tao", fresh),
                 growth_value_tao=_whole_wallet(
                     rows, "growth_value_tao", fresh),
                 coverage=account_coverage, timing=sides,
                 transfer_links=ties[address], activity_24h=activity[address],
                 ranks=dict.fromkeys(BOARDS))
        w["economic_realized_tao"] = economic
        w["open"] = _open_evidence(w, additions, end)
        _eligibility(w, end, settings)
        wallets.append(w)
    rankings = _rank(wallets)
    _save_run(store.conn, stamp, settings, wallets)
    return dict(checked_at=stamp, settings=settings, wallets=wallets,
                rankings=rankings)


def _save_run(db, stamp, settings, wallets):
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS wallet_performance_runs "
                   "(checked_at TEXT PRIMARY KEY, settings TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS wallet_performance "
                   "(address TEXT, checked_at TEXT, payload TEXT NOT NULL, "
                   "PRIMARY KEY(address, checked_at))")
        db.execute("CREATE INDEX IF NOT EXISTS wallet_performance_by_run "
                   "ON wallet_performance(checked_at)")
        db.execute("INSERT OR REPLACE INTO wallet_performance_runs "
                   "VALUES (?,?)", (stamp, json.dumps(settings)))
        db.execute("DELETE FROM wallet_performance WHERE checked_at=?",
                   (stamp,))
        db.executemany("INSERT INTO wallet_performance VALUES (?,?,?)",
                       [(w["address"], stamp, json.dumps(w)) for w in wallets])


def _has_history(db):
    row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                     "AND name='wallet_performance_runs'").fetchone()
    return row is not None


def load_run(db, checked_at=None):
    """Load one saved run, default latest; return None for older databases."""
    if not _has_history(db):
        return None
    sql = "SELECT checked_at, settings FROM wallet_performance_runs"
    if checked_at is not None:
        row = db.execute(sql + " WHERE checked_at=?", (checked_at,)).fetchone()
    else:
        row = db.execute(sql + " ORDER BY checked_at DESC LIMIT 1").fetchone()
    if not row:
        return None
    wallets = [json.loads(r[0]) for r in db.execute(
        "SELECT payload FROM wallet_performance WHERE checked_at=? "
        "ORDER BY address", (row[0],))]
    ranks = {}
    for name in BOARDS:
        selected = sorted((w for w in wallets if w["ranks"].get(name)),
                          key=lambda w: w["ranks"][name])
        ranks[name] = [w["address"] for w in selected]
    return dict(checked_at=row[0], settings=json.loads(row[1]),
                rankings=ranks, wallets=wallets)


def history(db, address, limit=30):
    """Return an address's latest observed rankings first."""
    if not _has_history(db) or limit < 1:
        return []
    rows = db.execute("SELECT checked_at,payload FROM wallet_performance "
                      "WHERE address=? ORDER BY checked_at DESC LIMIT ?",
                      (address, limit))
    return [dict(json.loads(payload), checked_at=stamp)
            for stamp, payload in rows]
