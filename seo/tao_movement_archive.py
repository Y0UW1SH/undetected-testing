"""Offline activity and transfer peers from a local finalized-block archive."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

DEFAULT_PATH = Path("try_tao_out/network_movements.sqlite")
APPLICATION_ID = 0x54414F4D
KEEP_BLOCKS = 50_400
KEEP_EVENTS = 100_000
MAX_BYTES = 256 * 1024 * 1024

SCHEMA = (
    "CREATE TABLE identity (genesis TEXT PRIMARY KEY, pruned_through INTEGER)",
    "CREATE TABLE blocks (height INTEGER PRIMARY KEY, hash TEXT NOT NULL, "
    "observed_at TEXT NOT NULL, chain_time TEXT, n_events INTEGER NOT NULL)",
    "CREATE TABLE movements (height INTEGER NOT NULL REFERENCES blocks(height)"
    " ON DELETE CASCADE, event_index INTEGER NOT NULL, kind TEXT NOT NULL, "
    "src TEXT, dst TEXT, payload TEXT NOT NULL, "
    "PRIMARY KEY(height,event_index)) WITHOUT ROWID",
)


class MovementArchive:
    """Keep complete blocks of successful recognized native movements."""

    def __init__(self, path, *, keep_blocks=KEEP_BLOCKS,
                 keep_events=KEEP_EVENTS):
        if min(keep_blocks, keep_events) < 1:
            raise ValueError("Archive limits must be positive")
        self.keep_blocks = keep_blocks
        self.keep_events = keep_events
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=4)
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA trusted_schema=OFF")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                app = self.db.execute("PRAGMA application_id").fetchone()[0]
                if app != APPLICATION_ID:
                    occupied = self.db.execute(
                        "SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
                    if app or occupied:
                        raise ValueError("Choose a separate movement archive")
                    for sql in SCHEMA:
                        self.db.execute(sql)
                    self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            pages = self.db.execute(
                f"PRAGMA max_page_count={MAX_BYTES // page_size}").fetchone()
            if pages[0] * page_size > MAX_BYTES:
                raise ValueError("Archive exceeds its file size limit")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.db.close()

    def record(self, genesis, height, block_hash, seen, chain_time, rows):
        """Commit one complete block before advancing the watched feed."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            ident = self.db.execute(
                "SELECT genesis,pruned_through FROM identity").fetchone()
            if ident and ident[0] != genesis:
                raise ValueError("Archive chain mismatch")
            if not ident:
                self.db.execute("INSERT INTO identity VALUES(?,-1)",
                                (genesis,))
            elif height <= ident[1]:
                return
            known = self.db.execute("SELECT hash FROM blocks WHERE height=?",
                                    (height,)).fetchone()
            if known:
                if known[0] != block_hash:
                    raise ValueError("Conflicting finalized block hash")
                if chain_time is not None:
                    self.db.execute("UPDATE blocks SET chain_time=? "
                                    "WHERE height=? AND chain_time IS NULL",
                                    (chain_time, height))
                return
            latest = self.db.execute("SELECT MAX(height) FROM blocks"
                                     ).fetchone()[0]
            cutoff = max(height, latest or height) - self.keep_blocks + 1
            if height < cutoff:
                return
            self.db.execute("DELETE FROM blocks WHERE height<?", (cutoff,))
            if len(rows) > self.keep_events:
                raise ValueError("One block exceeds the archive event limit")
            self.db.execute("INSERT INTO blocks VALUES(?,?,?,?,?)",
                            (height, block_hash, seen, chain_time, len(rows)))
            self.db.executemany(
                "INSERT INTO movements VALUES(?,?,?,?,?,?)",
                [(height, row["event_index"], row["kind"], row["src"],
                  row["dst"], json.dumps(row, separators=(",", ":"),
                                         allow_nan=False))
                 for row in rows])
            through = max(cutoff - 1, self._prune_events())
            self.db.execute("UPDATE identity SET pruned_through="
                            "MAX(pruned_through,?)", (through,))

    def _prune_events(self):
        stored = self.db.execute("SELECT SUM(n_events) FROM blocks").fetchone()
        excess = stored[0] - self.keep_events
        if excess > 0:
            for height, count in self.db.execute(
                    "SELECT height,n_events FROM blocks ORDER BY height"):
                excess -= count
                if excess <= 0:
                    self.db.execute("DELETE FROM blocks WHERE height<=?",
                                    (height,))
                    return height
        return -1


