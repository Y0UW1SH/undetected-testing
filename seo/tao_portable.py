"""Move downloaded wallet report/state pairs between local installations."""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
import time
import zipfile
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime
from pathlib import Path


FORMAT = "taostats-wallet-backup"
REPORT = "tao-wallet-report/tao_collect.json"
STATE = "tao-wallet-state/wallets.sqlite"
FEED = "try_tao_out/local_wallet_activity.json"
RUN = r"[0-9]{1,20}"
# tao_view.fetch also preserves repeat downloads under artifacts/GENERATION.
DOWNLOAD = r"\.download-[a-z0-9_]{1,32}"
GENERATION = rf"(?:{RUN}(?:-{DOWNLOAD})?|{DOWNLOAD})"
GROUP = re.compile(rf"github_out/{RUN}(?:/artifacts/{GENERATION})?")
MAX_DB = 512 * 1024**2
MAX_JSON = 64 * 1024**2
MAX_TOTAL = 1024**3
MAX_MEMBERS = 1024
MAX_RATIO = 500
CHUNK = 256 * 1024
MANIFEST = "manifest.json"
MAX_MANIFEST = 512 * 1024


def _path(root, name):
    """Reject symlinks, junctions and other reparse points in data paths."""
    current = root
    for part in Path(name).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(info, "st_file_attributes", 0)
        if (stat.S_ISLNK(info.st_mode)
                or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("Linked data path refused: " + name)
    return current


def _groups(root, run_ids):
    cache = _path(root, "github_out")
    if not cache.exists():
        return
    folders = [cache / run for run in run_ids] if run_ids else cache.iterdir()
    for folder in sorted(folders):
        if not re.fullmatch(RUN, folder.name):
            continue
        group = "github_out/" + folder.name
        if not _path(root, group).is_dir():
            continue
        yield group
        reruns = _path(root, group + "/artifacts")
        if reruns.is_dir():
            for entry in sorted(reruns.iterdir()):
                if re.fullmatch(GENERATION, entry.name):
                    yield group + "/artifacts/" + entry.name


def _limit(name):
    if name == MANIFEST:
        return MAX_MANIFEST
    return MAX_DB if name.endswith("/" + STATE) else MAX_JSON


def _pairs(names):
    groups = set()
    for name in names:
        if name == FEED:
            continue
        parent, sep, leaf = name.rpartition("/tao-wallet-")
        if (not sep or not GROUP.fullmatch(parent)
                or "tao-wallet-" + leaf not in (REPORT, STATE)):
            raise ValueError("Unexpected wallet backup member")
        groups.add(parent)
    for group in groups:
        if not all(group + "/" + leaf in names for leaf in (REPORT, STATE)):
            raise ValueError("incomplete wallet report/state pair: " + group)
    return sorted(groups)


def _json(path):
    if path.stat().st_size > MAX_JSON:
        raise ValueError("Wallet JSON exceeds the portable size limit")
    with path.open(encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError("Invalid wallet JSON")
    return value


def _feed(path):
    data = _json(path)
    allowed = {"source", "addresses", "checked_at", "poll_interval_seconds",
               "stale_after_seconds", "status", "head", "genesis_hash",
               "coverage_start", "address_coverage_start", "events",
               "discarded_events", "retained_events", "last_scanned_block",
               "lag_blocks", "rpc_calls", "rpc_limit", "rpc_rate_limit",
               "selection_reasons", "timing_selection", "snapshot_refresh",
               "rpc_attempts", "rpc_retry", "rpc_error"}
    event_fields = {"id", "block_number", "event_index", "extrinsic",
                    "event_name", "kind", "src", "dst", "hotkey",
                    "dst_hotkey", "netuid", "dst_netuid", "tao", "tao_rao",
                    "alpha", "alpha_units", "dst_alpha", "dst_alpha_units",
                    "time"}
    if (data.get("source") != "local-finney-events-v1"
            or not isinstance(data.get("addresses"), list)
            or not isinstance(data.get("events"), list)
            or set(data) - allowed
            or any(not isinstance(e, dict) or set(e) - event_fields
                   for e in data["events"])
            or not isinstance(data.get("timing_selection", {}), dict)
            or set(data.get("timing_selection", {}))
            - {"discovery_at", "buy", "sell"}
            or not isinstance(data.get("snapshot_refresh", {}), dict)
            or set(data.get("snapshot_refresh", {}))
            - {"checked_at", "status"}):
        raise ValueError("Unexpected local watcher feed schema")
    attempts = data.get("rpc_attempts", 0)
    if type(attempts) is not int or not 0 <= attempts <= 2:
        raise ValueError("Invalid watcher feed RPC attempt count")
    stages = ("genesis", "finalized_head", "head_height", "block_hash",
              "block_events", "decode_events", "block_timestamp", "connect",
              "poll")
    types = ("timeout", "connection", "invalid_data", "rpc_error")
    for key in ("rpc_error", "rpc_retry"):
        if key in data:
            error = data[key]
            if (not isinstance(error, dict) or set(error) != {"stage", "type"}
                    or error["stage"] not in stages or error["type"]
                    not in types):
                raise ValueError("Invalid watcher feed RPC diagnostic")


@contextmanager
def _freeze_feed(root):
    path = _path(root, FEED + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=0)) as lock:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise ValueError("Stop the local watcher before migrating its "
                             "feed and checkpoint") from exc
        yield


def _read_db(path, immutable=False):
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    db = sqlite3.connect(uri, uri=True, timeout=3)
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA query_only=ON")
    return db


def _snapshot(src, dest):
    """Capture one committed SQLite snapshot, including any WAL pages."""
    deadline = time.monotonic() + 30

    def bounded(status, remaining, total):
        if time.monotonic() > deadline:
            raise ValueError("Wallet SQLite snapshot busy; retry when idle")

    if src.stat().st_size > MAX_DB:
        raise ValueError("Wallet SQLite exceeds the portable size limit")
    with closing(_read_db(src)) as source:
        source.execute("BEGIN")
        pages = source.execute("PRAGMA page_count").fetchone()[0]
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        if pages * page_size > MAX_DB:
            raise ValueError("Wallet SQLite exceeds the portable size limit")
        with closing(sqlite3.connect(dest)) as copy:
            source.backup(copy, pages=256, progress=bounded, sleep=0.05)
            copy.execute("PRAGMA journal_mode=DELETE").fetchone()


def _validate_pair(report, state):
    data = _json(report)
    stamp = data.get("checked_at")
    if (not isinstance(stamp, str)
            or not isinstance(data.get("coverage"), dict)
            or not isinstance(data.get("following"), list)
            or datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo
            is None):
        raise ValueError("Invalid dated wallet report")
    with closing(_read_db(state, immutable=True)) as db:
        deadline = time.monotonic() + 30
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 5000)
        tables = dict(db.execute("SELECT name, sql FROM sqlite_master "
                                 "WHERE type='table'"))
        if not tables.get("wallet_events", "").upper().startswith(
                "CREATE TABLE"):
            raise ValueError("Invalid wallet state database")
        if db.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
            raise ValueError("Wallet SQLite integrity check failed")
        # Releases before the performance table have no report timestamp key.
        if "performance" in data:
            if (not tables.get("wallet_performance_runs", "").upper()
                    .startswith("CREATE TABLE") or not db.execute(
                        "SELECT 1 FROM wallet_performance_runs "
                        "WHERE checked_at=? LIMIT 1", (stamp,)).fetchone()):
                raise ValueError("Wallet report/state timestamp mismatch")


