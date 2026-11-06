"""Pinned share discovery must not invent balances or skip failed pages."""
import hashlib
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from seo.tao_holder_queue import select
from seo.tao_local_watch import WatchError, _addr


@pytest.fixture(autouse=True)
def isolated_local_environment(monkeypatch):
    # These tests use fake sources; guard tests set CI flags explicitly.
    monkeypatch.delenv('CI', raising=False)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)


PREFIX = '0x658faa385070e074c85bf6b568cf0555421d8174603a6a6036511a7619b402f0'
BLOCK = '0x' + 'ab' * 32
STAMP = '2026-11-06T12:00:00+00:00'


def key(hot, cold, netuid):
    def part(value):
        raw = bytes([value]) * 32
        return hashlib.blake2b(raw, digest_size=16).hexdigest() + raw.hex()
    return PREFIX + part(hot) + part(cold) + netuid.to_bytes(2, 'little').hex()


class Source:
    """Replace external RPC while keeping real SQLite persistence."""
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.fail_values = False

    def finalized(self):
        self.calls.append('finalized')
        return BLOCK, 123

    def prepare(self, block):
        assert block == BLOCK

    def page(self, block, after, limit):
        assert block == BLOCK
        self.calls.append(('page', after))
        return [k for k in sorted(self.rows)
                if after is None or k > after][:limit]

    def values(self, block, keys):
        assert block == BLOCK
        if self.fail_values:
            raise RuntimeError('wss://private/DO-NOT-PRINT')
        return {k: self.rows[k] for k in keys}


def row(mantissa=10, exponent=-4, pool=0, epoch=0):
    return {'shares': {'mantissa': mantissa, 'exponent': exponent},
            'pool_epoch': pool, 'row_epoch': epoch}


def test_pinned_pagination_resume_and_unranked_queue_without_alpha_values():
    from seo import tao_native_holders as native
    db = sqlite3.connect(':memory:')
    source = Source({key(1, 2, 28): row(), key(3, 2, 28): row(),
                     key(1, 4, 65535): row()})
    first = native.scan(db, source, page_size=2, max_pages=1, stamp=STAMP)
    assert first['complete'] is False and first['storage_rows'] == 2
    second = native.scan(db, source, page_size=2, max_pages=2, stamp=STAMP)
    assert second['complete'] is True and second['storage_rows'] == 3
    assert second['candidate_positions'] == 3
    assert second['candidate_wallets'] == 2
    assert second['full_holder_census'] is False
    assert source.calls.count('finalized') == 1
    candidates = db.execute('SELECT netuid,address,balance_units FROM '
                            'holder_registry ORDER BY netuid').fetchall()
    assert candidates == [(28, _addr(bytes([2]) * 32), 'unknown'),
                          (65535, _addr(bytes([4]) * 32), 'unknown')]
    assert set(select(db, [28, 65535], 0, STAMP)) == {r[1] for r in candidates}
    calls = len(source.calls)
    assert native.scan(db, source, 2, 1, STAMP)['complete'] is True
    assert len(source.calls) == calls


def test_zero_retired_and_pool_zero_special_case():
    from seo import tao_native_holders as native
    db = sqlite3.connect(':memory:')
    source = Source({key(1, 2, 1): row(mantissa=0),
                     key(1, 3, 1): row(pool=2, epoch=1),
                     key(1, 4, 1): row(pool=2, epoch=2),
                     key(1, 5, 1): row(pool=0, epoch=9)})
    result = native.scan(db, source, 10, 2, STAMP)
    assert result['storage_rows'] == 4
    assert result['candidate_positions'] == 2
    assert result['retired_positions'] == 1
    assert result['zero_share_positions'] == 1
    assert set(select(db, [1], 0, STAMP)) == {
        _addr(bytes([4]) * 32), _addr(bytes([5]) * 32)}


def test_failed_page_keeps_previous_cursor_and_retries_exact_page():
    from seo import tao_native_holders as native
    db = sqlite3.connect(':memory:')
    source = Source({key(1, 2, 28): row(), key(3, 4, 28): row()})
    native.scan(db, source, 1, 1, STAMP)
    source.fail_values = True
    with pytest.raises(WatchError) as error:
        native.scan(db, source, 1, 1, STAMP)
    assert 'DO-NOT-PRINT' not in str(error.value)
    count = db.execute('SELECT COUNT(*) FROM native_holder_positions')
    assert count.fetchone()[0] == 1
    source.fail_values = False
    result = native.scan(db, source, 1, 2, STAMP)
    assert result['complete'] and result['storage_rows'] == 2
    assert source.calls[-3][1] == source.calls[-2][1]


@pytest.mark.parametrize('change', ['short', 'long', 'bad_hash', 'prefix'])
def test_raw_nmap_key_is_strictly_validated(change):
    from seo import tao_native_holders as native
    valid = key(1, 2, 258)
    malformed = {'short': valid[:-2], 'long': valid + '00',
                 'bad_hash': (valid[:len(PREFIX)] + '00' * 16
                              + valid[len(PREFIX) + 32:]),
                 'prefix': '0x' + '00' * 32 + valid[len(PREFIX):]}[change]
    with pytest.raises(ValueError):
        native.decode_key(malformed)
    assert native.decode_key(valid) == (
        _addr(bytes([1]) * 32), _addr(bytes([2]) * 32), 258)


