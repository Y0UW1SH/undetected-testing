"""Store and restore Taostats directory checkpoints for GitHub Actions."""
import argparse
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

from seo.sources import tao_census

TABLES = ("census_state", "census_pages", "census_accounts")
MANIFEST = "census-checkpoint.json"
PART_BYTES = 8 * 1024 * 1024
MAX_CAMPAIGN = 80
CHUNKS_PER_JOB = 4
CHUNK_TIMEOUT = 12 * 60


def _manifest(state):
    path = state / MANIFEST
    if not path.exists():
        return {"format": 1, "generations": []}
    m = json.loads(path.read_text(encoding="utf8"))
    if m.get("format") != 1 or not isinstance(m.get("generations"), list):
        raise ValueError("Unsupported census checkpoint manifest")
    return m


def page_versions(path):
    if not path.is_file():
        return {}
    with closing(sqlite3.connect(path)) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='census_pages'").fetchone()
        if not exists:
            return {}
        return {p: (stamp, n) for p, stamp, n in db.execute(
            "SELECT page,checked_at,visits FROM census_pages")}


def _file(state, generation, part):
    return state / "census-data" / ("%06d-%04d.jsonl.gz" % (generation, part))


def _records(db, changed):
    for table in TABLES:
        query = f"SELECT * FROM {table}"
        parameters = ()
        if changed is not None and table != "census_state":
            if not changed:
                continue
            key = "page" if table == "census_pages" else "last_page"
            query += f" WHERE {key} IN ({','.join('?' for _ in changed)})"
            parameters = changed
        for row in db.execute(query, parameters):
            record = json.dumps([table, *row], separators=(",", ":"))
            yield record.encode() + b"\n"


def pack(state, path, before, *, part_size=PART_BYTES):
    """Write a full baseline once, then only verified page/account changes.

    A census stores a union of account keys. Each edited account has its
    last_page set by one of the page transactions that changed this pass.
    Page commits record visits even when the directory returns a tied rank.
    """
    state, path = Path(state), Path(path)
    manifest = _manifest(state)
    generations = manifest["generations"]
    prior_gen = len(generations)
    if not path.is_file():
        return prior_gen
    current = page_versions(path)
    changed = sorted(p for p, v in current.items() if v != before.get(p))
    if not current:
        return prior_gen
    with closing(sqlite3.connect(path)) as db:
        anchor = list(db.execute("SELECT * FROM census_state").fetchone())
    if prior_gen and not changed and generations[-1].get("anchor") == anchor:
        return prior_gen
    gen = prior_gen + 1
    (state / "census-data").mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    buffer = bytearray()
    parts = 0
    with closing(sqlite3.connect(path)) as db:
        for line in _records(db, changed if prior_gen else None):
            if buffer and len(buffer) + len(line) > part_size:
                parts += 1
                _file(state, gen, parts).write_bytes(gzip.compress(
                    buffer, compresslevel=5, mtime=0))
                buffer.clear()
            buffer.extend(line)
            digest.update(line)
    if buffer:
        parts += 1
        _file(state, gen, parts).write_bytes(gzip.compress(
            buffer, compresslevel=5, mtime=0))
    if not parts:
        raise ValueError("Empty census checkpoint")
    generations.append(dict(parts=parts, sha256=digest.hexdigest(),
                            anchor=anchor, next_page=_cursor(path)))
    tmp = state / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf8")
    tmp.replace(state / MANIFEST)
    return gen


def _replay(db, state, gen, info):
    digest = hashlib.sha256()
    with db:
        for part in range(1, info["parts"] + 1):
            with gzip.open(_file(state, gen, part), "rb") as f:
                for line in f:
                    digest.update(line)
                    table, *row = json.loads(line)
                    if table not in TABLES:
                        raise ValueError("Unknown table in census checkpoint")
                    db.execute(f"INSERT OR REPLACE INTO {table} VALUES "
                               f"({','.join('?' for _ in row)})", row)
        if digest.hexdigest() != info["sha256"]:
            raise ValueError(f"Census checkpoint {gen} digest mismatch")


