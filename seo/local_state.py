"""Private state, streamed SQLite backups and non-destructive recovery."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile


class StateError(Exception):
    """An actionable local-state error, safe to print."""


def default_root():
    if os.environ.get('TAO_LOCAL_HOME'):
        return Path(os.environ['TAO_LOCAL_HOME']).expanduser().resolve()
    if os.environ.get('LOCALAPPDATA'):
        return Path(os.environ['LOCALAPPDATA']) / (
            'StealthWalletMonitor/LocalRunner')
    return Path.home() / '.local/share/stealth-wallet-monitor'


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as src:
        for block in iter(lambda: src.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            out.write(data.encode('utf8') if isinstance(data, str) else data)
            out.flush()
            os.fsync(out.fileno())
            out.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def lease(root, timeout=0):
    """OS-released SQLite lease: no stale PID file after a crash/reboot."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / '.runner.lock',
                                 timeout=timeout)) as db:
        try:
            db.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError:
            raise StateError('Another local job is running. Try later.')
        yield


def sqlite_copy(source, destination):
    """Copy live SQLite including committed WAL, without a size cap."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    if not source.is_file():
        raise StateError('Source database does not exist.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        destination.name + '.' + uuid.uuid4().hex)
    deadline = time.monotonic() + 300

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise StateError('Database backup timed out; nothing replaced.')

    created = False
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=5)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst, pages=1024, progress=progress, sleep=0.1)
                if dst.execute('PRAGMA quick_check').fetchone() != ('ok',):
                    raise StateError('Database failed SQLite quick_check.')
        # Exclusive creation also closes the race after the initial check.
        with destination.open('xb') as out, temporary.open('rb') as src:
            created = True
            shutil.copyfileobj(src, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        # Remove only our incomplete new file, never a pre-existing one.
        if created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _relative(name):
    p = PurePosixPath(name)
    if (not isinstance(name, str) or not name or p.is_absolute()
            or '..' in p.parts or '\\' in name or ':' in name):
        raise StateError('Unsafe backup path.')
    return Path(*p.parts)


def _backup_files(root):
    # Allowlist intentionally excludes credentials, browser profiles and logs.
    paths = list(root.glob('*.sqlite'))
    for folder in ('reports', 'config', 'try_tao_out', 'snapshots'):
        base = root / folder
        if base.is_dir():
            paths.extend(p for p in base.rglob('*') if p.suffix in
                         ('.sqlite', '.json', '.html', '.csv'))
    return sorted(p for p in paths if p.is_file() and not p.is_symlink())


def backup(root):
    """Create a fresh verified generation. Caller holds the runner lease."""
    root = Path(root)
    parent = root / 'backups'
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.incomplete-', dir=parent))
    try:
        files = {}
        for source in _backup_files(root):
            relative = source.relative_to(root)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix == '.sqlite':
                sqlite_copy(source, target)
            else:
                shutil.copyfile(source, target)
            files[relative.as_posix()] = digest(target)
        atomic_write(staging / 'manifest.json', json.dumps(
            {'format': 1, 'created_at': stamp(), 'files': files}, indent=2))
        destination = parent / (stamp() + '-' + uuid.uuid4().hex[:8])
        staging.rename(destination)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def restore(source, destination):
    """Restore only into an absent destination, after verifying every file."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    manifest = json.loads((source / 'manifest.json').read_text('utf8'))
    if manifest.get('format') != 1 or not isinstance(
            manifest.get('files'), dict):
        raise StateError('Unsupported backup manifest.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix='.restore-',
            dir=destination.parent))
    try:
        for name, expected in manifest['files'].items():
            relative = _relative(name)
            path, target = source / relative, staging / relative
            if path.is_symlink() or not path.resolve().is_relative_to(
                    source.resolve()):
                raise StateError('Unsafe backup source.')
            if digest(path) != expected:
                raise StateError('Backup checksum mismatch; nothing restored.')
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == '.sqlite':
                sqlite_copy(path, target)
            else:
                shutil.copyfile(path, target)
        if destination.exists():
            raise FileExistsError(destination)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def import_artifact(archive, root):
    """Keep original ZIP, then import only a verified wallet DB and report."""
    from seo.tao_view import load_snapshot
    archive, root = Path(archive), Path(root)
    with lease(root):
        database = root / 'wallets.sqlite'
        report = root / 'reports/tao_collect.json'
        if database.exists() or report.exists():
            raise FileExistsError(
                'Wallet data exists. Import into a new root.')
        saved = root / 'imports' / (stamp() + '-' + uuid.uuid4().hex[:8])
        saved.mkdir(parents=True)
        original = saved / 'original.zip'
        shutil.copyfile(archive, original)
        atomic_write(saved / 'sha256.txt', digest(original) + '\n')
        with zipfile.ZipFile(original) as zf:
            selected = {}
            for info in zf.infolist():
                relative = _relative(info.filename.rstrip('/'))
                if relative.name in ('wallets.sqlite', 'tao_collect.json'):
                    if relative.name in selected or info.is_dir():
                        raise StateError('Artifact contains ambiguous files.')
                    selected[relative.name] = info
            if set(selected) != {'wallets.sqlite', 'tao_collect.json'}:
                raise StateError('Artifact must contain wallet DB and report.')
            for name, info in selected.items():
                with zf.open(info) as src, (saved / name).open('xb') as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
        load_snapshot(saved / 'tao_collect.json')
        sqlite_copy(saved / 'wallets.sqlite', database)
        raw = (saved / 'tao_collect.json').read_bytes()
        atomic_write(root / 'snapshots' / ''.join(
            c for c in stamp() if c.isdigit()) / 'tao_collect.json', raw)
        atomic_write(report, raw)
        return saved
