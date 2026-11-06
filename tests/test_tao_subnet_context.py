"""Pinned lifecycle context never turns a reserve into a realizable exit."""
import json
from types import SimpleNamespace

import pytest

from seo.tao_local_watch import WatchError


BLOCK = '0x' + 'ab' * 32
STAMP = '2026-11-06T12:00:00+00:00'


@pytest.fixture(autouse=True)
def local_environment(monkeypatch):
    monkeypatch.delenv('CI', raising=False)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)


def raw(active=True, registration=90, counter=2):
    return dict(NetworksAdded=active, NetworkRegisteredAt=registration,
                RegisteredSubnetCounter=counter, SubnetMechanism=1,
                SubnetTAO=253765051114, SubnetAlphaIn=60611035058807,
                SwapBalancer={'quote': 400000000000000000},
                PalSwapInitialized=True, FeeRate=33, SubtokenEnabled=True,
                SubnetState='Started')


class Source:
    def __init__(self, rows=None, immunity=20, height=100):
        self.rows = {1: raw()} if rows is None else rows
        self.immunity, self.height = immunity, height

    def pin(self, block=None):
        return BLOCK, self.height

    def read(self, block, known):
        assert block == BLOCK
        return dict(rows={n: self.rows.get(n, {})
                          for n in set(self.rows) | set(known)},
                    retained=set(self.rows), immunity=self.immunity,
                    timestamp_ms=1793966399000, metadata_missing=[])


def run(source=None, **kwargs):
    from seo.tao_subnet_context import collect
    return collect(source or Source(), stamp=STAMP, **kwargs)


def test_generation_immunity_and_raw_weighted_reserves():
    result = run()
    row = result['subnets'][0]
    assert result['network_immunity_period_blocks'] == 20
    assert result['block_time'] == '2026-11-06T11:59:59+00:00'
    assert row['generation_id'] == 'finney:1:registration:90'
    assert row['generation_basis'] == 'registration_block'
    assert row['age_blocks'] == 10
    assert row['subnet_immunity_remaining_blocks'] == 10
    assert row['subnet_immune'] is True
    assert row['generation_changed'] is None
    liquidity = row['liquidity']
    assert liquidity['model'] == 'weighted_balancer'
    assert liquidity['tao_reserve_rao'] == '253765051114'
    assert liquidity['tao_reserve_tao'] == '253.765051114'
    assert liquidity['tao_weight_perquintill'] == '400000000000000000'
    assert liquidity['alpha_weight_perquintill'] == '600000000000000000'
    assert liquidity['spot_price_tao_per_alpha'] is None
    assert liquidity['exit_quote_tao'] is None


@pytest.mark.parametrize('height,immune,remaining', [
    (109, True, 1), (110, False, 0)])
def test_subnet_immunity_exact_boundary(height, immune, remaining):
    row = run(Source(height=height))['subnets'][0]
    assert row['subnet_immune'] is immune
    assert row['subnet_immunity_remaining_blocks'] == remaining


def test_missing_registration_not_assumed_genesis_or_known_immunity():
    rows = {1: raw(registration=None), 2: raw(registration=0)}
    a, b = run(Source(rows))['subnets']
    assert a['age_blocks'] is None and a['subnet_immune'] is None
    assert a['generation_id'] is None
    assert b['age_blocks'] == 100


def test_unknown_pool_not_constant_product_and_root_has_no_pool():
    rows = {0: raw(), 1: raw(), 2: raw()}
    rows[1]['SwapBalancer'] = None
    rows[2]['PalSwapInitialized'] = False
    root, missing, pending = run(Source(rows))['subnets']
    assert root['liquidity']['model'] == 'root_no_pool'
    assert root['liquidity']['tao_reserve_rao'] is None
    assert root['subnet_immune'] is None
    assert missing['liquidity']['model'] == 'unknown'
    assert pending['liquidity']['model'] == 'unknown'


def test_previous_ids_retained_and_counter_detects_reuse():
    previous = run(Source({1: raw(counter=1, registration=80), 4: raw()},
                          height=99))
    result = run(Source({1: raw(), 3: raw(active=False)}), previous=previous)
    rows = {r['netuid']: r for r in result['subnets']}
    assert rows[1]['generation_changed'] is True
    assert rows[1]['previous_generation_id'] == 'finney:1:registration:80'
    assert rows[3]['lifecycle_status'] == 'inactive_retained'
    assert rows[4]['lifecycle_status'] == 'inactive_previous_only'
    assert rows[4]['active'] is False
    assert rows[4]['subnet_immune'] is None
    assert rows[4]['registration_block'] is None
    assert result['history'][0]['subnets'][1]['netuid'] == 4


def test_previous_future_pin_and_future_registration_rejected():
    from seo.tao_subnet_context import collect
    previous = run(Source(height=101))
    with pytest.raises(WatchError):
        collect(Source(), previous=previous, stamp=STAMP)
    with pytest.raises(WatchError):
        run(Source({1: raw(registration=101)}))


@pytest.mark.parametrize('name,value', [
    ('SubnetTAO', -1), ('NetworksAdded', 1),
    ('SwapBalancer', {'quote': 10**18 + 1})])
