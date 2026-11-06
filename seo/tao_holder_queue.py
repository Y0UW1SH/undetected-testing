"""Persistent holder research with separate complete and backfill queues."""
import json
from collections import deque
from datetime import datetime, timedelta, timezone


def schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS holder_research_attempts "
                 "(address TEXT PRIMARY KEY, checked_at TEXT NOT NULL, "
                 "status TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS holder_research_observations "
                 "(address TEXT NOT NULL, checked_at TEXT NOT NULL, "
                 "status TEXT NOT NULL, PRIMARY KEY(address,checked_at))")


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name=?", (name,)).fetchone() is not None


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return (parsed.replace(tzinfo=timezone.utc) if not parsed.tzinfo
            else parsed.astimezone(timezone.utc))


def _coverage_status(coverage):
    if not isinstance(coverage, dict) or coverage.get("account") != "complete":
        return "unavailable"
    if (coverage.get("screened") is True
            or coverage.get("link_expansion") == "screened"):
        return "screened"
    tabs = ("transactions", "transfers")
    if all(coverage.get(tab) == "complete" for tab in tabs):
        return "complete"
    # A complete tab, even empty, is useful but does not complete the other.
    if any(coverage.get(tab) == "complete" or (
            coverage.get(tab) == "partial"
            and type(coverage.get(tab + "_rows")) is int
            and coverage[tab + "_rows"] > 0) for tab in tabs):
        return "partial"
    return "unavailable"


def research_state(databases, stamp, excluded=(), addresses=None):
    """Read attributed progress without creating tables or writing.

    Legacy ``inspected`` is scheduling evidence only. Latest usable attributed
    coverage determines partial/complete status; failures preserve earlier
    evidence. Screens override both. Future evidence is ignored throughout.
    """
    now = _time(stamp)
    if now is None:
        raise ValueError("invalid research timestamp")
    allowed = set(addresses) if addresses is not None else None
    usable, attempts, last_checked, attempt_order = {}, {}, {}, {}
    screened = set(excluded)
    attempt_keys = set()

    def observe(address, stamp, status, precedence, attempt=False):
        checked = _time(stamp)
        if (checked is None or checked > now
                or (allowed is not None and address not in allowed)):
            return
        last_checked[address] = max(
            checked, last_checked.get(address, checked))
        if attempt:
            attempt_keys.add((address, checked))
            key = checked, attempt
            if address not in attempts or key > attempt_order[address]:
                attempts[address] = checked, status
                attempt_order[address] = key
        if status == "screened":
            screened.add(address)
        elif status in ("complete", "partial"):
            key = checked, precedence, status == "partial"
            if address not in usable or key > usable[address][0]:
                usable[address] = key, status

    for db in databases:
        for table in ("holder_research_attempts",
                      "holder_research_observations"):
            if _has_table(db, table):
                for address, checked, status in db.execute(
                        "SELECT address,checked_at,status FROM " + table):
                    priority = 2 if table == 'holder_research_attempts' else 1
                    observe(address, checked, status, 0, attempt=priority)
        if _has_table(db, "wallet_performance"):
            rows = db.execute("SELECT address,checked_at,payload "
                              "FROM wallet_performance")
            for address, checked, raw in rows:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if (not isinstance(payload, dict)
                        or payload.get("address") != address):
                    continue
                coverage = payload.get("coverage")
                if not payload.get("screens") and (
                        not isinstance(coverage, dict)
                        or coverage.get("account") not in
                        ("complete", "unavailable")):
                    # Reports also retain passive transfer peers. Refreshing
                    # such a profile is not a new attempt or backfill visit.
                    continue
                status = ("screened" if payload.get("screens") else
                          _coverage_status(coverage))
                observe(address, checked, status, 1)
    complete = {a for a, (_, status) in usable.items() if status == "complete"}
    partial = {a for a, (_, status) in usable.items() if status == "partial"}
    complete -= screened
    partial -= screened
    waiting = {a for a, (checked, status) in attempts.items()
               if status == "unavailable"
               and now < checked + timedelta(hours=24)
               and (a not in usable or checked >= usable[a][0][0])}
    waiting -= complete | screened
    return dict(complete=complete, partial=partial, screened=screened,
                waiting=waiting, attempts=attempts, last_checked=last_checked,
                attempt_count=len(attempt_keys))


def _state(conn, netuids, stamp):
    netuids = list(dict.fromkeys(netuids))
    queues = {n: [] for n in netuids}
    if netuids and _has_table(conn, "holder_registry"):
        placeholders = ",".join("?" for _ in netuids)
        for n, address, rank in conn.execute(
                "SELECT netuid,address,rank FROM holder_registry "
                "WHERE netuid IN (%s)" % placeholders, netuids):
            queues[n].append((address, rank))
    addresses = {a for rows in queues.values() for a, _ in rows}
    return queues, research_state([conn], stamp, addresses=addresses)


