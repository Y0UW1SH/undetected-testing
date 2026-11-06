"""Validate local archival stake evidence without fetching or storing secrets.

Unchanged shares, a live pool and complete indexed movement history establish
an interval reconciliation, not the time or tax valuation of each reward. We
retain that source distinction instead of claiming a full block-event replay.
"""
from datetime import datetime
from fractions import Fraction
import re


def _time(value):
    if not isinstance(value, str):
        raise ValueError('missing evidence timestamp')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('evidence timestamps require timezones')
    return stamp


def _integer(value, name, maximum=2**128 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('invalid ' + name)
    return value


def _shares(value):
    if not isinstance(value, dict):
        raise ValueError('missing native shares')
    mantissa = _integer(value.get('mantissa'), 'share mantissa')
    exponent = value.get('exponent')
    if type(exponent) is not int or not -128 <= exponent <= 127:
        raise ValueError('invalid share exponent')
    return Fraction(mantissa) * Fraction(10) ** exponent


def _epoch(state):
    storage = state['storage']
    names = ('AlphaShareEpoch', 'AlphaSharePoolEpoch')
    if not any(n in storage for n in names):
        if state.get('epoch_storage_absent') is not True:
            raise ValueError('missing pool generation evidence')
        return 0
    row, pool = (_integer(storage.get(n), n, 2**64 - 1) for n in names)
    if pool and row != pool:
        raise ValueError('retired share position')
    return pool


def stake_units(state):
    """Native base units, with integer-floor rounding and epoch validation."""
    storage = state.get('storage')
    if not isinstance(storage, dict):
        raise ValueError('missing native storage')
    _epoch(state)
    numerator = _shares(storage.get('AlphaV2'))
    denominator = _shares(storage.get('TotalHotkeySharesV2'))
    amount = _integer(storage.get('TotalHotkeyAlpha'), 'pool alpha', 2**64 - 1)
    if denominator <= 0 or numerator > denominator:
        raise ValueError('invalid share pool')
    return int(numerator * amount / denominator)


def _whole_subnet(state):
    """A per-hotkey growth proof may only price an isolated subnet position."""
    hotkeys = state.get('staking_hotkeys')
    others = state.get('other_hotkey_stakes')
    if (not isinstance(hotkeys, list) or not isinstance(others, list)
            or not all(isinstance(h, str) for h in hotkeys)
            or len(set(hotkeys)) != len(hotkeys)
            or state['hotkey'] not in hotkeys):
        raise ValueError('missing complete native staking hotkey inventory')
    found = set()
    for row in others:
        hotkey = row.get('hotkey')
        if hotkey == state['hotkey'] or hotkey in found:
            raise ValueError('duplicate native stake inventory')
        if any(row.get(k) != state[k] for k in (
                'block', 'block_hash', 'coldkey', 'netuid', 'time', 'source')):
            raise ValueError('native inventory identity mismatch')
        legacy = row.get('storage', {}).get('Alpha')
        if (row.get('legacy_storage_absent') is not True
                and legacy != {'bits': 0}):
            raise ValueError('other hotkey legacy stake not proven empty')
        if stake_units(row):
            raise ValueError('growth proof requires isolated subnet stake')
        found.add(hotkey)
    if found != set(hotkeys) - {state['hotkey']}:
        raise ValueError('missing other hotkey stake evidence')


def growth_evidence(before, after, events, coverage):
    """Return a ledger growth interval only when its evidence agrees.

The caller supplies complete Taostats account transaction coverage at or after
the endpoint. Native snapshots prove balances and unchanged shares; indexed
history supplies the no-intervening-movement check. The returned provenance
does not claim independent exhaustive native event coverage.
"""
    for row in (before, after):
        if row.get('source') != 'local-finney-storage':
            raise ValueError('expected local native storage evidence')
        if not re.fullmatch(r'0x[0-9a-fA-F]{64}', row.get('block_hash', '')):
            raise ValueError('invalid evidence block hash')
        _integer(row.get('block'), 'block number', 2**64 - 1)
        _integer(row.get('netuid'), 'netuid', 65535)
        if not row.get('coldkey') or not row.get('hotkey'):
            raise ValueError('missing stake identity')
    if any(before[k] != after[k] for k in ('coldkey', 'hotkey', 'netuid')):
        raise ValueError('different stake identities')
    start, end = _time(before['time']), _time(after['time'])
    if before['block'] >= after['block'] or start >= end:
        raise ValueError('invalid evidence interval')
    address = before['coldkey']
    if (coverage.get('address') != address
            or coverage.get('transactions') != 'complete'
            or _time(coverage.get('checked_at')) < end):
        raise ValueError('complete history through endpoint required')
    if _epoch(before) != _epoch(after):
        raise ValueError('share pool generation changed')
    if (_shares(before['storage']['AlphaV2'])
            != _shares(after['storage']['AlphaV2'])):
        raise ValueError('stake shares changed')
    for event in events:
        if (event.get('netuid') != before['netuid']
                or address not in (event.get('src'), event.get('dst'))
                or event.get('kind') == 'tao_transfer'):
            continue
        when = _time(event.get('time'))
        if start < when <= end:
            raise ValueError('intervening stake movement')
        # Native blocks close the same-timestamp ambiguity in indexed history.
        xid = re.fullmatch(r'(\d+)-\d+', event.get('extrinsic') or '')
        if xid and before['block'] < int(xid[1]) <= after['block']:
            raise ValueError('intervening stake movement block')
    first, last = stake_units(before), stake_units(after)
    if first <= 0 or last <= first:
        raise ValueError('no positive proven growth')
    _whole_subnet(before)
    _whole_subnet(after)
    return dict(
        id=f"native-growth/{address}/{before['netuid']}/"
           f"{before['block_hash']}/{after['block_hash']}",
        address=address, netuid=before['netuid'],
        start_time=before['time'], time=after['time'],
        start_block=before['block'], end_block=after['block'],
        before_alpha=first / 1e9, after_alpha=last / 1e9,
        evidence='verified_stake_growth', source='local-finney-storage',
        history_source='taostats-complete-history',
        history_checked_at=coverage['checked_at'],
        proof=dict(before=before, after=after),
        limitation='Interval growth reconciled from unchanged native shares '
                   'and indexed movement history; individual reward times '
                   'and receipt valuations were not reconstructed.')
