"""Keep an offline-readable activity feed for explicitly watched accounts."""
import json

from seo.tao_wallets import _parse_ts


def update(db, stamp, watched, events, coverage):
    """A failed or too-shallow read must not silently advance a watermark."""
    db.execute("CREATE TABLE IF NOT EXISTS wallet_monitor_cursor "
               "(address TEXT, tab TEXT, checked_at TEXT, "
               "PRIMARY KEY(address, tab))")
    db.execute("CREATE TABLE IF NOT EXISTS wallet_monitor_start "
               "(address TEXT, tab TEXT, started_at TEXT, "
               "PRIMARY KEY(address, tab))")
    db.execute("CREATE TABLE IF NOT EXISTS wallet_monitor_feed "
               "(address TEXT, key TEXT, time TEXT, seen_at TEXT, "
               "payload TEXT, PRIMARY KEY(address, key))")
    states = {}
    with db:
        for addr in sorted(watched):
            status = states[addr] = {}
            info = coverage.get(addr, {})
            for tab in ("transactions", "transfers"):
                row = db.execute("SELECT checked_at FROM wallet_monitor_cursor"
                                 " WHERE address=? AND tab=?", (addr, tab)
                                 ).fetchone()
                cursor = row[0] if row else None
                depth = _parse_ts(info.get(tab + "_since"))
                cutoff = _parse_ts(cursor or stamp)
                partial = info.get(tab) == "partial"
                ok = (info.get(tab) == "complete"
                      or (partial and depth and depth <= cutoff))
                if not ok:
                    status[tab] = "gap"
                    continue
                status[tab] = "checked" if cursor else "baseline"
                db.execute("INSERT OR IGNORE INTO wallet_monitor_start VALUES "
                           "(?,?,?)", (addr, tab, cursor or stamp))
                origin = db.execute("SELECT started_at FROM "
                                    "wallet_monitor_start WHERE address=? "
                                    "AND tab=?", (addr, tab)).fetchone()[0]
                if cursor:
                    for e in events:
                        cash = e["kind"] == "tao_transfer"
                        when = _parse_ts(e["time"])
                        if (addr not in (e["src"], e["dst"])
                                or cash != (tab == "transfers")
                                or not _parse_ts(origin) < when
                                <= _parse_ts(stamp)):
                            continue
                        db.execute("INSERT OR IGNORE INTO wallet_monitor_feed "
                                   "VALUES (?,?,?,?,?)",
                                   (addr, e["id"], e["time"], stamp,
                                    json.dumps(e)))
                db.execute("INSERT OR REPLACE INTO wallet_monitor_cursor "
                           "VALUES (?,?,?)", (addr, tab, stamp))
    feed = [dict(json.loads(r[2]), watched_address=r[0], first_reported=r[1])
            for r in db.execute("SELECT address,seen_at,payload FROM "
                                "wallet_monitor_feed ORDER BY time DESC, "
                                "address LIMIT 250")]
    count = db.execute("SELECT COUNT(*) FROM wallet_monitor_feed "
                       "WHERE seen_at=?", (stamp,)).fetchone()[0]
    return dict(status=states, recent=feed, new_count=count)