def _stream(source, limit, target=None):
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise ValueError("Backup member exceeds its allowed size")
        digest.update(chunk)
        if target is not None:
            target.write(chunk)
    return dict(size=size, sha256=digest.hexdigest())


def _meta(path, limit):
    with path.open("rb") as source:
        return _stream(source, limit)


def _copy(src, dest, limit):
    with src.open("rb") as source, dest.open("xb") as target:
        return _stream(source, limit, target)


def _publish(src, dest):
    """Hard links publish completed files without replacing existing data."""
    try:
        os.link(src, dest)
    except FileExistsError:
        raise
    except OSError:
        # FAT/exFAT media may lack hard links. Keep exclusive creation there.
        with src.open("rb") as source, dest.open("xb") as target:
            try:
                shutil.copyfileobj(source, target, CHUNK)
                target.flush()
                os.fsync(target.fileno())
            except BaseException:
                target.close()
                dest.unlink()
                raise


def _safe_db(root, name):
    for suffix in ("-wal", "-shm", "-journal"):
        _path(root, name + suffix)
    return _path(root, name)


def export_bundle(root, archive, *, include_local_feed=False, run_ids=()):
    """Create a new version-1 ZIP. Never replace an existing backup."""
    root, archive = Path(root).resolve(), Path(archive).absolute()
    if archive.suffix.lower() != ".zip":
        raise ValueError("Choose a .zip backup filename")
    if archive.exists():
        raise FileExistsError("Backup already exists: " + str(archive))
    run_ids = {str(run) for run in run_ids}
    if any(not re.fullmatch(RUN, run) for run in run_ids):
        raise ValueError("Specify numeric GitHub Actions run IDs")
    groups = []
    for group in _groups(root, run_ids):
        present = [_path(root, group + "/" + leaf).is_file()
                   for leaf in (REPORT, STATE)]
        if any(present):
            if not all(present):
                raise ValueError(f"incomplete wallet pair: {group}")
            groups.append(group)
    if run_ids - {group.split("/")[1] for group in groups}:
        raise ValueError("A requested GitHub run has no complete wallet pair")
    private = include_local_feed and _path(root, FEED).is_file()
    if include_local_feed and not private:
        raise ValueError("Requested local watcher feed could not be found")
    if not groups and not private:
        raise ValueError("No downloaded wallet report/state pairs to export")
    if len(groups) * 2 + int(private) + 1 > MAX_MEMBERS:
        raise ValueError("Too many wallet snapshots for one backup")
    with tempfile.TemporaryDirectory(dir=archive.parent) as temp:
        stage = Path(temp)
        files = {}
        size = 0
        for group in groups:
            report, state = [group + "/" + leaf for leaf in (REPORT, STATE)]
            for name in (report, state):
                (stage / name).parent.mkdir(parents=True)
            source = _path(root, report)
            files[report] = _copy(source, stage / report, MAX_JSON)
            _snapshot(_safe_db(root, state), stage / state)
            _validate_pair(stage / report, stage / state)
            if _meta(source, MAX_JSON) != files[report]:
                raise ValueError("Wallet report changed during export; retry")
            files[state] = _meta(stage / state, MAX_DB)
            size += files[report]["size"] + files[state]["size"]
            if size > MAX_TOTAL:
                raise ValueError("Wallet snapshots exceed the backup limit")
        if private:
            with _freeze_feed(root):
                (stage / FEED).parent.mkdir()
                source = _path(root, FEED)
                _feed(source)
                files[FEED] = _copy(source, stage / FEED, MAX_JSON)
                _feed(stage / FEED)
        if size + files.get(FEED, {}).get("size", 0) > MAX_TOTAL:
            raise ValueError("Wallet snapshots exceed the backup limit")
        bundle = stage / "backup.zip"
        manifest = dict(format=FORMAT, version=1, files=files)
        store = set()
        while True:
            with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED,
                                 compresslevel=1) as z:
                z.writestr(MANIFEST, json.dumps(manifest, sort_keys=True))
                for name in files:
                    if name in store:
                        z.write(stage / name, name, zipfile.ZIP_STORED)
                    else:
                        z.write(stage / name, name)
                dense = {i.filename for i in z.infolist() if _too_dense(i)}
            if not dense:
                break
            store.update(dense)
        _publish(bundle, archive)
    return dict(pairs=len(groups), files=len(files), private_feed=private)


