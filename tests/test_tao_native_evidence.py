from copy import deepcopy

import pytest

from seo.tao_native_evidence import stake_units, growth_evidence


def state(block, alpha, *, shares=10, epoch=1):
    return dict(block=block, block_hash='0x' + str(block % 10) * 64,
                source='local-finney-storage', coldkey='wallet', hotkey='hot',
                netuid=24, time=f'2026-11-{block:02d}T00:00:00Z',
                staking_hotkeys=['hot'], other_hotkey_stakes=[],
                storage=dict(Alpha=dict(bits=0),
                             AlphaV2=dict(mantissa=shares, exponent=0),
                             TotalHotkeySharesV2=dict(mantissa=100,
                                                      exponent=0),
                             TotalHotkeyAlpha=alpha * 10**9,
                             AlphaShareEpoch=epoch, AlphaSharePoolEpoch=epoch))


def proof(before=None, after=None, events=(), coverage=None):
    return growth_evidence(
        before or state(1, 1000), after or state(3, 1100), events,
        coverage or dict(address='wallet', transactions='complete',
                         checked_at='2026-11-04T00:00:00+00:00'))


def test_native_growth_exact_math_and_provenance():
    item = proof()
    assert stake_units(state(1, 1000)) == 100 * 10**9
    assert item['before_alpha'] == 100
    assert item['after_alpha'] == 110
    assert item['evidence'] == 'verified_stake_growth'
    assert item['start_block'] == 1 and item['end_block'] == 3
    assert item['history_source'] == 'taostats-complete-history'
    assert item['proof']['before']['block_hash'].startswith('0x')
    assert 'tao' not in item  # It is never a zero-priced purchase.


@pytest.mark.parametrize('change', [
    lambda s: s['storage']['AlphaV2'].update(mantissa=11),
    lambda s: s['storage'].update(AlphaShareEpoch=0),
    lambda s: s.update(hotkey='different'),
    lambda s: s.update(coldkey='different'),
    lambda s: s.update(netuid=28),
    lambda s: s.update(block_hash='invalid'),
    lambda s: s.update(time='2026-10-01T00:00:00+00:00'),
    lambda s: s['storage'].update(TotalHotkeyAlpha=0),
])
def test_conflicting_native_states_rejected(change):
    after = state(3, 1100)
    change(after)
    with pytest.raises(ValueError):
        proof(after=after)


def test_incomplete_or_outdated_history_does_not_prove_growth():
    for coverage in (
        dict(address='wallet', transactions='partial',
             checked_at='2026-11-04T00:00:00Z'),
        dict(address='other', transactions='complete',
             checked_at='2026-11-04T00:00:00Z'),
        dict(address='wallet', transactions='complete',
             checked_at='2026-11-02T00:00:00Z'),
    ):
        with pytest.raises(ValueError):
            proof(coverage=coverage)


def test_intervening_movement_cannot_be_misclassified_as_growth():
    move = dict(src='wallet', dst='other', netuid=24, kind='stake_transfer',
                time='2026-11-02T00:00:00Z', extrinsic='2-0001')
    with pytest.raises(ValueError):
        proof(events=[move])
    # Other subnets and unrelated wallets do not affect this position.
    assert proof(events=[dict(move, netuid=28)])
    assert proof(events=[dict(move, src='unrelated')])


def test_missing_epoch_requires_explicit_absent_metadata_and_zero_successor():
    before = state(1, 1000, epoch=0)
    del before['storage']['AlphaShareEpoch']
    del before['storage']['AlphaSharePoolEpoch']
    after = state(3, 1100, epoch=0)
    with pytest.raises(ValueError):
        proof(before, after)
    before['epoch_storage_absent'] = True
    assert proof(before, after)
    newer = deepcopy(after)
    newer['storage'].update(AlphaShareEpoch=1, AlphaSharePoolEpoch=1)
    with pytest.raises(ValueError):
        proof(before, newer)


def test_share_math_preserves_nano_precision_and_rejects_bad_values():
    row = state(1, 1000)
    row['storage']['AlphaV2'] = dict(
        mantissa=333333333333333333333, exponent=-20)
    row['storage']['TotalHotkeySharesV2'] = dict(mantissa=10, exponent=0)
    row['storage']['TotalHotkeyAlpha'] = 1000000000001
    assert stake_units(row) == 333333333333
    row['storage']['AlphaV2']['exponent'] = 1000000
    with pytest.raises(ValueError):
        stake_units(row)


def test_growth_requires_whole_subnet_native_inventory():
    after = state(3, 1100)
    del after['staking_hotkeys']
    with pytest.raises(ValueError):
        proof(after=after)
    after['staking_hotkeys'] = ['hot', 'other']
    with pytest.raises(ValueError):
        proof(after=after)
    other = state(3, 100)
    other['hotkey'] = 'other'
    after['other_hotkey_stakes'] = [other]
    with pytest.raises(ValueError):
        proof(after=after)
    other['storage']['AlphaV2']['mantissa'] = 0
    assert proof(after=after)
    other['storage']['Alpha']['bits'] = 100 << 64
    with pytest.raises(ValueError):
        proof(after=after)
    other['storage']['Alpha']['bits'] = 0
    other['block_hash'] = '0x' + 'f' * 64
    with pytest.raises(ValueError):
        proof(after=after)
