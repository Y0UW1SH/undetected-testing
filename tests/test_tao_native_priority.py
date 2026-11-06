"""Native ranking is exact, subnet-local, fair and independent of profit."""
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from seo.tao_local_watch import WatchError, _addr
from seo.tao_native_holders import PREFIX, schema


BLOCK = '0x' + 'ab' * 32
STAMP = '2026-11-06T12:00:00+00:00'


@pytest.fixture(autouse=True)
def local_environment(monkeypatch):
    monkeypatch.delenv('CI', raising=False)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    schema(conn)
    conn.execute('INSERT INTO native_holder_scan VALUES '
                 '(1,?,123,NULL,2,0,?)', (BLOCK, STAMP))
    yield conn
    conn.close()


def position(db, hot, cold, netuid, mantissa, exponent=0):
    def part(v):
        raw = bytes([v]) * 32
        return hashlib.blake2b(raw, digest_size=16).hexdigest() + raw.hex()
    key = (PREFIX + part(hot) + part(cold)
           + netuid.to_bytes(2, 'little').hex())
    hotkey, address = _addr(bytes([hot]) * 32), _addr(bytes([cold]) * 32)
    db.execute('INSERT INTO native_holder_positions VALUES '
               '(?,?,?,?,?,?,0,0,?)',
               (key, hotkey, address, netuid, str(mantissa), exponent,
                'candidate'))
    db.execute('INSERT OR IGNORE INTO holder_registry VALUES '
               '(?,?,2147483647,\'unknown\',?,?)',
               (netuid, address, STAMP, STAMP))
    return hotkey, address


def pool(alpha=100000000000, mantissa=100, exponent=0):
    return dict(alpha_units=alpha,
                shares=dict(mantissa=mantissa, exponent=exponent))


class Source:
    def __init__(self, active, pools):
        self.networks, self.pool_values = set(active), pools
        self.queried = None

    def prepare(self, block):
        assert block == BLOCK

    def active(self, block):
        assert block == BLOCK
        return self.networks

    def pools(self, block, pools):
        assert block == BLOCK
        self.queried = set(pools)
        return {key: self.pool_values[key] for key in pools}


def test_exact_floor_aggregates_hotkeys_without_inventing_tao(db):
    from seo import tao_native_priority as priority
    hot, cold = position(db, 1, 2, 128, 333333333333333333333, -20)
    hot2, _ = position(db, 3, 2, 128, 1)
    source = Source([128], {
        (hot, 128): pool(1000000000001, 10),
        (hot2, 128): pool(3, 2),
    })
    result = priority.prioritize(db, source, limit=1, stamp=STAMP)
    row = result['candidates'][0]
    assert row['address'] == cold
    assert row['positions'][0]['observed_alpha_units'] == '333333333334'
    assert row['positions'][0]['observed_alpha'] == '333.333333334'
    assert row['positions'][0]['tao_value'] is None
    assert result['holder_census_complete'] is False
    assert result['alpha_v2_traversal_complete'] is False
    assert 'profit' in result['limitation'].lower()
    assert db.execute('SELECT DISTINCT balance_units FROM holder_registry'
                      ).fetchall() == [('unknown',)]


def test_every_active_subnet_has_coverage_and_amount_beats_address_order(db):
    from seo import tao_native_priority as priority
    hot, small = position(db, 1, 2, 1, 1)
    _, large = position(db, 1, 3, 1, 90)
    _, remote = position(db, 1, 4, 128, 80)
    source = Source([1, 99, 128], {(hot, 1): pool(), (hot, 128): pool()})
    result = priority.prioritize(db, source, limit=2, stamp=STAMP)
    assert result['addresses'] == [large, remote]
    assert small not in result['addresses']
    coverage = {r['netuid']: r for r in result['subnet_coverage']}
    assert set(coverage) == {1, 99, 128}
    assert coverage[99]['observed_positions'] == 0
    assert coverage[99]['eligible_wallets'] == 0
    assert result['active_subnets'] == [1, 99, 128]
    assert result['active_subnets_with_selected_wallets'] == 2


def test_inactive_subnets_and_shared_wallet_deduplication(db):
    from seo import tao_native_priority as priority
    hot, shared = position(db, 1, 2, 1, 90)
    position(db, 1, 2, 2, 90)
    _, other = position(db, 1, 3, 2, 80)
    position(db, 1, 4, 99, 99)
    source = Source([1, 2], {(hot, 1): pool(), (hot, 2): pool()})
    result = priority.prioritize(db, source, limit=2, stamp=STAMP)
    assert result['addresses'] == [shared, other]
    assert source.queried == {(hot, 1), (hot, 2)}
    assert result['inactive_observed_positions'] == 1
    subnets = [p['netuid'] for p in result['candidates'][0]['positions']]
    assert subnets == [1, 2]


