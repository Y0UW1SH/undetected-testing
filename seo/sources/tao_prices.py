"""Time-stamped subnet prices from public Taostats pages in the active browser.

Pool sparklines supply about seven days of block-stamped quotes for all
subnets. Selected subnet charts supply hourly opens from their displayed
three-month range. Only candles with observed volume qualify as new marks.
"""
import base64
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import time
from urllib.parse import parse_qs, urlsplit

from seo.sources import taostats
from seo.sources.tao_history import timestamp
from seo.sources.taostats_wallets import BASE, find_query

CHART_RANGE = """
(() => {
  const doc = document.querySelector('iframe[title="Financial Chart"]')
                      ?.contentDocument;
  const selector = '[aria-label="3 months in 1 hour intervals"]';
  const range = doc?.querySelector(selector);
  if (!range) return false;
  range.click(); return true;
})()
"""
MAX_CHARTS = 12


def _number(raw):
    try:
        n = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) else None


def _pools(html):
    data = find_query(html, "dtaoSubnetPools")
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return []
    return [p for p in data["data"] if isinstance(p, dict)
            and type(p.get("netuid")) is int and p["netuid"] >= 0
            and timestamp(p.get("registration_timestamp"))]


def _pool_marks(pools):
    for p in pools:
        born = timestamp(p["registration_timestamp"])
        for point in p.get("seven_day_prices") or ():
            if not isinstance(point, dict):
                continue
            t = timestamp(point.get("timestamp"))
            px = _number(point.get("price"))
            block = point.get("block_number")
            if (t and t >= born and px and px > 0 and type(block) is int
                    and block >= 0):
                yield dict(netuid=p["netuid"], registered_at=born,
                           fetched_at=t, price_tao=px, block_number=block,
                           source="pool")


def chart_marks(netuid, born, query, data):
    """Return TAO-denominated hourly opens for the requested subnet."""
    if (query.get("symbol") != "SUB-%s" % netuid
            or query.get("resolution") != "60" or not isinstance(data, dict)
            or data.get("s") != "ok"):
        return []
    arrays = [data.get(k) for k in ("t", "o", "v")]
    if (not all(isinstance(a, list) for a in arrays)
            or len(set(map(len, arrays))) != 1):
        return []
    marks = []
    now = datetime.now(timezone.utc).timestamp()
    for epoch, opening, volume in zip(*arrays):
        price, volume = _number(opening), _number(volume)
        if (type(epoch) is not int or epoch < 0 or epoch > now
                or epoch % 3600 or price is None or price <= 0
                or volume is None or volume <= 0):
            continue
        t = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
        if t >= born:
            marks.append(dict(netuid=netuid, registered_at=born, fetched_at=t,
                              price_tao=price, block_number=None,
                              source="chart_hourly_open"))
    return marks


def _chart(session, n, born, want_older):
    # Observe only the chart's own responses to navigation and its range
    # control. No direct data requests or credentials.
    import mycdp

    tab, loop = session.sb.page, session.sb.loop
    pending = {}
    quotes = {}
    clicked = False
    finished_range = False

    async def observe(evt, *_):
        url = urlsplit(evt.response.url)
        host = url.hostname or ""
        if (evt.response.status == 200
                and (host == "taostats.io" or host.endswith(".taostats.io"))
                and url.path.endswith("/udf/history")):
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if q.get("symbol") == "SUB-%s" % n and q.get("resolution") == "60":
                pending[evt.request_id] = q

    tab.add_handler(mycdp.network.ResponseReceived, observe)
    try:
        loop.run_until_complete(tab.send(mycdp.network.enable()))
        session.pause()
        session.goto("%s/subnets/%s/chart" % (BASE, n), check_google=False)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            loop.run_until_complete(tab.wait(0.5))
            for rid, q in list(pending.items()):
                try:
                    body, encoded = loop.run_until_complete(tab.send(
                        mycdp.network.get_response_body(rid)))
                except Exception:
                    continue  # ResponseReceived can precede LoadingFinished.
                del pending[rid]
                try:
                    data = json.loads(base64.b64decode(body) if encoded
                                      else body)
                except (TypeError, ValueError):
                    continue
                rows = chart_marks(n, born, q, data)
                for r in rows:
                    quotes[r["fetched_at"]] = r
                # Some subnets have under three months of history. A range
                # request returning bars still counts when that is all it has.
                if clicked and rows and int(q.get("countback", 0)) >= 500:
                    finished_range = True
            if quotes and (not want_older or finished_range):
                break
            if quotes and want_older and not clicked:
                clicked = bool(session.sb.evaluate(CHART_RANGE))
    finally:
        tab.handlers[mycdp.network.ResponseReceived].remove(observe)
    return list(quotes.values())


