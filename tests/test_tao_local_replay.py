import json
from pathlib import Path

import pytest

from seo import storage, tao_local_replay
from seo.models import WalletSnapshot
from tests.test_tao_ledger import day, event


def fixture(tmp_path):
    store = storage.Storage(str(tmp_path / 'source.sqlite'))
    store.conn.execute('CREATE TABLE wallet_events(id TEXT PRIMARY KEY, '
                       'payload TEXT NOT NULL)')
    store.conn.execute('CREATE TABLE wallet_inventory(address TEXT, '
                       'time TEXT, batch TEXT, payload TEXT)')
    buy = event(1, 'buy', None, 'A', 100, 10)
    store.conn.execute('INSERT INTO wallet_events VALUES(?,?)',
                       (buy['id'], json.dumps(buy)))
    store.save([WalletSnapshot(address='A', snapshot_at=day(4),
                               positions_complete=True, balance_total=20)],
               stamp=day(4))
    report = dict(checked_at=day(4), coverage={'A': dict(
        account='complete', transactions='complete', transfers='complete')},
        performance=dict(settings=dict(min_sales=2, min_timing=3, move_pct=5,
                                       max_balance_tao=100000,
                                       max_activity_24h=100),
                         wallets=[dict(address='A', positions=[dict(
                             netuid=8, price_tao=.2, quote_at=day(4),
                             current_quote=True)])]))
    store.conn.commit()
    store.close()
    path = tmp_path / 'report.json'
    path.write_text(json.dumps(report))
    return path, tmp_path / 'source.sqlite'


def test_local_replay_keeps_sources_and_snapshot_time(tmp_path):
    report, db = fixture(tmp_path)
    original = db.read_bytes(), report.read_bytes()
    result = tao_local_replay.replay(report, db, tmp_path / 'replay', [], [])
    assert original == (db.read_bytes(), report.read_bytes())
    assert result['checked_at'] == day(4)
    wallet = result['performance']['wallets'][0]
    assert wallet['positions'][0]['known_buy_price_tao'] == .1
    assert wallet['gross_unrealized_tao'] == 10
    assert result['local_evidence']['source_snapshot'] == day(4)
    assert (tmp_path / 'replay/tao_collect.json').is_file()


def test_refuses_output_aliasing_downloaded_database(tmp_path):
    report, db = fixture(tmp_path)
    # Output database is named wallets.sqlite; source path must not alias it.
    target = tmp_path / 'wallets.sqlite'
    db.rename(target)
    with pytest.raises(ValueError):
        tao_local_replay.replay(report, target, tmp_path, [], [])


def test_never_replays_source_events_from_after_snapshot(tmp_path):
    report, db = fixture(tmp_path)
    store = storage.Storage(str(db))
    extra = event(5, 'sell', 'A', None, 50, 100)
    store.conn.execute('INSERT INTO wallet_events VALUES(?,?)',
                       (extra['id'], json.dumps(extra)))
    store.conn.commit()
    store.close()
    result = tao_local_replay.replay(report, db, tmp_path / 'replay', [], [])
    assert result['performance']['wallets'][0]['positions'][0]['alpha'] == 100
    assert len(result['events']) == 1


def test_replay_rejects_github_environment(monkeypatch, tmp_path):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    with pytest.raises(ValueError, match='local'):
        tao_local_replay.replay(Path('absent'), Path('absent'),
                                tmp_path, [], [])


def test_inventory_observed_after_source_run_start_is_preserved(tmp_path):
    report, db = fixture(tmp_path)
    store = storage.Storage(str(db))
    later = '2026-10-04T00:01:00+00:00'
    checkpoint = dict(address='A', batch=day(4), time=later, units={8: 110})
    store.conn.execute('INSERT INTO wallet_inventory VALUES(?,?,?,?)',
                       ('A', later, day(4), json.dumps(checkpoint)))
    store.conn.commit()
    store.close()
    result = tao_local_replay.replay(report, db, tmp_path / 'replay', [], [])
    position = result['performance']['wallets'][0]['positions'][0]
    assert position['alpha'] == 110
    assert position['unknown_alpha'] == 10
    assert position['gross_unrealized_tao'] is None


def test_source_report_event_ids_bound_replay_instead_of_start_clock(tmp_path):
    report, db = fixture(tmp_path)
    store = storage.Storage(str(db))
    source_event = event(4, 'buy', None, 'A', 20, 4)
    source_event['time'] = '2026-10-04T00:01:00+00:00'
    future = event(5, 'buy', None, 'A', 50, 5)
    for e in (source_event, future):
        store.conn.execute('INSERT INTO wallet_events VALUES(?,?)',
                           (e['id'], json.dumps(e)))
    store.conn.commit()
    store.close()
    data = json.loads(report.read_text())
    data['events'] = [event(1, 'buy', None, 'A', 100, 10), source_event]
    report.write_text(json.dumps(data))
    result = tao_local_replay.replay(report, db, tmp_path / 'replay', [], [])
    assert result['performance']['wallets'][0]['positions'][0]['alpha'] == 120