def test_history_excludes_inspected_screened_and_retry_without_registry(db):
    from seo import tao_native_priority as priority
    hot, inspected = position(db, 1, 2, 1, 90)
    _, screened = position(db, 1, 3, 1, 80)
    _, waiting = position(db, 1, 4, 1, 70)
    _, untouched = position(db, 1, 5, 1, 60)
    history = sqlite3.connect(':memory:')
    history.execute('CREATE TABLE wallet_performance '
                    '(address TEXT,checked_at TEXT,payload TEXT)')
    for address, extra in (
            (inspected, {'coverage': {
                'account': 'complete', 'transactions': 'complete'}}),
            (screened, {'screens': ['known_exchange']})):
        history.execute('INSERT INTO wallet_performance VALUES (?,?,?)',
                        (address, STAMP, json.dumps(
                            dict(address=address, **extra))))
    history.execute('CREATE TABLE holder_research_attempts '
                    '(address TEXT,checked_at TEXT,status TEXT)')
    history.execute('INSERT INTO holder_research_attempts VALUES (?,?,?)',
                    (waiting, STAMP, 'unavailable'))
    result = priority.prioritize(
        db, Source([1], {(hot, 1): pool()}), history_dbs=[history],
        limit=4, stamp=STAMP)
    assert result['addresses'] == [untouched]
    assert result['subnet_coverage'][0]['inspected_wallets'] == 1
    assert result['subnet_coverage'][0]['screened_wallets'] == 1
    assert result['subnet_coverage'][0]['retry_wait_wallets'] == 1
    history.close()


def test_later_runs_favor_subnets_not_yet_served_and_defer_failed_retries(db):
    from seo import tao_native_priority as priority
    hot, tried = position(db, 1, 2, 1, 99)
    _, untouched = position(db, 1, 3, 1, 50)
    _, distant = position(db, 1, 4, 128, 1)
    db.execute('CREATE TABLE holder_research_attempts '
               '(address TEXT,checked_at TEXT,status TEXT)')
    db.execute('INSERT INTO holder_research_attempts VALUES (?,?,?)',
               (tried, '2026-11-01T00:00:00Z', 'unavailable'))
    source = Source([1, 128], {(hot, 1): pool(), (hot, 128): pool()})
    result = priority.prioritize(db, source, limit=2, stamp=STAMP)
    assert result['addresses'] == [distant, untouched]


def test_zero_valued_shares_and_explicit_exclusions_are_not_selected(db):
    from seo import tao_native_priority as priority
    hot, dust = position(db, 1, 2, 1, 1)
    _, excluded = position(db, 1, 3, 1, 99)
    result = priority.prioritize(
        db, Source([1], {(hot, 1): pool(1)}), limit=10,
        exclude_addresses=[excluded], stamp=STAMP)
    assert result['addresses'] == []
    assert result['subnet_coverage'][0]['zero_valued_wallets'] == 2
    assert dust not in result['addresses']


@pytest.mark.parametrize('bad', [
    pool(mantissa=-1), pool(alpha=-1), pool(exponent=1000000)])
def test_invalid_pool_never_creates_a_rank_or_public_output(db, bad):
    from seo import tao_native_priority as priority
    hot, _ = position(db, 1, 2, 1, 50)
    with pytest.raises(WatchError):
        priority.prioritize(db, Source([1], {(hot, 1): bad}), stamp=STAMP)


def test_ci_guard_precedes_endpoint_or_database_access(tmp_path, monkeypatch):
    from seo import tao_native_priority as priority
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setattr(priority, '_file_endpoint',
                        lambda _: pytest.fail('private endpoint read'))
    out = tmp_path / 'candidates.json'
    assert priority.main(['--db', str(tmp_path / 'absent'),
                          '--out', str(out)]) == 2
    assert not out.exists()


def test_connection_failure_keeps_output_and_never_prints_secret(
        tmp_path, monkeypatch, capsys):
    from seo import tao_native_priority as priority
    endpoint_files = []

    def endpoint(path):
        endpoint_files.append(path.name)
        return 'private'

    def connect(*args):
        raise RuntimeError('wss://private/DO-NOT-PRINT')

    monkeypatch.setattr(priority, '_file_endpoint', endpoint)
    monkeypatch.setattr(priority, '_connect', connect)
    out = tmp_path / 'candidates.json'
    out.write_text('keep')
    assert priority.main(['--db', str(tmp_path / 'absent'),
                          '--out', str(out)]) == 2
    assert out.read_text() == 'keep'
    assert endpoint_files == ['dwellir.txt']
    captured = capsys.readouterr()
    assert 'DO-NOT-PRINT' not in captured.out + captured.err


