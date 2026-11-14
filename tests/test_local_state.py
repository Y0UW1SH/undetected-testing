"""Local data preservation and recovery, without network or browser access."""
import json
import sqlite3
import zipfile

import pytest

from seo import local_state as state


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE sample (value TEXT)')
    db.execute("INSERT INTO sample VALUES ('keep me')")
    db.commit()
    return db


def test_backup_reads_wal_and_never_overwrites(tmp_path):
    source = tmp_path / 'src.sqlite'
    with database(source) as db:
        dest = tmp_path / 'copy.sqlite'
        state.sqlite_copy(source, dest)
        with sqlite3.connect(dest) as backup:
            assert backup.execute('SELECT * FROM sample').fetchall() == [
                ('keep me',)]
        with pytest.raises(FileExistsError):
            state.sqlite_copy(source, dest)
        assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)


def test_lock_rejects_overlap_and_releases_after_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with state.lease(tmp_path):
            with pytest.raises(state.StateError, match='running'):
                with state.lease(tmp_path):
                    pytest.fail('second writer entered')
            raise RuntimeError('interrupted')
    with state.lease(tmp_path):
        pass


def test_backup_restore_verifies_hashes_and_excludes_secrets(tmp_path):
    root = tmp_path / 'root'
    db = database(root / 'wallets.sqlite')
    db.close()
    (root / 'dwellir.txt').write_text('never copy this')
    (root / 'reports').mkdir()
    (root / 'reports' / 'tao_collect.json').write_text('{}')
    backup = state.backup(root)
    assert not (backup / 'dwellir.txt').exists()
    restored = tmp_path / 'restored'
    state.restore(backup, restored)
    assert (restored / 'wallets.sqlite').exists()
    with pytest.raises(FileExistsError):
        state.restore(backup, restored)
    (backup / 'reports' / 'tao_collect.json').write_text('changed')
    other = tmp_path / 'other'
    with pytest.raises(state.StateError, match='checksum'):
        state.restore(backup, other)
    assert not other.exists()


def test_restore_rejects_manifest_traversal(tmp_path):
    src = tmp_path / 'backup'
    src.mkdir()
    (src / 'manifest.json').write_text(json.dumps({
        'format': 1, 'files': {'../elsewhere': 'abc'}}))
    with pytest.raises(state.StateError):
        state.restore(src, tmp_path / 'destination')


def test_artifact_import_preserves_original_and_refuses_existing_data(
        tmp_path):
    source = tmp_path / 'source.sqlite'
    with database(source) as db:
        db.execute('CREATE TABLE wallet_events '
                   '(id TEXT PRIMARY KEY, payload TEXT)')
    db.close()
    report = {'checked_at': '2026-11-07T10:39:51+00:00',
              'performance': {'wallets': []}}
    archive = tmp_path / 'artifact.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.write(source, 'tao-wallet-state/wallets.sqlite')
        zf.writestr('tao-wallet-report/tao_collect.json', json.dumps(report))
    original = archive.read_bytes()
    root = tmp_path / 'new root'
    state.import_artifact(archive, root)
    assert (root / 'wallets.sqlite').is_file()
    assert json.loads(
        (root / 'reports/tao_collect.json').read_text()) == report
    assert next((root / 'imports').glob('*/original.zip')
                ).read_bytes() == original
    with pytest.raises(FileExistsError):
        state.import_artifact(archive, root)
    assert archive.read_bytes() == original


def test_artifact_rejects_ambiguous_wallets(tmp_path):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('one/wallets.sqlite', 'x')
        zf.writestr('two/wallets.sqlite', 'y')
    with pytest.raises(state.StateError):
        state.import_artifact(archive, tmp_path / 'root')
    assert not (tmp_path / 'root/wallets.sqlite').exists()


def test_default_state_is_outside_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv('TAO_LOCAL_HOME', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert state.default_root() == (
        tmp_path / 'StealthWalletMonitor/LocalRunner')
    monkeypatch.setenv('TAO_LOCAL_HOME', str(tmp_path / 'custom'))
    assert state.default_root() == tmp_path / 'custom'


def test_failed_copy_does_not_leave_partial_database(tmp_path, monkeypatch):
    src, dst = tmp_path / 'src.sqlite', tmp_path / 'dst.sqlite'
    database(src).close()

    def fail_publish(source, destination):
        raise OSError('disk full')

    function = 'rename' if state.os.name == 'nt' else 'link'
    monkeypatch.setattr(state.os, function, fail_publish)
    with pytest.raises(OSError):
        state.sqlite_copy(src, dst)
    assert not dst.exists()


def test_backup_restore_keeps_local_snapshot_history(tmp_path):
    root = tmp_path / 'root'
    report = root / 'snapshots/20261108000000/tao_collect.json'
    report.parent.mkdir(parents=True)
    report.write_text('{"mode": "discovery"}')
    saved = state.backup(root)
    restored = tmp_path / 'restored'
    state.restore(saved, restored)
    assert (restored / report.relative_to(root)).read_bytes() == (
        report.read_bytes())


def test_sqlite_copy_publishes_only_a_complete_verified_file(
        tmp_path, monkeypatch):
    src, dst = tmp_path / 'src.sqlite', tmp_path / 'dst.sqlite'
    database(src).close()
    linked = []
    original_link = state.os.link

    def link_complete(temporary, destination):
        assert not destination.exists()
        with sqlite3.connect(temporary) as db:
            assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
        linked.append(destination)
        return original_link(temporary, destination)

    monkeypatch.setattr(state.os, 'link', link_complete)
    state.sqlite_copy(src, dst)
    if state.os.name != 'nt':
        assert linked == [dst]


def test_failed_import_rolls_back_new_live_files_and_allows_retry(
        tmp_path, monkeypatch):
    from tests.test_local_reconcile import artifact
    archive = artifact(tmp_path, 'source', [('a', '{}')])
    root = tmp_path / 'root'
    original_write = state.atomic_write

    def fail_report(path, data):
        if path == root / 'reports/tao_collect.json':
            raise OSError('disk full')
        return original_write(path, data)

    monkeypatch.setattr(state, 'atomic_write', fail_report)
    with pytest.raises(OSError):
        state.import_artifact(archive, root)
    assert not (root / 'wallets.sqlite').exists()
    assert not (root / 'reports/tao_collect.json').exists()
    assert next((root / 'imports').glob('*/original.zip')).is_file()
    monkeypatch.setattr(state, 'atomic_write', original_write)
    state.import_artifact(archive, root)
    assert (root / 'wallets.sqlite').is_file()


def test_backup_does_not_follow_allowlisted_directory_symlinks(tmp_path):
    root, outside = tmp_path / 'root', tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    (outside / 'secrets.json').write_text('{"private": true}')
    try:
        (root / 'config').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('directory symlinks unavailable on this system')
    saved = state.backup(root)
    assert not (saved / 'config/secrets.json').exists()