def _schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS subnet_price_marks("
               "netuid INTEGER, registered_at TEXT, fetched_at TEXT, "
               "price_tao REAL, block_number INTEGER, source TEXT, "
               "PRIMARY KEY(netuid,registered_at,fetched_at,source))")
    db.execute("CREATE TABLE IF NOT EXISTS subnet_price_generations("
               "netuid INTEGER PRIMARY KEY, registered_at TEXT NOT NULL)")


def _save(db, marks):
    db.executemany("INSERT OR IGNORE INTO subnet_price_marks VALUES("
                   ":netuid,:registered_at,:fetched_at,:price_tao,"
                   ":block_number,:source)", marks)


def _stored(db):
    cursor = db.execute("SELECT m.* FROM subnet_price_marks m JOIN "
                        "subnet_price_generations g "
                        "USING(netuid,registered_at)"
                        " ORDER BY fetched_at,"
                        " CASE m.source WHEN 'pool' THEN 0 ELSE 1 END")
    cols = [c[0] for c in cursor.description]
    rows = {}
    for values in cursor:
        r = dict(zip(cols, values))
        rows.setdefault((r["netuid"], r["fetched_at"]), r)
    return list(rows.values())


def _audit(events, prices):
    times, rows = defaultdict(list), defaultdict(list)
    for r in sorted(prices, key=lambda p: p["fetched_at"]):
        n = r["netuid"]
        times[n].append(datetime.fromisoformat(r["fetched_at"]))
        rows[n].append(r)
    total, missing, matches = 0, Counter(), []
    for e in events:
        if e["kind"] not in ("buy", "sell") or not e["alpha"] or not e["tao"]:
            continue
        total += 1
        n = e["netuid"]
        trade_time = timestamp(e["time"])
        if not trade_time:
            missing[n] += 1
            continue
        t = datetime.fromisoformat(trade_time)
        i = bisect_left(times[n], t + timedelta(hours=24))
        if (i >= len(times[n]) or times[n][i] > t + timedelta(hours=30)
                or trade_time < rows[n][i]["registered_at"]):
            missing[n] += 1
            continue
        p = rows[n][i]
        execution = e["tao"] / e["alpha"]
        direction = 1 if e["kind"] == "buy" else -1
        move = direction * 100 * (p["price_tao"] / execution - 1)
        flat = math.isclose(p["price_tao"], execution, rel_tol=1e-12)
        market_move = "flat" if flat else (
            "rise" if p["price_tao"] > execution else "fall")
        url = ("%s/subnets/%d/chart" % (BASE, n)
               if p["source"] == "chart_hourly_open" else taostats.SUBNETS_URL)
        matches.append(dict(id=e.get("id"), address=e["dst"] or e["src"],
                            netuid=n, kind=e["kind"], trade_time=trade_time,
                            extrinsic=e.get("extrinsic"),
                            fill_price_tao=execution, price_tao=p["price_tao"],
                            quote_time=p["fetched_at"],
                            quote_source=p["source"],
                            quote_url=url, quote_block=p["block_number"],
                            market_move=market_move,
                            hours_after=(times[n][i] - t).total_seconds()
                            / 3600,
                            favorable_move_pct=round(move, 5)))
    return total, missing, matches