def fair_select(queues, order, limit, state):
    """Choose (subnet,address) pairs with a partial-history reservation.

    Input queues have source rank order. Backfills sort oldest-attempt first
    within each subnet. A quarter of positive limits (rounded up) is reserved
    for backfill; unused capacity crosses lanes. Single slots choose backfill
    every fourth recorded attempt when both lanes exist, without a cursor.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError("research limit must be nonnegative")
    excluded = state['complete'] | state['screened'] | state['waiting']
    partial = state['partial']
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    lanes = []
    for backfill in (True, False):
        lane = {}
        for n in order:
            addresses = [a for a in queues[n] if a not in excluded
                         and (a in partial) == backfill]
            if backfill:
                addresses.sort(
                    key=lambda a: state['last_checked'].get(a, epoch))
            lane[n] = deque(addresses)
        lanes.append(lane)
    selected, seen = [], set()

    def take(lane, count, backfill=False):
        ready = [n for n in order if lane[n]]
        if backfill:
            ready.sort(
                key=lambda n: state['last_checked'].get(lane[n][0], epoch))
        active = deque(ready)
        taken = 0
        while active and (count is None or taken < count):
            n = active.popleft()
            while lane[n] and lane[n][0] in seen:
                lane[n].popleft()
            if lane[n]:
                address = lane[n].popleft()
                seen.add(address)
                selected.append((n, address))
                taken += 1
            if lane[n]:
                active.append(n)

    reserve = ((limit + 3) // 4 if limit > 1 else
               int(state['attempt_count'] % 4 == 0))
    take(lanes[0], reserve if limit else None, backfill=True)
    take(lanes[1], limit - len(selected) if limit else None)
    if not limit or len(selected) < limit:
        take(lanes[0], limit - len(selected) if limit else None, backfill=True)
    return selected


def select(conn, netuids, limit, stamp):
    """Select unique pending and partial wallets fairly across subnets.

    Zero selects all eligible wallets. Rank one follows ranks two through six.
    Complete history requires both transaction and transfer tabs. Backfills
    stay eligible until complete, with a reserved share of bounded batches.
    """
    queues, state = _state(conn, netuids, stamp)
    served = (set(state['attempts']) | state['complete'] | state['partial']
              | state['screened'])
    order = sorted(queues,
                   key=lambda n: sum(a in served for a, _ in queues[n]))

    def priority(row):
        address, rank = row
        rank_order = 6.5 if rank == 1 else rank
        return address in state['attempts'], rank_order, address

    ranked = {n: [a for a, _ in sorted(queues[n], key=priority)]
              for n in order}
    return [a for _, a in fair_select(ranked, order, limit, state)]


def summary(conn, netuids, stamp):
    """Partial history is usable and also pending unless a retry is waiting."""
    queues, state = _state(conn, netuids, stamp)
    registered = {a for rows in queues.values() for a, _ in rows}
    complete, partial = state['complete'], state['partial']
    screened, waiting = state['screened'], state['waiting']
    pending = registered - complete - screened - waiting
    return dict(registered=len(registered),
                usable_history=len(complete | partial),
                complete_history=len(complete), partial_history=len(partial),
                screened=len(screened), retry_wait=len(waiting),
                pending=len(pending), backfill_pending=len(pending & partial),
                uninspected_pending=len(pending - partial))


def record(conn, coverage, stamp):
    """Record an attempt and retain dated proof across later failures.

    Observations preserve earlier proof and scheduling progress; legacy
    ``inspected`` never manufactures completeness. Latest attempts cannot
    move backwards in time.
    """
    checked = _time(stamp)
    if checked is None:
        raise ValueError("invalid research timestamp")
    stamp = checked.isoformat()
    schema(conn)
    with conn:
        for address, item in coverage.items():
            status = _coverage_status(item)
            conn.execute(
                "INSERT OR IGNORE INTO holder_research_observations "
                "SELECT address,checked_at,status "
                "FROM holder_research_attempts "
                "WHERE address=?", (address,))
            conn.execute(
                "INSERT INTO holder_research_observations VALUES (?,?,?) "
                "ON CONFLICT(address,checked_at) DO UPDATE SET "
                "status=excluded.status WHERE "
                "holder_research_observations.status!='screened' AND "
                "(excluded.status!='unavailable' "
                "OR holder_research_observations.status NOT IN "
                "('complete','partial','screened'))", (address, stamp, status))
            conn.execute(
                "INSERT INTO holder_research_attempts VALUES (?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET "
                "checked_at=excluded.checked_at,status=excluded.status "
                "WHERE julianday(excluded.checked_at)>= "
                "julianday(holder_research_attempts.checked_at)",
                (address, stamp, status))
