"""Resumable discovery of coldkeys from the live Taostats holder ranking.

This is a moving-page traversal, never a fixed-block census. Registry balances
are sums of distinct positions observed together on a page, not full holdings
or financial evidence; a later observation replaces that page-level amount.
"""
import re
import time

from seo.sources import tao_holders as holders
from seo.sources.taostats_wallets import BASE, _ss58
from seo.tao_wallets import ADDRESS_RE


def schema(conn):
    """Add holder discovery state to the existing wallet database."""
    conn.execute("""CREATE TABLE IF NOT EXISTS holder_registry (
        netuid INTEGER NOT NULL, address TEXT NOT NULL, rank INTEGER NOT NULL,
        balance_units TEXT NOT NULL, first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL, PRIMARY KEY (netuid, address))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS holder_page_state (
        netuid INTEGER PRIMARY KEY, next_page INTEGER NOT NULL,
        total_pages INTEGER NOT NULL, total_items INTEGER NOT NULL,
        page_size INTEGER NOT NULL, checked_at TEXT NOT NULL)""")


class _PageError(ValueError):
    """Only fixed diagnostic codes, never response bodies or browser errors."""


def _number(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise _PageError("invalid_pagination")


def _validate(data, netuid, page):
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise _PageError("page_unavailable")
    pagination = data.get("pagination")
    if not isinstance(pagination, dict):
        raise _PageError("invalid_pagination")
    current, total, items, size = [_number(pagination.get(key)) for key in (
        "current_page", "total_pages", "total_items", "per_page")]
    if current != page or size < 1:
        raise _PageError("invalid_pagination")
    if items == 0:
        if total not in (0, 1) or page != 1:
            raise _PageError("invalid_pagination")
    elif total != (items + size - 1) // size or not 1 <= page <= total:
        raise _PageError("invalid_pagination")
    expected = min(size, max(0, items - (page - 1) * size))
    rows = data["data"]
    if len(rows) != expected:
        raise _PageError("incomplete_page")
    _, valid = holders._ranked(rows, netuid)
    if not valid:
        raise _PageError("invalid_holder_rows")
    # Multiple hotkeys can belong to one coldkey. Repeated rows for the same
    # position must not increase its balance or create another queue entry.
    positions = {}
    for row in rows:
        balance, rank = int(row["balance"]), int(row["subnet_rank"])
        if balance <= 0:
            continue
        address, hotkey = _ss58(row["coldkey"]), _ss58(row.get("hotkey"))
        if hotkey is not None and (not isinstance(hotkey, str)
                                   or not ADDRESS_RE.fullmatch(hotkey)):
            raise _PageError("invalid_holder_rows")
        key = (address, hotkey or "")
        previous = positions.get(key)
        if previous and previous[1] != balance:
            raise _PageError("conflicting_holder_rows")
        positions[key] = (min(rank, previous[0]) if previous else rank,
                          balance)
    coldkeys = {}
    for (address, _), (rank, balance) in positions.items():
        previous = coldkeys.get(address)
        coldkeys[address] = (
            min(rank, previous[0]) if previous else rank,
            balance + (previous[1] if previous else 0))
    return (total, items, size), coldkeys


def _state(conn, netuid):
    return conn.execute("SELECT next_page,total_pages,total_items,page_size "
                        "FROM holder_page_state WHERE netuid=?", (netuid,)
                        ).fetchone()


def _save_page(conn, netuid, page, paging, coldkeys, stamp):
    total, items, size = paging
    # Isolate rollback from unrelated pending writes on the shared wallet DB.
    # With no outer transaction, releasing this savepoint commits the page.
    conn.execute("SAVEPOINT holder_registry_page")
    try:
        for address, (rank, balance) in coldkeys.items():
            conn.execute("""INSERT INTO holder_registry
                (netuid,address,rank,balance_units,first_seen,last_seen)
                VALUES (?,?,?,?,?,?) ON CONFLICT(netuid,address) DO UPDATE SET
                rank=MIN(holder_registry.rank,excluded.rank),
                balance_units=excluded.balance_units,
                last_seen=excluded.last_seen
                """, (netuid, address, rank, str(balance), stamp, stamp))
        conn.execute("""INSERT INTO holder_page_state
            (netuid,next_page,total_pages,total_items,page_size,checked_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(netuid) DO UPDATE SET
            next_page=excluded.next_page,total_pages=excluded.total_pages,
            total_items=excluded.total_items,page_size=excluded.page_size,
            checked_at=excluded.checked_at""",
                     (netuid, page + 1 if total else 1, total, items,
                      size, stamp))
    except Exception:
        conn.execute("ROLLBACK TO holder_registry_page")
        conn.execute("RELEASE holder_registry_page")
        raise
    else:
        conn.execute("RELEASE holder_registry_page")


def _select_page(session, page):
    session.pause()
    jump = session.sb.evaluate(holders.SELECT_PAGE.replace("PAGE", str(page)))
    if not jump:
        raise _PageError("page_control_unavailable")
    if jump == "jump":
        for _ in range(10):
            if session.sb.evaluate(
                    holders.FILL_JUMP.replace("PAGE", str(page))):
                break
            time.sleep(.1)
        else:
            raise _PageError("page_control_unavailable")
        if not session.sb.evaluate(holders.CONFIRM_JUMP):
            raise _PageError("page_control_unavailable")


def collect_pages(session, conn, netuid, stamp, pages=4):
    """Save up to 1..100 consecutive verified pages, resuming per subnet.

    A resume run reads page 1 only to validate live pagination; this extra
    bootstrap read does not change records, the cursor, or ``pages_read``.
    Completed traversals stay completed until new rows extend the ranking.
    Shrinking counts or a changed page size need a new traversal decision and
    do not silently skip or restart pages. Any mid-run count change stops at
    the last verified checkpoint.
    """
    if type(pages) is not int or not 1 <= pages <= 100:
        raise ValueError("use 1..100 holder pages")
    if type(netuid) is not int or not 0 <= netuid <= 65535:
        raise ValueError("use a valid subnet ID")
    if not isinstance(stamp, str) or not stamp:
        raise ValueError("use a nonempty timestamp")
    schema(conn)
    saved = _state(conn, netuid)
    pages_read, errors, finished = 0, [], False
    target = 1
    try:
        session.pause()
        session.goto("%s/subnets/%d/chart" % (BASE, netuid),
                     check_google=False)
        for _ in range(20):
            if session.sb.evaluate(holders.SELECT_HOLDERS):
                break
            time.sleep(.5)
        else:
            raise _PageError("holders_tab_unavailable")
        bootstrap = holders._read(session, netuid, 1)
        paging, first_coldkeys = _validate(bootstrap, netuid, 1)
        total, items, size = paging
        target = saved[0] if saved else 1
        if saved:
            _, old_total, old_items, old_size = saved
            if size != old_size:
                raise _PageError("page_size_changed")
            if total < old_total or items < old_items:
                raise _PageError("pagination_shrank")
            if target > old_total and items > old_items and old_items % size:
                # New rows can extend the previously partial final page.
                target = old_total
            if target > total:
                finished = True
        if not finished:
            for page in range(target, max(total, 1) + 1):
                target = page
                if pages_read >= pages:
                    break
                if page == 1:
                    observed, coldkeys = paging, first_coldkeys
                else:
                    _select_page(session, page)
                    observed, coldkeys = _validate(
                        holders._read(session, netuid, page), netuid, page)
                if observed != paging:
                    raise _PageError("pagination_changed")
                _save_page(conn, netuid, page, paging, coldkeys, stamp)
                pages_read += 1
                finished = page >= total
    except Exception as exc:
        # Browser and database exceptions may contain URLs, paths or secrets.
        code = str(exc) if isinstance(exc, _PageError) else type(exc).__name__
        errors.append("page_%d: %s" % (target, code))
    saved = _state(conn, netuid)
    captured = conn.execute("SELECT COUNT(*) FROM holder_registry "
                            "WHERE netuid=?", (netuid,)).fetchone()[0]
    return {
        "status": "traversed_live" if finished else (
            "partial" if pages_read else "unavailable"),
        "captured": captured, "pages_read": pages_read,
        "next_page": saved[0] if saved else 1,
        "total_pages": saved[1] if saved else None,
        "total_items": saved[2] if saved else None,
        "errors": errors}