def restore(state, dest, *, through=None, replace=False):
    """Restore into a new SQLite file. Publish only fully verified output."""
    state, dest = Path(state), Path(dest)
    if not state.is_dir():
        raise ValueError("Census state directory is missing")
    if dest.exists() and not replace:
        raise FileExistsError(dest)
    manifest = _manifest(state)
    generations = manifest["generations"]
    if through is not None and not 1 <= through <= len(generations):
        raise ValueError("Requested checkpoint generation is unavailable")
    legacy = state / "accounts.sqlite"
    if not generations and not legacy.is_file():
        raise ValueError("The branch contains no census checkpoint")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as f:
        tmp = Path(f.name)
    try:
        with closing(sqlite3.connect(tmp)) as db:
            if generations:
                tao_census._schema(db)
                for gen, info in enumerate(generations[:through], 1):
                    _replay(db, state, gen, info)
            else:
                uri = legacy.resolve().as_uri() + "?mode=ro"
                with closing(sqlite3.connect(uri, uri=True)) as old:
                    old.backup(db)
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Restored census failed SQLite quick_check")
            anchors = db.execute("SELECT COUNT(*) FROM census_state")
            if not anchors.fetchone()[0] and generations:
                raise ValueError("Census checkpoint lacks a verified anchor")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def continuation(report, before, remaining):
    if report.get("error"):
        return "failed"
    cursor = report.get("next_page")
    if cursor is None and report.get("status") in (
            "complete_single_block", "traversed_live"):
        return "complete"
    if (not isinstance(cursor, int) or cursor <= before
            or not report.get("verified_pages_this_run")):
        return "stalled"
    return "continue" if remaining else "limit"


def _cursor(path):
    if not path.exists():
        return 1
    with closing(sqlite3.connect(path)) as db:
        state = db.execute("SELECT page_size,latest_total FROM census_state "
                           "WHERE id=1").fetchone()
        return tao_census._next_page(db, *state) if state else 1


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, text=True, check=True,
                          stdout=subprocess.PIPE, timeout=150).stdout.strip()


def _save(state, db, before, branch):
    previous = len(_manifest(state)["generations"])
    gen = pack(state, db, before)
    if gen != previous:
        _git("add", "--", MANIFEST, "census-data", cwd=state)
        # Migrate a legacy branch without retaining another raw database
        # in its new tree. The older Git commit stays reachable.
        if _git("ls-files", "--", "accounts.sqlite", cwd=state):
            _git("rm", "--cached", "--", "accounts.sqlite", cwd=state)
        _git("commit", "-m", f"Save account census checkpoint {gen} [skip ci]",
             cwd=state)
        commit = _git("rev-parse", "HEAD", cwd=state)
        # Use the primary checkout's GitHub credentials; never force push.
        _git("push", "origin", f"{commit}:refs/heads/{branch}")
    return gen


def _scan(db, out, pages, delay, timeout):
    out.unlink(missing_ok=True)
    result = subprocess.run([
        "timeout", "--kill-after=15s", f"{timeout}s", sys.executable,
        "-m", "seo.sources.tao_census", "--db", str(db), "--out", str(out),
        "--page-size", "100", "--pages", str(pages), "--delay", delay,
        "--sample-last"], check=False)
    if result.returncode:
        if out.is_file():
            report = json.loads(out.read_text(encoding="utf8"))
            report["error"] = report.get("error") or "Census process failed"
        else:
            with closing(sqlite3.connect(db)) as conn:
                tao_census._schema(conn)
                error = f"Census process stopped (exit {result.returncode})"
                report = tao_census._report(conn, [], error)
        tao_census._write_report(out, report)
        return report
    return json.loads(out.read_text(encoding="utf8"))


