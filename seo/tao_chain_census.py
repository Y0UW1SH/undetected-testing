"""Enumerate Finney System.Account at one finalized block, on this PC only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import urllib.request

from seo.tao_local_watch import WatchError, _addr, _endpoint

# xxhash128("System") + xxhash128("Account"); map keys are Blake2_128Concat.
PREFIX = ("0x26aa394eea5630e07c48ae0c9558cef7"
          "b99d880ec681799c0cf30e8886371da9")
HEX = re.compile(r"0x[0-9a-f]{64}")


def address(key):
    if not isinstance(key, str) or not key.startswith(PREFIX):
        raise ValueError("unexpected storage prefix")
    tail = bytes.fromhex(key[len(PREFIX):])
    if len(tail) != 48 or hashlib.blake2b(
            tail[16:], digest_size=16).digest() != tail[:16]:
        raise ValueError("unexpected System.Account storage key")
    return _addr(tail[16:])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def rpc_client(ws, max_calls):
    # Never follow redirects with an endpoint whose path contains a secret.
    endpoint = "https://" + ws.split("://", 1)[1]
    last = [0.]
    lock = threading.Lock()
    calls = 0

    def rpc(method, params):
        nonlocal calls
        body = json.dumps(dict(jsonrpc="2.0", id=1, method=method,
                               params=params)).encode()
        req = urllib.request.Request(endpoint, body, headers={
            "Content-Type": "application/json", "User-Agent": "FinneyCensus/1"
        })
        for attempt in range(3):
            with lock:
                if calls >= max_calls:
                    raise WatchError("Local request cap reached. "
                                     "Checkpoint retained.")
                time.sleep(max(0., .34 - (time.monotonic() - last[0])))
                last[0] = time.monotonic()
                calls += 1
            try:
                opener = urllib.request.build_opener(NoRedirect)
                with opener.open(req, timeout=35) as response:
                    data = json.load(response)
                break
            except (OSError, ValueError) as exc:
                http = getattr(exc, "code", None)
                retryable = http in (None, 408, 429, 500, 502, 503, 504)
                if attempt >= 2 or not retryable:
                    raise WatchError(f"Local RPC {method} failed "
                                     f"({type(exc).__name__}). "
                                     "Checkpoint retained.") from None
                time.sleep(2 ** attempt)
        if "error" in data or "result" not in data:
            code = (data.get("error") or {}).get("code")
            raise WatchError(f"Local RPC {method} failed (code {code}).")
        return data["result"]

    return rpc


def ranged_scan(db, rpc, page_size, max_pages, workers):
    """Resume the ordered scan in disjoint first-byte storage prefixes."""
    summary = scan(db, rpc, page_size, 0)
    if summary["complete"]:
        return summary
    cursor = db.execute("SELECT cursor FROM census_pin").fetchone()[0] or ""
    block = summary["block_hash"]
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS census_prefixes "
                   "(prefix TEXT PRIMARY KEY, after_key TEXT, done INTEGER)")
        for value in range(256):
            prefix = f"{PREFIX}{value:02x}"
            done = bool(cursor and cursor[:len(prefix)] > prefix)
            after = cursor if cursor.startswith(prefix) else None
            db.execute("INSERT OR IGNORE INTO census_prefixes VALUES(?,?,?)",
                       (prefix, after, int(done)))
    reads = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while reads < max_pages:
            rows = db.execute("SELECT prefix,after_key FROM census_prefixes "
                              "WHERE done=0 ORDER BY prefix LIMIT ?",
                              (min(workers, max_pages - reads),)).fetchall()
            if not rows:
                break
            pending = [(prefix, after, pool.submit(
                rpc, "state_getKeysPaged", [prefix, page_size, after, block]))
                for prefix, after in rows]
            for prefix, after, job in pending:
                keys = job.result()
                if (not isinstance(keys, list) or len(keys) > page_size
                        or not all(isinstance(k, str) and k.startswith(prefix)
                                   for k in keys)
                        or keys != sorted(set(keys))
                        or (after and keys and keys[0] <= after)):
                    raise WatchError("Invalid prefix page. Checkpoint kept.")
                pairs = [(address(k), k) for k in keys]
                with db:
                    db.executemany("INSERT INTO native_accounts VALUES (?,?)",
                                   pairs)
                    db.execute("UPDATE census_prefixes SET after_key=?,done=? "
                               "WHERE prefix=?", (keys[-1] if keys else after,
                                                  int(not keys), prefix))
                    if keys:
                        db.execute("UPDATE census_pin SET pages=pages+1")
                reads += 1
            if reads % 100 == 0:
                print(f"Read {reads} prefix pages at pinned block "
                      f"{summary['finalized_block']}.", flush=True)
    remaining = db.execute("SELECT COUNT(*) FROM census_prefixes WHERE done=0"
                           ).fetchone()[0]
    if not remaining:
        with db:
            db.execute("UPDATE census_pin SET complete=1")
    summary = scan(db, rpc, page_size, 0)
    return dict(summary, page_reads_this_run=reads,
                completed_prefixes=256 - remaining, total_prefixes=256)


def scan(db, rpc, page_size, max_pages):
    db.execute("CREATE TABLE IF NOT EXISTS census_pin (id INTEGER PRIMARY KEY,"
               " block_hash TEXT, height INTEGER, cursor TEXT, pages INTEGER,"
               " complete INTEGER, started TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS native_accounts "
               "(address TEXT PRIMARY KEY, storage_key TEXT UNIQUE)")
    pin = db.execute("SELECT block_hash, height, cursor, pages, complete "
                     "FROM census_pin WHERE id=1").fetchone()
    if pin:
        block, height, cursor, pages, complete = pin
    else:
        block = rpc("chain_getFinalizedHead", [])
        if not isinstance(block, str) or not HEX.fullmatch(block):
            raise WatchError("Finalized block hash unavailable.")
        height = int(rpc("chain_getHeader", [block])["number"], 16)
        cursor, pages, complete = None, 0, 0
        with db:
            db.execute("INSERT INTO census_pin VALUES(1,?,?,?,?,?,?)",
                       (block, height, cursor, pages, complete,
                        datetime.now(timezone.utc).isoformat()))
    reads = 0
    for _ in range(max_pages if not complete else 0):
        keys = rpc("state_getKeysPaged", [PREFIX, page_size, cursor, block])
        reads += 1
        if not isinstance(keys, list) or len(keys) > page_size:
            raise WatchError("Invalid account page; checkpoint retained.")
        if not keys:
            with db:
                db.execute("UPDATE census_pin SET complete=1 WHERE id=1")
            complete = 1
            break
        if sorted(set(keys)) != keys or (cursor and keys[0] <= cursor):
            raise WatchError("Account page did not advance at pinned head.")
        pairs = [(address(k), k) for k in keys]
        with db:
            db.executemany("INSERT INTO native_accounts VALUES (?,?)", pairs)
            cursor, pages = keys[-1], pages + 1
            db.execute("UPDATE census_pin SET cursor=?,pages=? WHERE id=1",
                       (cursor, pages))
        if pages % 100 == 0:
            print(f"Stored {pages} account pages at block {height}.",
                  flush=True)
    count = db.execute("SELECT COUNT(*) FROM native_accounts").fetchone()[0]
    return dict(source="Finney System.Account", block_hash=block,
                finalized_block=height, accounts=count, pages=pages,
                page_reads_this_run=reads, complete=bool(complete),
                scope="Native system accounts present at this block, not "
                "historical users, stake lots, or proof of wallet ownership.")


def attach_receipts(db, paths):
    db.execute("CREATE TABLE IF NOT EXISTS observed_transfers ("
               "id TEXT PRIMARY KEY, stamp TEXT, src TEXT, dst TEXT,"
               " kind TEXT, evidence TEXT, receipt TEXT)")
    with db:
        for path in paths:
            r = json.loads(path.read_text(encoding="utf8"))
            for e in r["transfer_details"]:
                if not e["src"] or not e["dst"] or e["src"] == e["dst"]:
                    continue
                db.execute("INSERT INTO observed_transfers VALUES "
                           "(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                           "stamp=excluded.stamp,receipt=excluded.receipt "
                           "WHERE excluded.stamp>=observed_transfers.stamp",
                           (e["id"], r["checked_at"], e["src"], e["dst"],
                            e["kind"], e["evidence"], json.dumps(e)))
    q = db.execute("SELECT COUNT(*),COUNT(DISTINCT src||':'||dst) "
                   "FROM observed_transfers WHERE evidence='transaction'")
    n, pairs = q.fetchone()
    count = db.execute("SELECT COUNT(*) FROM native_accounts WHERE address "
                       "IN (SELECT src FROM observed_transfers WHERE "
                       "evidence='transaction' UNION SELECT dst FROM "
                       "observed_transfers WHERE evidence='transaction')"
                       ).fetchone()[0]
    return dict(transfer_records=n, observed_directed_pairs=pairs,
                native_accounts_in_observed_transfers=count)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--report", type=Path, action="append", default=[])
    p.add_argument("--page-size", type=int, default=512)
    p.add_argument("--max-pages", type=int, default=1500)
    p.add_argument("--workers", type=int, choices=range(1, 5), default=3)
    p.add_argument("--prompt", action="store_true")
    args = p.parse_args(argv)
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        print("The chain census runs only on a local PC.", file=sys.stderr)
        return 2
    if not 1 <= args.max_pages <= 2000 or not 1 <= args.page_size <= 1000:
        p.error("use up to 2,000 pages and 1–1,000 keys per page")
    try:
        args.db.parent.mkdir(parents=True, exist_ok=True)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        ws = _endpoint(args.prompt)
        with sqlite3.connect(args.db) as db:
            summary = ranged_scan(db, rpc_client(ws, args.max_pages + 2),
                                  args.page_size, args.max_pages, args.workers)
            summary.update(attach_receipts(db, args.report))
        args.out.write_text(json.dumps(summary, indent=2), encoding="utf8")
        print(json.dumps(summary), flush=True)
        return 0
    except WatchError as exc:
        print(exc, file=sys.stderr)
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        print(f"Census stopped: {type(exc).__name__}; checkpoint retained.",
              file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
