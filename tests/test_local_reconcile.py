"""Compare divergent artifacts without inventing merged history."""
import json
import sqlite3
import zipfile

import pytest

from seo import local_reconcile as reconcile
from seo import local_state as state


def artifact(tmp_path, name, events, *, report=None, extra_sql=()):
    db_path = tmp_path / (name + '.sqlite')
    with sqlite3.connect(db_path) as db:
        db.execute(
            'CREATE TABLE wallet_events (id TEXT PRIMARY KEY, payload TEXT)')
        db.executemany('INSERT INTO wallet_events VALUES (?,?)', events)
        for sql in extra_sql:
            db.execute(sql)
    report = report or {'checked_at': '2026-11-08T00:00:00+00:00',
                        'performance': {'wallets': []},
                        'coverage': {'example': {'transactions': 'partial'}}}
    path = tmp_path / (name + '.zip')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.write(db_path, 'state/wallets.sqlite')
        archive.writestr('report/tao_collect.json', json.dumps(report))
    return path


def test_preserves_divergent_events_and_payload_versions(tmp_path):
    left = artifact(tmp_path, 'left', [('old', 'a'), ('same', 'old payload')])
    right = artifact(
        tmp_path, 'right', [
            ('new', 'b'), ('same', 'new payload')])
    originals = [state.digest(left), state.digest(right)]
    dest = tmp_path / 'comparison'
    result = reconcile.preserve_and_compare(left, right, dest)
    events = result['tables']['wallet_events']
    assert events['left_only_keys'] == 1
    assert events['right_only_keys'] == 1
    assert events['changed_keys'] == 1
    assert events['left_only_rows'] == 2
    assert result['merged'] is False and result['active_source'] is None
    assert result['databases_have_same_rows'] is False
    for number, archive in enumerate((left, right), 1):
        preserved = dest / ('source-%d' % number)
        assert state.digest(
            preserved / 'original.zip') == originals[number - 1]
        with sqlite3.connect(preserved / 'wallets.sqlite') as db:
            assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert json.loads((preserved / 'tao_collect.json').read_text())[
            'coverage']['example']['transactions'] == 'partial'
        assert state.digest(archive) == originals[number - 1]
    assert not (dest / 'wallets.sqlite').exists()
    with pytest.raises(FileExistsError):
        reconcile.preserve_and_compare(left, right, dest)


def test_reports_missing_schema_duplicate_rows_and_sequence_key_conflicts(
        tmp_path):
    common = ["CREATE TABLE bags (value TEXT)",
              "INSERT INTO bags VALUES ('same'), ('same')",
              'CREATE TABLE sequence (id INTEGER PRIMARY KEY, value TEXT)',
              "INSERT INTO sequence VALUES (1,'old')"]
    left = artifact(tmp_path, 'left', [], extra_sql=common)
    right = artifact(tmp_path, 'right', [], extra_sql=[
        *common, "DELETE FROM bags WHERE rowid=1",
        "UPDATE sequence SET value='new' WHERE id=1",
        'CREATE TABLE extra (value TEXT)'])
    result = reconcile.preserve_and_compare(left, right, tmp_path / 'out')
    assert result['tables']['bags']['left_only_rows'] == 1
    assert result['tables']['bags']['right_only_rows'] == 0
    assert result['tables']['bags']['primary_key'] == []
    assert result['tables']['sequence']['changed_keys'] == 1
    assert result['tables']['sequence']['key_may_be_local_sequence'] is True
    assert result['tables']['extra']['status'] == 'right_only_table'


def test_schema_difference_never_reports_containment(tmp_path):
    left = artifact(tmp_path, 'left', [], extra_sql=[
        'CREATE TABLE changed (id TEXT PRIMARY KEY)'])
    right = artifact(tmp_path, 'right', [], extra_sql=[
        'CREATE TABLE changed (id TEXT PRIMARY KEY, payload TEXT)'])
    result = reconcile.preserve_and_compare(left, right, tmp_path / 'out')
    assert result['tables']['changed']['status'] == 'schema_mismatch'
    assert result['databases_have_same_rows'] is False


def test_second_bad_artifact_never_publishes_half_bundle(tmp_path):
    left = artifact(tmp_path, 'left', [('a', '{}')])
    right = tmp_path / 'broken.zip'
    right.write_bytes(b'not a ZIP')
    dest = tmp_path / 'out'
    with pytest.raises((state.StateError, zipfile.BadZipFile)):
        reconcile.preserve_and_compare(left, right, dest)
    assert not dest.exists()
    assert left.is_file() and right.read_bytes() == b'not a ZIP'


def test_manifest_hashes_cover_every_preserved_file(tmp_path):
    a = artifact(tmp_path, 'one', [('a', '{}')])
    dest = tmp_path / 'out'
    reconcile.preserve_and_compare(a, a, dest)
    manifest = json.loads((dest / 'manifest.json').read_text())
    assert manifest['merged'] is False
    assert 'comparison.json' in manifest['files']
    for name, expected in manifest['files'].items():
        assert state.digest(dest / name) == expected
    report = json.loads((dest / 'comparison.json').read_text())
    assert report['databases_have_same_rows'] is True
    assert report['coverage_complete'] is None


def test_live_sqlite_wal_sidecar_artifact_is_rejected(tmp_path):
    path = artifact(tmp_path, 'one', [('a', '{}')])
    with zipfile.ZipFile(path, 'a') as zf:
        zf.writestr('state/wallets.sqlite-wal', b'uncheckpointed changes')
    with pytest.raises(state.StateError, match='WAL'):
        reconcile.preserve_and_compare(path, path, tmp_path / 'out')


def test_unrelated_sqlite_is_not_accepted_as_wallet_artifact(tmp_path):
    db_path = tmp_path / 'unrelated.sqlite'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE unrelated (value TEXT)')
    archive = tmp_path / 'unrelated.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.write(db_path, 'wallets.sqlite')
        zf.writestr('tao_collect.json', json.dumps({
            'checked_at': '2026-11-08T00:00:00+00:00',
            'performance': {'wallets': []}}))
    with pytest.raises(state.StateError, match='wallet_events'):
        reconcile.preserve_and_compare(archive, archive, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


def test_null_duplicates_nullable_keys_and_nocase_values(tmp_path):
    common = ['CREATE TABLE nullable (a TEXT, b TEXT, PRIMARY KEY(a,b))',
              "INSERT INTO nullable VALUES (NULL,'x'), (NULL,'x')",
              'CREATE TABLE folded (value TEXT COLLATE NOCASE)',
              "INSERT INTO folded VALUES ('Alpha')"]
    left = artifact(tmp_path, 'left', [], extra_sql=common)
    right = artifact(tmp_path, 'right', [], extra_sql=[
        *common, 'DELETE FROM nullable WHERE rowid=1',
        "UPDATE folded SET value='alpha'"])
    report = reconcile.preserve_and_compare(left, right, tmp_path / 'out')
    nullable = report['tables']['nullable']
    assert nullable['key_comparison'] == 'skipped_nullable_keys'
    assert nullable['left_only_rows'] == 1
    assert report['tables']['folded']['left_only_rows'] == 1
    assert report['tables']['folded']['right_only_rows'] == 1