@pytest.mark.parametrize('bad', ['duplicate', 'unordered',
                                 'wrong_prefix', 'too_many'])
def test_malformed_pages_never_advance_or_insert(bad):
    from seo import tao_native_holders as native
    db = sqlite3.connect(':memory:')
    source = Source({key(1, 2, 28): row(), key(3, 4, 28): row()})
    keys = sorted(source.rows)
    invalid = {'duplicate': [keys[0], keys[0]], 'unordered': keys[::-1],
               'wrong_prefix': ['0x00'],
               'too_many': keys + [key(2, 5, 28)]}[bad]
    source.page = lambda *args: invalid
    with pytest.raises(WatchError):
        native.scan(db, source, 2, 1, STAMP)
    saved = db.execute('SELECT cursor,pages FROM native_holder_scan')
    assert saved.fetchone() == (None, 0)
    count = db.execute('SELECT COUNT(*) FROM holder_registry')
    assert count.fetchone()[0] == 0


@pytest.mark.parametrize('value', [
    row(mantissa=-1), row(mantissa=True), row(exponent='oops'), row(pool=-1)])
def test_invalid_decoded_share_or_epoch_rejects_whole_page(value):
    from seo import tao_native_holders as native
    db = sqlite3.connect(':memory:')
    source = Source({key(1, 2, 28): value})
    with pytest.raises(WatchError):
        native.scan(db, source, 2, 1, STAMP)
    count = db.execute('SELECT COUNT(*) FROM native_holder_positions')
    assert count.fetchone()[0] == 0


def test_candidate_discovery_keeps_existing_measured_registry_balance():
    from seo import tao_native_holders as native
    from seo.sources.tao_holder_registry import schema
    db = sqlite3.connect(':memory:')
    schema(db)
    address = _addr(bytes([2]) * 32)
    db.execute('INSERT INTO holder_registry VALUES (28,?,3,?,?,?)',
               (address, '56789', STAMP, STAMP))
    native.scan(db, Source({key(1, 2, 28): row()}), 2, 2, STAMP)
    saved = db.execute('SELECT rank,balance_units FROM holder_registry')
    assert saved.fetchone() == (3, '56789')


def test_database_failure_rolls_back_candidates_and_cursor():
    from seo import tao_native_holders as native
    db = sqlite3.connect(':memory:')
    source = Source({key(1, 2, 28): row()})
    native.scan(db, source, 2, 0, STAMP)
    db.execute("CREATE TRIGGER deny_candidate BEFORE INSERT "
               "ON holder_registry "
               "BEGIN SELECT RAISE(FAIL,'blocked'); END")
    with pytest.raises(WatchError):
        native.scan(db, source, 2, 1, STAMP)
    saved = db.execute('SELECT cursor,pages FROM native_holder_scan')
    assert saved.fetchone() == (None, 0)
    count = db.execute('SELECT COUNT(*) FROM native_holder_positions')
    assert count.fetchone()[0] == 0


def test_cli_ci_guard_precedes_secret_read_and_database_creation(
        tmp_path, monkeypatch, capsys):
    from seo import tao_native_holders as native
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setattr(native, '_file_endpoint',
                        lambda _: pytest.fail('secret read'))
    target = tmp_path / 'holders.sqlite'
    assert native.main(['--db', str(target)]) == 2
    assert not target.exists()
    assert 'local' in capsys.readouterr().err.lower()


def test_cli_redacts_connection_exception_and_defaults_to_endpoint_file(
        tmp_path, monkeypatch, capsys):
    from seo import tao_native_holders as native
    monkeypatch.delenv('CI', raising=False)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    seen = []

    def endpoint(path):
        seen.append(path.name)
        return 'private endpoint'
    monkeypatch.setattr(native, '_file_endpoint', endpoint)

    def failed(*args):
        raise RuntimeError('wss://private/DO-NOT-PRINT')
    monkeypatch.setattr(native, '_connect', failed)
    assert native.main(['--db', str(tmp_path / 'holders.sqlite')]) == 2
    assert seen == ['dwellir.txt']
    captured = capsys.readouterr()
    assert 'DO-NOT-PRINT' not in captured.out + captured.err


