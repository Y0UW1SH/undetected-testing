"""Persistent research progress for discovered subnet holder candidates."""
import json
from collections import deque
from datetime import datetime, timedelta, timezone


def schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS holder_research_attempts "
                 "(address TEXT PRIMARY KEY, checked_at TEXT NOT NULL, "
                 "status TEXT NOT NULL)")


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name=?", (name,)).fetchone() is not None


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if not parsed.tzinfo else parsed


def _coverage_status(coverage):
    if not isinstance(coverage, dict) or coverage.get("account") != "complete":
        return "unavailable"
    transactions = coverage.get("transactions")
    rows = coverage.get("transactions_rows")
    # Partial can mean every returned row was malformed or the page was stale.
    # Only a verified complete history can establish that zero rows is empty.
    if transactions == "complete" or (
            transactions == "partial" and type(rows) is int and rows > 0):
        return "inspected"
    if (coverage.get("screened") is True
            or coverage.get("link_expansion") == "screened"):
        return "screened"
    return "unavailable"


def _state(conn, netuids, stamp):
    now = _time(stamp)
    if now is None:
        raise ValueError("invalid research timestamp")
    schema(conn)
    netuids = list(dict.fromkeys(netuids))
    queues = {n: [] for n in netuids}
    usable, screened, waiting = set(), set(), set()
    attempts = {}
    if not netuids or not _has_table(conn, "holder_registry"):
        return queues, attempts, usable, screened, waiting
    placeholders = ",".join("?" for _ in netuids)
    registry = "SELECT address FROM holder_registry WHERE netuid IN (%s)"
    registry %= placeholders
    for n, address, rank in conn.execute(
            "SELECT netuid,address,rank FROM holder_registry "
            "WHERE netuid IN (%s)" % placeholders, netuids):
        queues[n].append((address, rank))
    for address, checked_at, status in conn.execute(
            "SELECT address,checked_at,status FROM holder_research_attempts "
            "WHERE address IN (%s)" % registry, netuids):
        attempts[address] = (checked_at, status)
        if status == "inspected":
            usable.add(address)
        elif status == "screened":
            screened.add(address)
        elif status == "unavailable":
            checked = _time(checked_at)
            if checked and now < checked + timedelta(hours=24):
                waiting.add(address)
    # A performance row can exist merely because this wallet was somebody
    # else's transfer peer. Only its own attributed coverage proves inspection.
    if _has_table(conn, "wallet_performance"):
        for address, raw in conn.execute(
                "SELECT address,payload FROM wallet_performance "
                "WHERE address IN (%s)" % registry, netuids):
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if (not isinstance(payload, dict)
                    or payload.get("address") != address):
                continue
            status = _coverage_status(payload.get("coverage"))
            if status == "inspected":
                usable.add(address)
            elif status == "screened":
                screened.add(address)
    screened -= usable
    waiting -= usable | screened
    return queues, attempts, usable, screened, waiting


def select(conn, netuids, limit, stamp):
    """Return unique pending wallets fairly across the requested subnets.

    Zero selects every pending wallet. Rank one follows ranks two through six
    instead of disappearing from the queue. Recorded progress determines which
    subnet starts, so even one-wallet batches can advance other subnets.
    """
    if limit < 0:
        raise ValueError("research limit must be nonnegative")
    queues, attempts, usable, screened, waiting = _state(conn, netuids, stamp)
    excluded = usable | screened | waiting
    served = set(attempts) | usable | screened
    order = sorted(queues,
                   key=lambda n: sum(a in served for a, _ in queues[n]))

    def priority(row):
        address, rank = row
        # Retries follow untouched candidates; failures must not pin a subnet
        # to the same inaccessible account whenever the retry delay elapses.
        rank_order = 6.5 if rank == 1 else rank
        return address in attempts, rank_order, address

    pending = {
        n: deque(a for a, _ in sorted(queues[n], key=priority)
                 if a not in excluded) for n in order
    }
    selected, seen = [], set()
    active = deque(n for n in order if pending[n])
    while active and (not limit or len(selected) < limit):
        n = active.popleft()
        while pending[n] and pending[n][0] in seen:
            pending[n].popleft()
        if pending[n]:
            address = pending[n].popleft()
            seen.add(address)
            selected.append(address)
        if pending[n]:
            active.append(n)
    return selected


def summary(conn, netuids, stamp):
    """Count distinct candidates by evidence and pending research state."""
    queues, _, usable, screened, waiting = _state(conn, netuids, stamp)
    registered = {a for rows in queues.values() for a, _ in rows}
    return dict(registered=len(registered), usable_history=len(usable),
                screened=len(screened), retry_wait=len(waiting),
                pending=len(registered - usable - screened - waiting))


def record(conn, coverage, stamp):
    """Save attempts, retaining usable history across subsequent failures.

    ``coverage`` maps inspected addresses to the collector's account/history
    status dictionaries. Complete history counts even with zero trades;
    partial history needs at least one parsed transaction row. A ``screened``
    account was excluded by a configured screen and does not claim history.
    """
    if _time(stamp) is None:
        raise ValueError("invalid research timestamp")
    schema(conn)
    with conn:
        for address, item in coverage.items():
            status = _coverage_status(item)
            conn.execute(
                "INSERT INTO holder_research_attempts VALUES (?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET "
                "checked_at=excluded.checked_at,status=excluded.status "
                "WHERE holder_research_attempts.status NOT IN "
                "('inspected','screened') OR excluded.status='inspected'",
                (address, stamp, status))