def test_closed_pool_without_denominator_has_no_spendable_alpha(db):
    from seo import tao_native_priority as priority
    hot, _ = position(db, 1, 2, 1, 50)
    result = priority.prioritize(
        db, Source([1], {(hot, 1): pool(alpha=100, mantissa=0)}), stamp=STAMP)
    assert result['addresses'] == []
    assert result['subnet_coverage'][0]['zero_valued_wallets'] == 1


class Chain:
    prefixes = {
        'NetworksAdded': ('0x658faa385070e074c85bf6b568cf0555'
                          '0e30450fc4d507a846032a7fa65d9a43'),
        'TotalHotkeyAlpha': ('0x658faa385070e074c85bf6b568cf0555'
                             'ee25c3b5b1886863480497907f1829e6'),
        'TotalHotkeySharesV2': ('0x658faa385070e074c85bf6b568cf0555'
                                'e47c637aa82c21537eb9caa511e543c5')}

    def __init__(self):
        self.batch_sizes = []
        self.bad_response = None
        self.page_calls = 0
        self.metadata = SimpleNamespace(get_metadata_pallet=lambda _: (
            SimpleNamespace(value={'storage': {'prefix': 'SubtensorModule'}})))

    def create_storage_key(self, pallet, name, params=None):
        raise AssertionError('create_storage_key implicitly queries head')

    def get_metadata_storage_function(self, pallet, name, block_hash):
        assert block_hash == BLOCK
        types = ['u16'] if name == 'NetworksAdded' else ['AccountId', 'u16']
        hashers = (['Identity'] if name == 'NetworksAdded'
                   else ['Blake2_128Concat', 'Identity'])
        return SimpleNamespace(value={'name': name, 'default': b'\x00'},
                               value_object={'default': SimpleNamespace(
                                   value_object=bytearray(b'\x00'))},
                               get_param_info=lambda: types,
                               get_param_hashers=lambda: hashers,
                               get_value_type_string=lambda: name)

    def rpc_request(self, method, params):
        assert params[-1] == BLOCK
        if method == 'state_getKeysPaged':
            self.page_calls += 1
            assert params[:2] == [self.prefixes['NetworksAdded'], 512]
            keys = sorted(self.prefixes['NetworksAdded']
                          + n.to_bytes(2, 'little').hex()
                          for n in (0, 99, 258))
            return {'result': keys if params[2] is None else []}
        assert method == 'state_queryStorageAt'
        keys, block = params
        self.batch_sizes.append(len(keys))
        changes = [[k, '0x01'] for k in keys]
        if self.bad_response == 'missing':
            changes.pop()
        elif self.bad_response == 'duplicate':
            changes.append(changes[0])
        elif self.bad_response == 'wrong_block':
            block = '0x' + 'ef' * 32
        return {'result': [{'block': block, 'changes': changes}]}

    def decode_scale(self, value_type, raw, block_hash):
        assert block_hash == BLOCK and raw in ('0x00', '0x01')
        if raw == '0x00':
            return (dict(mantissa=0, exponent=0)
                    if value_type.endswith('V2') else
                    False if value_type == 'NetworksAdded' else 0)
        if value_type == 'NetworksAdded':
            return True
        if value_type == 'TotalHotkeyAlpha':
            return 1000
        assert value_type == 'TotalHotkeySharesV2'
        return dict(mantissa=100, exponent=0)


def test_active_subnet_raw_keys_keep_full_u16_and_wait_for_empty_page():
    from seo import tao_native_priority as priority
    chain = Chain()
    source = priority.PoolSource(chain)
    source.prepare(BLOCK)
    assert source.active(BLOCK) == {0, 99, 258}
    assert chain.page_calls == 2


def test_pool_queries_batch_distinct_pools_and_stay_at_pinned_block():
    from seo import tao_native_priority as priority
    chain = Chain()
    source = priority.PoolSource(chain)
    source.prepare(BLOCK)
    wanted = {}
    for i in range(257):
        account = i.to_bytes(32, 'little')
        wanted['0x' + account.hex(), 128] = (
            hashlib.blake2b(account, digest_size=16).hexdigest()
            + account.hex() + '8000')
    values = source.pools(BLOCK, wanted)
    assert set(values) == set(wanted)
    assert chain.batch_sizes == [512, 2]
    assert all(v == pool(1000) for v in values.values())


@pytest.mark.parametrize('kind', ['missing', 'duplicate', 'wrong_block'])
def test_incomplete_or_wrong_block_pool_response_is_rejected(kind):
    from seo import tao_native_priority as priority
    chain = Chain()
    chain.bad_response = kind
    source = priority.PoolSource(chain)
    source.prepare(BLOCK)
    with pytest.raises(ValueError):
        source.pools(BLOCK, {('0x' + '01' * 32, 1): '01' * 48 + '0100'})