def test_native_cli_does_not_import_browser_dependencies():
    script = ("import sys; sys.modules['bs4'] = None; "
              "from seo.tao_native_holders import main; main(['--help'])")
    result = subprocess.run([sys.executable, '-c', script],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


class Chain:
    """Real RPC response shape, without network in the unit suite."""
    prefixes = {
        'AlphaV2': PREFIX,
        'AlphaShareEpoch': ('0x658faa385070e074c85bf6b568cf0555'
                            '7d5f646b6504d6b113791974df342604'),
        'AlphaSharePoolEpoch': ('0x658faa385070e074c85bf6b568cf0555'
                                'd851552cd35f25e6f62c168db3aa2838'),
    }

    def __init__(self):
        self.response = None
        self.requested = None
        self.wrong_hashers = False
        self.wrong_types = False
        self.bad_default = False

    def get_metadata_storage_function(self, pallet, name, block_hash):
        assert pallet == 'SubtensorModule' and block_hash == BLOCK
        types = (['AccountId', 'u16'] if name.endswith('PoolEpoch')
                 else ['AccountId', 'AccountId', 'u16'])
        hashers = ['Blake2_128Concat'] * (len(types) - 1) + ['Identity']
        return SimpleNamespace(
            get_param_info=lambda: ['u16'] if self.wrong_types else types,
            get_param_hashers=(lambda: ['Identity']
                               if self.wrong_hashers else hashers),
            get_value_type_string=lambda: name)

    def create_storage_key(self, pallet, name):
        return SimpleNamespace(
            to_hex=lambda: self.prefixes[name],
            decode_scale_value=lambda: SimpleNamespace(
                value=1 if self.bad_default else 0))

    def rpc_request(self, method, params):
        assert method == 'state_queryStorageAt' and params[1] == BLOCK
        self.requested = params[0]
        return {'jsonrpc': '2.0', 'id': 1, 'result': self.response}

    def decode_scale(self, value_type, raw, block_hash):
        assert block_hash == BLOCK
        return {'0x01': {'mantissa': 8, 'exponent': 4}, '0x02': 2}[raw]


def storage_reply(storage_key, changes=None):
    # Independent key construction: row epoch retains all 98 tail bytes,
    # pool epoch retains the 48-byte hashed hotkey and 2-byte subnet only.
    tail = storage_key[66:]
    epoch = Chain.prefixes['AlphaShareEpoch'] + tail
    pool = Chain.prefixes['AlphaSharePoolEpoch'] + tail[:96] + tail[192:]
    return [{'block': BLOCK, 'changes': changes or [
        [storage_key, '0x01'], [epoch, '0x02'], [pool, '0x02']]}]


def test_batch_reads_exact_row_and_pool_epoch_keys_at_pinned_block():
    from seo import tao_native_holders as native
    chain = Chain()
    storage_key = key(1, 2, 258)
    chain.response = storage_reply(storage_key)
    source = native.ChainSource(chain)
    source.prepare(BLOCK)
    values = source.values(BLOCK, [storage_key])
    assert set(chain.requested) == {
        change[0] for change in chain.response[0]['changes']}
    assert values == {
        storage_key: row(mantissa=8, exponent=4, pool=2, epoch=2)}
    chain.response[0]['changes'][1][1] = None
    chain.response[0]['changes'][2][1] = None
    assert source.values(BLOCK, [storage_key])[storage_key]['pool_epoch'] == 0


@pytest.mark.parametrize('kind', ['wrong_block', 'missing',
                                  'duplicate', 'missing_share'])
def test_storage_batch_must_exactly_cover_requested_keys(kind):
    from seo import tao_native_holders as native
    chain = Chain()
    storage_key = key(1, 2, 258)
    chain.response = storage_reply(storage_key)
    if kind == 'wrong_block':
        chain.response[0]['block'] = '0x' + 'cd' * 32
    elif kind == 'missing':
        chain.response[0]['changes'].pop()
    elif kind == 'duplicate':
        chain.response[0]['changes'].append(chain.response[0]['changes'][0])
    elif kind == 'missing_share':
        chain.response[0]['changes'][0][1] = None
    source = native.ChainSource(chain)
    source.prepare(BLOCK)
    with pytest.raises(ValueError):
        source.values(BLOCK, [storage_key])


@pytest.mark.parametrize('kind', [
    'wrong_hashers', 'wrong_types', 'bad_default'])
def test_changed_runtime_storage_contract_is_rejected(kind):
    from seo import tao_native_holders as native
    chain = Chain()
    setattr(chain, kind, True)
    with pytest.raises(ValueError):
        native.ChainSource(chain).prepare(BLOCK)


def test_checkpoint_survives_reopen_and_repeated_page_cannot_advance(tmp_path):
    from seo import tao_native_holders as native
    path = tmp_path / 'checkpoint.sqlite'
    source = Source({key(1, 2, 28): row()})
    with sqlite3.connect(path) as db:
        native.scan(db, source, 2, 1, STAMP)
    with sqlite3.connect(path) as db:
        saved = db.execute('SELECT cursor FROM native_holder_scan')
        saved_cursor = saved.fetchone()[0]
        source.page = lambda *args: [saved_cursor]
        with pytest.raises(WatchError):
            native.scan(db, source, 2, 1, STAMP)
        saved = db.execute('SELECT cursor,pages FROM native_holder_scan')
        assert saved.fetchone() == (saved_cursor, 1)
        count = db.execute('SELECT COUNT(*) FROM native_holder_positions')
        assert count.fetchone()[0] == 1