def _central_directory(source):
    """Bound zipfile's central-directory allocation before opening the ZIP."""
    length = source.seek(0, os.SEEK_END)
    if length < 22 or length > MAX_TOTAL + 8 * 1024**2:
        raise ValueError("Invalid backup ZIP size")
    # Our format uses single-disk ZIPs, no ZIP64, comments or trailing data.
    source.seek(-22, os.SEEK_END)
    signature, disk, start_disk, local, count, size, offset, comment = (
        struct.unpack("<4s4H2LH", source.read(22)))
    if (signature != b"PK\x05\x06" or disk or start_disk or comment
            or local != count or not 1 <= count <= MAX_MEMBERS
            or size > MAX_MEMBERS * 512 or offset + size != length - 22):
        raise ValueError("Invalid or oversized backup ZIP directory")


def _too_dense(info):
    return info.file_size > max(1024**2, info.compress_size * MAX_RATIO)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate portable manifest key")
        result[key] = value
    return result


def _unpack(archive, stage, private):
    with archive.open("rb") as source:
        _central_directory(source)
        with zipfile.ZipFile(source) as z:
            infos = z.infolist()
            names = [i.orig_filename for i in infos]
            if len(names) > MAX_MEMBERS:
                raise ValueError("Too many portable backup members")
            if len(set(names)) != len(names):
                raise ValueError("duplicate backup member")
            if names.count(MANIFEST) != 1:
                raise ValueError("Missing portable backup manifest")
            payload = set(names) - {MANIFEST}
            groups = _pairs(payload)
            if not payload:
                raise ValueError("Empty portable backup")
            if FEED in payload and not private:
                raise ValueError("This backup includes a private watcher "
                                 "feed. Add --include-local-feed to restore.")
            total = 0
            for info in infos:
                mode = stat.S_IFMT(info.external_attr >> 16)
                if (info.flag_bits & 1 or mode not in (0, stat.S_IFREG)
                        or info.compress_type not in
                        (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ValueError("Unsupported ZIP member type")
                if info.file_size > _limit(info.orig_filename):
                    raise ValueError("Backup member exceeds its allowed size")
                if _too_dense(info):
                    raise ValueError("Excessive backup compression ratio")
                if info.orig_filename != MANIFEST:
                    total += info.file_size
            if total > MAX_TOTAL:
                raise ValueError("Portable backup exceeds total size limit")
            manifest = json.loads(z.read(MANIFEST),
                                  object_pairs_hook=_unique_keys)
            if (not isinstance(manifest, dict)
                    or manifest.get("format") != FORMAT
                    or type(manifest.get("version")) is not int
                    or manifest["version"] != 1):
                raise ValueError("Unsupported portable backup version")
            files = manifest.get("files")
            if not isinstance(files, dict) or set(files) != payload:
                raise ValueError("Portable manifest and ZIP members disagree")
            for name in sorted(payload):
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with z.open(name) as member, path.open("xb") as target:
                    info = _stream(member, _limit(name), target)
                if files[name] != info:
                    raise ValueError(f"Backup checksum mismatch: {name}")
            for group in groups:
                _validate_pair(stage / group / REPORT, stage / group / STATE)
            if FEED in payload:
                _feed(stage / FEED)
    return groups, files


def _existing(root, name, metadata, stage):
    dst = _path(root, name)
    if name.endswith("/" + STATE):
        _safe_db(root, name)
        if not dst.exists() and any(Path(str(dst) + s).exists()
                                    for s in ("-wal", "-shm", "-journal")):
            raise ValueError("Wallet migration conflict: orphan SQLite "
                             "sidecar for " + name)
    if not dst.exists():
        return False
    if dst.is_file():
        if name.endswith("/" + STATE):
            # A raw hash ignores live WAL changes. Normalize committed state
            # exactly as export does, also when comparing an original DB.
            normalized = stage / "existing.sqlite"
            _snapshot(dst, normalized)
            try:
                if _meta(normalized, MAX_DB) == metadata:
                    return True
            finally:
                normalized.unlink()
        elif _meta(dst, _limit(name)) == metadata:
            return True
    raise ValueError(f"Wallet migration conflict: {name}")


def import_bundle(root, archive, *, include_local_feed=False):
    """Validate the entire backup and all conflicts before publishing files."""
    root, archive = Path(root).resolve(), Path(archive).absolute()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temp:
        stage = Path(temp)
        groups, files = _unpack(archive, stage, include_local_feed)
        with (_freeze_feed(root) if FEED in files else nullcontext()):
            missing = [name for name in files if not _existing(
                root, name, files[name], stage)]
            # A report becomes visible to the viewer only after its state.
            missing.sort(key=lambda name: name.endswith("/" + REPORT))
            published = []
            try:
                for name in missing:
                    dst = _path(root, name)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _publish(stage / name, dst)
                    published.append((dst, dst.stat()))
            except BaseException:
                for dst, after in reversed(published):
                    # Leave any file that was replaced by another process.
                    if dst.exists() and os.path.samestat(after, dst.stat()):
                        dst.unlink()
                raise
    return dict(pairs=len(groups), restored=len(missing),
                skipped=len(files) - len(missing), private_feed=FEED in files)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "import"))
    parser.add_argument("archive", type=Path, help="wallet data ZIP file")
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="data directory containing github_out")
    parser.add_argument("--include-local-feed", action="store_true",
                        help="also migrate private watcher feed/checkpoint "
                        "(stop the local watcher first)")
    parser.add_argument("--run", action="append", default=[], metavar="RUN_ID",
                        help="export only these cached runs (repeatable)")
    args = parser.parse_args(argv)
    try:
        if args.action == "export":
            result = export_bundle(args.root, args.archive, run_ids=args.run,
                                   include_local_feed=args.include_local_feed)
        else:
            if args.run:
                parser.error("--run applies to exports only")
            result = import_bundle(args.root, args.archive,
                                   include_local_feed=args.include_local_feed)
    except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile,
            RecursionError) as exc:
        parser.exit(1, "Wallet backup failed: %s\n" % exc)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