PARTICIPANTS = """
WITH participants AS (
    SELECT src AS address, dst AS peer, kind, height FROM movements
    WHERE src IS NOT NULL
    UNION ALL
    SELECT dst, src, kind, height FROM movements
    WHERE dst IS NOT NULL AND dst IS NOT src
)
"""


def _report(db, top, wallet):
    if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise ValueError("File is not a movement archive")
    db.row_factory = sqlite3.Row
    coverage = [dict(r) for r in db.execute("""
        SELECT MIN(height) first_block, MAX(height) last_block,
               COUNT(*) blocks, MIN(observed_at) first_seen,
               MAX(observed_at) last_seen
        FROM (SELECT *, height - ROW_NUMBER() OVER (ORDER BY height) AS grp
              FROM blocks)
        GROUP BY grp ORDER BY first_block
    """)]
    kinds = dict(db.execute("SELECT kind, COUNT(*) FROM movements "
                            "GROUP BY kind ORDER BY kind"))
    active = [dict(r) for r in db.execute(PARTICIPANTS + """
        SELECT address, COUNT(*) movements, COUNT(*) OVER() total_wallets,
            SUM(kind='stake_increase') stake_increases,
            SUM(kind='stake_decrease') stake_decreases,
            SUM(kind='tao_transfer') tao_transfers,
            SUM(kind='stake_transfer') stake_transfers,
            COUNT(DISTINCT CASE WHEN peer<>address THEN peer END) peers,
            MIN(height) first_block, MAX(height) last_block
        FROM participants GROUP BY address
        ORDER BY (stake_increases + stake_decreases) DESC,
                 movements DESC, address LIMIT ?
    """, (top,))]
    active_count = active[0]["total_wallets"] if active else 0
    for row in active:
        del row["total_wallets"]
    peers, events = [], []
    if wallet:
        peers = [dict(r) for r in db.execute(PARTICIPANTS + """
            SELECT peer AS address, COUNT(*) movements,
                   MIN(height) first_block, MAX(height) last_block
            FROM participants WHERE address=? AND peer<>address
                AND kind IN ('tao_transfer','stake_transfer')
            GROUP BY peer ORDER BY movements DESC, last_block DESC, peer
            LIMIT ?
        """, (wallet, top))]
        for r in db.execute("""
                SELECT payload, hash, observed_at, chain_time
                FROM movements JOIN blocks USING(height)
                WHERE movements.src=? OR movements.dst=?
                ORDER BY height DESC,event_index DESC LIMIT ?
        """, (wallet, wallet, top)):
            events.append(dict(json.loads(r["payload"]), block_hash=r["hash"],
                               observed_at=r["observed_at"],
                               chain_time=r["chain_time"]))
    genesis = db.execute("SELECT genesis FROM identity").fetchone()
    return dict(source="local-finney-movements-v1",
                genesis=genesis[0] if genesis else None,
                blocks=sum(span["blocks"] for span in coverage),
                movements=sum(kinds.values()), by_kind=kinds,
                coverage=coverage,
                active_wallets=active, active_wallet_count=active_count,
                wallet=wallet, transfer_peers=peers,
                wallet_movements=events)


def report(path=DEFAULT_PATH, *, top=10, wallet=None):
    """Read one consistent local snapshot without opening a provider."""
    if not 1 <= top <= 100:
        raise ValueError("Use a list size of 1 to 100")
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=4)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        return _report(db, top, wallet)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--wallet", help="show this address's transfer peers")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = report(args.db, top=args.top, wallet=args.wallet)
    except (ValueError, sqlite3.Error, OSError):
        print("Could not read this movement archive. Check the file and "
              "use --top 1 through 100.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"{data['blocks']:,} finalized blocks, "
          f"{data['movements']:,} recognized successful movements across "
          f"{data['active_wallet_count']:,} active addresses.")
    for span in data["coverage"][-8:]:
        print(f"  Blocks {span['first_block']} to {span['last_block']} "
              f"({span['blocks']:,} blocks)")
    if len(data["coverage"]) > 8:
        print("Showing the eight latest intervals. --json includes them all.")
    print("Active wallets by observed stake increases and decreases")
    for row in data["active_wallets"]:
        print(f"  {row['address']}  {row['stake_increases']} increases, "
              f"{row['stake_decreases']} decreases, {row['movements']} "
              f"movements, {row['peers']} transfer peers")
    if args.wallet:
        print(f"Observed transfer peers for {args.wallet}")
        for row in data["transfer_peers"]:
            print(f"  {row['address']}  {row['movements']} transfers, "
                  f"latest block {row['last_block']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