def _finish(reason, report, state, remaining, mode):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf8") as out:
        print("continue=" + str(reason == "continue").lower(), file=out)
        print("complete=" + str(reason == "complete").lower(), file=out)
        if reason == "continue":
            print("commit=" + _git("rev-parse", "HEAD", cwd=state), file=out)
            print(f"remaining={remaining}", file=out)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf8") as out:
        print("## Taostats account census", file=out)
        print(f"\n{mode.title()} scan stopped with status `{reason}`.\n",
              file=out)
        count = report.get("unique_coldkeys", 0)
        print(f"Observed {count:,} distinct coldkeys. "
              f"Directory total: {report.get('total_reported')}. "
              f"First unverified page: {report.get('next_page')}.", file=out)
        gen = len(_manifest(state)["generations"])
        if gen:
            print(f"\nSaved checkpoint generation **{gen}**.", file=out)
        print("\nThese are directory observations. Wallet histories and "
              "transfer ties are checked separately.", file=out)
    return 0 if reason in ("continue", "complete", "manual") else 1


def run(args):
    state, db, out = Path(args.state), Path(args.db), Path(args.out)
    full = args.mode == "full"
    if full and os.environ.get("GITHUB_RUN_ATTEMPT", "1") != "1":
        raise ValueError("Start a new full dispatch to resume after a failure")
    if args.expected:
        if not full or args.expected != _git("rev-parse", "HEAD", cwd=state):
            raise ValueError("Continuation checkpoint differs from the branch")
    if not 1 <= args.remaining <= MAX_CAMPAIGN or not 1 <= args.pages <= 100:
        raise ValueError("Use 1..80 full chunks and 1..100 manual pages")
    pauses = tao_census.delay_range(args.delay)
    if full and (pauses[0] < 1 or pauses[1] > 6):
        raise ValueError("Use 1..6 seconds for the bounded full census")
    if (state / MANIFEST).is_file() or (state / "accounts.sqlite").is_file():
        restore(state, db)
    started = float(os.environ.get("CENSUS_JOB_EPOCH", time.time()))
    available = int(started + 37 * 60 - time.time())
    if available < 180:
        raise RuntimeError("Too little time remains to safely start the scan")
    deadline = time.monotonic() + min(available, (27 if full else 35) * 60)
    remaining = args.remaining
    report = {}
    reason = "manual"
    for _ in range(min(CHUNKS_PER_JOB, remaining) if full else 1):
        before = page_versions(db)
        cursor = _cursor(db) or 1
        remaining -= 1
        seconds = max(1, int(deadline - time.monotonic()))
        timeout = min(CHUNK_TIMEOUT, seconds) if full else seconds
        report = _scan(db, out, 100 if full else args.pages, args.delay,
                       timeout)
        if report.get("error") and not report.get("verified_pages_this_run"):
            verified = page_versions(db)
            report["verified_pages_this_run"] = sorted(
                p for p, v in verified.items() if before.get(p) != v)
            tao_census._write_report(out, report)
        _save(state, db, before, args.branch)
        reason = continuation(report, cursor, remaining)
        print(f"Next page {report.get('next_page')}. "
              f"Remaining full chunks {remaining}.", flush=True)
        if not full:
            if reason not in ("complete", "failed"):
                reason = "manual"
            break
        if reason != "continue" or deadline - time.monotonic() < CHUNK_TIMEOUT:
            break
    return _finish(reason, report, state, remaining, args.mode)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    rd = sub.add_parser("restore", help="reconstruct a local census database")
    rd.add_argument("--state", required=True)
    rd.add_argument("--db", required=True)
    rd.add_argument("--through", type=int)
    rd.add_argument("--replace", action="store_true")
    worker = sub.add_parser("run", help="run bounded GitHub Actions chunks")
    for name in ("state", "db", "out", "branch"):
        worker.add_argument(f"--{name}", required=True)
    worker.add_argument("--mode", choices=("chunk", "full"), default="chunk")
    worker.add_argument("--delay", default="2")
    worker.add_argument("--pages", type=int, default=8)
    worker.add_argument("--remaining", type=int, default=80)
    worker.add_argument("--expected", default="")
    args = parser.parse_args(argv)
    if args.command == "restore":
        restore(args.state, args.db, through=args.through,
                replace=args.replace)
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