def test_malformed_values_fail_closed(name, value):
    rows = {1: raw()}
    rows[1][name] = value
    with pytest.raises(WatchError):
        run(Source(rows))


@pytest.mark.parametrize('key', ['CI', 'GITHUB_ACTIONS'])
def test_local_guard_before_any_rpc(monkeypatch, key):
    from seo.tao_subnet_context import collect
    monkeypatch.setenv(key, 'true')
    with pytest.raises(WatchError):
        collect(object())


def test_rpc_response_must_match_pin_and_all_keys():
    from seo.tao_subnet_context import SubnetSource
    chain = SimpleNamespace(rpc_request=lambda *a: {'result': [
        {'block': BLOCK, 'changes': [['0x11', None]]}]})
    source = SubnetSource(chain)
    source.entries['NetworksAdded'] = ('0x11', 'bool', False, 'Identity')
    assert source.values(BLOCK, {'0x11': 'NetworksAdded'}) == {'0x11': None}
    with pytest.raises(ValueError):
        source.values(BLOCK, dict.fromkeys(('0x11', '0x12'), 'NetworksAdded'))
    with pytest.raises(ValueError):
        source.values('0x' + 'cd' * 32, {'0x11': 'NetworksAdded'})


def test_raw_inventory_validates_length_order_and_pin():
    from seo.tao_subnet_context import SubnetSource
    prefix = '0x' + '11' * 32
    calls = []
    pages = [[prefix + '0100', prefix + '0300'], []]

    def rpc(method, params):
        calls.append((method, params))
        return {'result': pages.pop(0)}

    source = SubnetSource(SimpleNamespace(rpc_request=rpc))
    source.entries['NetworksAdded'] = (prefix, 'bool', False, 'Identity')
    assert source.inventory(BLOCK, 'NetworksAdded') == {1, 3}
    assert all(c[1][-1] == BLOCK for c in calls)
    pages[:] = [[prefix + '010000']]
    with pytest.raises(ValueError):
        source.inventory(BLOCK, 'NetworksAdded')


def test_cli_connection_error_never_prints_endpoint(
        monkeypatch, tmp_path, capsys):
    from seo import tao_subnet_context as context
    secret = 'https://private.invalid/key-secret'
    monkeypatch.setattr(context, '_file_endpoint', lambda p: secret)

    def fail(*args):
        raise ValueError(secret)

    monkeypatch.setattr(context, '_connect', fail)
    out = tmp_path / 'context.json'
    assert context.main(['--out', str(out)]) == 2
    assert secret not in capsys.readouterr().err
    assert not out.exists()


def test_atomic_public_output_contains_no_transport_input(tmp_path):
    from seo.tao_subnet_context import _write
    out = tmp_path / 'public.json'
    _write(out, run())
    value = json.loads(out.read_text())
    assert value['source'] == 'local-finney-subnet-context'
    assert value['active_subnet_inventory_complete'] is True


@pytest.mark.parametrize('state', [
    'PendingDissolution', {'Dissolving': {'phase': 'ignored'}}])
def test_dissolving_state_detected_even_if_active(state):
    rows = {1: raw()}
    rows[1]['SubnetState'] = state
    row = run(Source(rows))['subnets'][0]
    assert row['active'] is True
    assert row['is_dissolving'] is True


def test_registration_identity_does_not_require_historical_counter():
    previous = run(Source({1: raw(counter=None)}, height=99))
    generation = previous['subnets'][0]['generation_id']
    assert generation == 'finney:1:registration:90'
    row = run(previous=previous)['subnets'][0]
    assert row['generation_changed'] is False
    assert row['generation_counter_inconsistent'] is False


def test_counter_conflict_is_inconsistency_not_new_registration():
    previous = run(Source({1: raw(counter=1)}, height=99))
    row = run(previous=previous)['subnets'][0]
    assert row['generation_changed'] is False
    assert row['generation_counter_inconsistent'] is True


def test_cleanup_inventory_and_verified_metadata_defaults_are_retained():
    from seo.tao_subnet_context import SubnetSource

    class Pinned(SubnetSource):
        def prepare(self, block):
            assert block == BLOCK
            self.missing = []
            self.entries = {
                'DissolveCleanupQueue': ('0x10', 'Vec', [], None),
                'Now': ('0x11', 'u64', 0, None),
                'NetworkImmunityPeriod': ('0x12', 'u64', 20, None),
                'NetworksAdded': ('0x20', 'bool', False, 'Identity'),
                'FeeRate': ('0x21', 'u16', 33, 'Identity')}

        def inventory(self, block, name):
            return {1}

        def values(self, block, wanted):
            assert block == BLOCK
            return {k: {'DissolveCleanupQueue': [9],
                        'Now': 1793966399000}.get(n)
                    for k, n in wanted.items()}

    data = Pinned(None).read(BLOCK, {7})
    assert data['retained'] == {1, 9}
    assert set(data['rows']) == {1, 7, 9}
    assert data['rows'][9]['DissolveCleanupQueued'] is True
    assert data['rows'][1]['FeeRate'] == 33
    assert data['immunity'] == 20
