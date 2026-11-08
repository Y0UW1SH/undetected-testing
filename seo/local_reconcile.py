"""Preserve divergent wallet artifacts and compare them without merging."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile

from seo import local_state as state


def preserve_artifact(archive, destination):
    """Keep originals and validate structure, not coverage claims."""
    from seo.tao_view import load_snapshot
    archive, destination = Path(archive), Path(destination)
    destination.mkdir(parents=True)
    original = destination / 'original.zip'
    shutil.copyfile(archive, original)
    checksum = state.digest(original)
    if checksum != state.digest(archive):
        raise state.StateError('Source archive changed while being copied.')
    state.atomic_write(destination / 'sha256.txt', checksum + '\n')
    with zipfile.ZipFile(original) as zf:
        selected = {}
        for info in zf.infolist():
            relative = state._relative(info.filename.rstrip('/'))
            if relative.name in ('wallets.sqlite-wal',
                                 'wallets.sqlite-journal'):
                if info.file_size:
                    raise state.StateError(
                        'Archive has a WAL/journal sidecar. '
                        'Export a closed SQLite backup.')
            if relative.name in ('wallets.sqlite', 'tao_collect.json'):
                if relative.name in selected or info.is_dir():
                    raise state.StateError(
                        'Artifact contains ambiguous files.')
                selected[relative.name] = info
        if set(selected) != {'wallets.sqlite', 'tao_collect.json'}:
            raise state.StateError(
                'Artifact must contain wallet DB and report.')
        for name, info in selected.items():
            with zf.open(info) as src, (destination / name).open('xb') as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
    report = load_snapshot(destination / 'tao_collect.json')
    db_path = destination / 'wallets.sqlite'
    with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro',
                                 uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise state.StateError('Artifact failed SQLite quick_check.')
        table = db.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                           "AND name='wallet_events'").fetchone()
        columns = {row[1] for row in db.execute(
            'PRAGMA table_info(wallet_events)')}
        if (not table or not table[0].upper().startswith('CREATE TABLE')
                or not {'id', 'payload'} <= columns):
            raise state.StateError('Artifact needs an ordinary wallet_events '
                                   'table with id and payload columns.')
    source = dict(source_file=archive.name, archive_sha256=checksum,
                  database_sha256=state.digest(db_path),
                  report_sha256=state.digest(destination / 'tao_collect.json'),
                  report_checked_at=report['checked_at'],
                  coverage_verified=False)
    state.atomic_write(
        destination
        / 'source.json',
        json.dumps(
            source,
            indent=2))
    return source


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _schema(db, side):
    return dict(db.execute(
        f'SELECT name,sql FROM {side}.sqlite_master '
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"))


def _equal(columns, left='a', right='b'):
    return ' AND '.join(f'{left}.{_quote(c)} COLLATE BINARY IS '
                        f'{right}.{_quote(c)} COLLATE BINARY' for c in columns)


def _missing_rows(db, table, columns, left, right):
    """Count full-row differences, including duplicate and NULL rows."""
    cols = ','.join(_quote(c) + ' COLLATE BINARY' for c in columns)
    count = '__local_compare_count'
    while count in columns:
        count += '_'
    n = _quote(count)
    sql = (f'WITH a AS (SELECT {cols},count(*) AS {n} FROM '
           f'{left}.{_quote(table)} GROUP BY {cols}), '
           f'b AS (SELECT {cols},count(*) AS {n} FROM '
           f'{right}.{_quote(table)} GROUP BY {cols}) '
           f'SELECT COALESCE(sum(max(a.{n}-coalesce(b.{n},0),0)),0) '
           f'FROM a LEFT JOIN b ON {_equal(columns)}')
    return db.execute(sql).fetchone()[0]


def compare_databases(left, right):
    """All ordinary tables, read-only. Declared keys are not semantic IDs."""
    with closing(sqlite3.connect(':memory:', uri=True)) as db:
        for side, path in (('lhs', left), ('rhs', right)):
            db.execute(f'ATTACH DATABASE ? AS {side}',
                       (Path(path).resolve().as_uri() + '?mode=ro',))
        db.execute('PRAGMA temp_store=FILE')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        schemas = {side: _schema(db, side) for side in ('lhs', 'rhs')}
        tables = {}
        for name in sorted(set(schemas['lhs']) | set(schemas['rhs'])):
            table = _quote(name)
            item = {key + '_rows': db.execute(
                f'SELECT count(*) FROM {side}.{table}').fetchone()[0]
                if name in schemas[side] else 0
                for key, side in (('left', 'lhs'), ('right', 'rhs'))}
            tables[name] = item
            if name not in schemas['lhs'] or name not in schemas['rhs']:
                item['status'] = ('right_only_table' if name not in
                                  schemas['lhs'] else 'left_only_table')
                continue
            if schemas['lhs'][name] != schemas['rhs'][name]:
                item['status'] = 'schema_mismatch'
                continue
            info = list(db.execute(f'PRAGMA lhs.table_info({table})'))
            columns = [row[1] for row in info]
            keys = [row[1] for row in sorted(info, key=lambda row: row[5])
                    if row[5]]
            item.update(status='compared', primary_key=keys,
                        key_may_be_local_sequence=(len(keys) == 1 and any(
                            row[1] == keys[0] and row[2].upper() == 'INTEGER'
                            for row in info)))
            item['left_only_rows'] = _missing_rows(
                db, name, columns, 'lhs', 'rhs')
            item['right_only_rows'] = _missing_rows(
                db, name, columns, 'rhs', 'lhs')
            if not keys:
                continue
            nulls = ' OR '.join(_quote(key) + ' IS NULL' for key in keys)
            if any(db.execute(f'SELECT 1 FROM {side}.{table} WHERE {nulls} '
                              'LIMIT 1').fetchone()
                   for side in ('lhs', 'rhs')):
                item['key_comparison'] = 'skipped_nullable_keys'
                continue
            for label, a, b in (('left', 'lhs', 'rhs'),
                                ('right', 'rhs', 'lhs')):
                item[label + '_only_keys'] = db.execute(
                    f'SELECT count(*) FROM {a}.{table} AS a WHERE NOT EXISTS '
                    f'(SELECT 1 FROM {b}.{table} AS b WHERE {_equal(keys)})'
                ).fetchone()[0]
            item['changed_keys'] = db.execute(
                f'SELECT count(*) FROM lhs.{table} AS a JOIN rhs.{table} AS b '
                f'ON {_equal(keys)} WHERE NOT ({_equal(columns)})'
            ).fetchone()[0]
        return tables


def preserve_and_compare(left, right, destination):
    """Publish a complete two-source bundle into a new directory only."""
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError('Comparison destination already exists.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix='.compare-',
            dir=destination.parent))
    try:
        sources = [preserve_artifact(source, staging / ('source-%d' % number))
                   for number, source in enumerate((left, right), 1)]
        tables = compare_databases(staging / 'source-1/wallets.sqlite',
                                   staging / 'source-2/wallets.sqlite')
        same = all(item['status'] == 'compared'
                   and item['left_only_rows'] == item['right_only_rows'] == 0
                   for item in tables.values())
        result = dict(format=1, merged=False, active_source=None,
                      coverage_complete=None, sources=sources, tables=tables,
                      comparison_scope='ordinary tables; no SQLite internals',
                      databases_have_same_rows=same,
                      note='Differences do not select a winning source. '
                      'Declared primary keys may be local sequence numbers. '
                      'Matching rows do not establish complete coverage. '
                      'Original reports and coverage claims are unchanged.')
        state.atomic_write(staging / 'comparison.json',
                           json.dumps(result, indent=2))
        files = {path.relative_to(staging).as_posix(): state.digest(path)
                 for path in sorted(staging.rglob('*')) if path.is_file()}
        state.atomic_write(staging / 'manifest.json', json.dumps(
            dict(format=1, merged=False, files=files), indent=2))
        if destination.exists():
            raise FileExistsError('Comparison destination already exists.')
        staging.rename(destination)
        return result
    finally:
        if staging.exists():
            shutil.rmtree(staging)