def timing_evidence(events, prices, stamp):
    """Match observed trades to merged prices available by the cutoff."""
    end = timestamp(stamp.isoformat() if isinstance(stamp, datetime)
                    else stamp)
    if not end:
        raise ValueError("timing evidence requires a UTC cutoff")
    generations = {}
    marks = []
    for p in prices:
        when = timestamp(p.get("fetched_at"))
        px = _number(p.get("price_tao"))
        if not when or when > end or px is None or px <= 0:
            continue
        n = p["netuid"]
        born = timestamp(p.get("registered_at")) or ""
        generations[n] = max(born, generations.get(n, ""))
        marks.append(dict(netuid=n, fetched_at=when, price_tao=px,
                          block_number=p.get("block_number"),
                          registered_at=born,
                          source=p.get("source") or "subnet_snapshot"))
    unique = {}
    for mark in marks:
        born = generations[mark["netuid"]]
        if (mark["registered_at"] and mark["registered_at"] != born
                or mark["fetched_at"] < born):
            continue
        mark["registered_at"] = born
        unique.setdefault((mark["netuid"], mark["fetched_at"]), mark)
    observed = []
    for e in events:
        when = timestamp(e.get("time"))
        if when and when <= end:
            observed.append(e)
    total, missing, matches = _audit(observed, unique.values())
    matches.sort(key=lambda m: m["trade_time"], reverse=True)
    # Keep each wallet's most recent match before showing any second match.
    fair = []
    counts = Counter()
    for index, m in enumerate(matches):
        fair.append((counts[m["address"]], index))
        counts[m["address"]] += 1
    selected = sorted(index for _, index in sorted(fair)[:500])
    details = [matches[index] for index in selected]
    day_ago = datetime.fromisoformat(end) - timedelta(hours=24)
    waiting = sum(e["kind"] in ("buy", "sell")
                  and datetime.fromisoformat(timestamp(e["time"])) > day_ago
                  for e in observed)
    return dict(as_of=end, eligible_trades=total, matched_trades=len(matches),
                awaiting_24h=waiting,
                missing_trades_by_subnet=dict(missing), matches=details,
                market_moves={side: dict(Counter(
                    m["market_move"] for m in matches if m["kind"] == side))
                    for side in ("buy", "sell")},
                match_details_omitted=len(matches) - len(details),
                metric="Directional change from the trade fill to the first "
                       "observed mark 24 to 30 hours later, before fees.")


def refresh_history(store, session, events, *, chart_limit=0):
    """Persist observed prices and return quotes with their timing coverage."""
    if not 0 <= chart_limit <= MAX_CHARTS:
        raise ValueError("chart_limit must be between 0 and %d" % MAX_CHARTS)
    db = store.conn
    _schema(db)
    pool_error = None
    try:
        pools = _pools(session.html())
        if not pools:
            session.pause()
            session.goto(taostats.SUBNETS_URL, check_google=False)
            pools = _pools(session.html())
    except Exception as exc:
        pools, pool_error = [], type(exc).__name__
    generations = {p["netuid"]: timestamp(p["registration_timestamp"])
                   for p in pools}
    pool_marks = list(_pool_marks(pools))
    with db:
        db.executemany("INSERT INTO subnet_price_generations VALUES(?,?) "
                       "ON CONFLICT(netuid) DO UPDATE SET registered_at = "
                       "excluded.registered_at WHERE excluded.registered_at > "
                       "registered_at", generations.items())
        _save(db, pool_marks)
    quotes = _stored(db)
    now = datetime.now(timezone.utc)
    eligible = []
    for e in events:
        when = timestamp(e.get("time"))
        if when and e["kind"] in ("buy", "sell") and (
            now - timedelta(days=94) <= datetime.fromisoformat(when)
                <= now - timedelta(hours=24)):
            eligible.append(e)
    _, missing, _ = _audit(eligible, quotes)
    charts = {}
    candidates = [n for n, _ in missing.most_common()
                  if n in generations and n > 0]
    for n in candidates[:chart_limit]:
        older = any(e["netuid"] == n and datetime.fromisoformat(timestamp(
            e["time"])) < now - timedelta(days=13) for e in eligible)
        try:
            bars = _chart(session, n, generations[n], older)
        except Exception as exc:
            charts[n] = dict(status="unavailable", error=type(exc).__name__)
            continue
        with db:
            _save(db, bars)
        charts[n] = dict(status="observed" if bars else "unavailable",
                         rows=len(bars), url="%s/subnets/%d/chart" % (BASE, n))
    quotes = _stored(db)
    info = timing_evidence(events, quotes, datetime.now(timezone.utc))
    info.update(pool_subnets=len(pools), pool_rows_read=len(pool_marks),
                pool_source_url=taostats.SUBNETS_URL,
                pool_status="observed" if pool_marks else "unavailable",
                pool_error=pool_error, charts=charts,
                stored_quotes=len(quotes),
                oldest=quotes[0]["fetched_at"] if quotes else None,
                newest=quotes[-1]["fetched_at"] if quotes else None)
    return quotes, info
