"""Rank discovered native stake within every active subnet, locally only.

Amounts are exact observed AlphaV2 position subtotals at the discovery pin.
They are not whole-wallet balances or profit. No AMM spot-price assumption is
made: TAO value and any TAO minimum remain unknown. Both input databases are
read-only; this module never assigns artificial browser holder ranks.
"""
import argparse
from collections import defaultdict, deque
from contextlib import ExitStack, closing, suppress
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from seo.tao_holder_queue import _coverage_status, _has_table, _time
from seo.tao_local_watch import WatchError, _connect, _file_endpoint
from seo.tao_native_evidence import _shares
from seo.tao_native_holders import (
    ChainSource, HASH, HEX, PALLET, PREFIX, _local_only, decode_key)


PREFIXES = {
    'NetworksAdded': ('0x658faa385070e074c85bf6b568cf0555'
                      '0e30450fc4d507a846032a7fa65d9a43'),
    'TotalHotkeyAlpha': ('0x658faa385070e074c85bf6b568cf0555'
                         'ee25c3b5b1886863480497907f1829e6'),
    'TotalHotkeySharesV2': ('0x658faa385070e074c85bf6b568cf0555'
                            'e47c637aa82c21537eb9caa511e543c5')}


class PoolSource(ChainSource):
    """Batch pinned pool reads through the existing bounded local transport."""
    def prepare(self, block):
        for name, types, hashers in (
                ('NetworksAdded', ['u16'], ['Identity']),
                ('TotalHotkeyAlpha', ['AccountId', 'u16'],
                 ['Blake2_128Concat', 'Identity']),
                ('TotalHotkeySharesV2', ['AccountId', 'u16'],
                 ['Blake2_128Concat', 'Identity'])):
            meta = self.chain.get_metadata_storage_function(
                PALLET, name, block_hash=block)
            if (meta is None or meta.get_param_info() != types
                    or meta.get_param_hashers() != hashers):
                raise ValueError('unsupported native pool metadata')
            pallet = self.chain.metadata.get_metadata_pallet(PALLET)
            if (pallet.value['storage']['prefix'] != PALLET
                    or meta.value['name'] != name):
                raise ValueError('unsupported native storage prefix')
            value_type = meta.get_value_type_string()
            default_bytes = bytes(
                meta.value_object['default'].value_object)
            default = self.chain.decode_scale(
                value_type, '0x' + default_bytes.hex(), block_hash=block)
            self.entries[name] = (PREFIXES[name], value_type, default)

    def _values(self, block, wanted):
        response = self._rpc('state_queryStorageAt', [list(wanted), block])
        if (not isinstance(response, list) or len(response) != 1
                or not isinstance(response[0], dict)
                or response[0].get('block') != block
                or not isinstance(response[0].get('changes'), list)):
            raise ValueError('invalid pinned pool response')
        result = {}
        for change in response[0]['changes']:
            if not isinstance(change, (list, tuple)) or len(change) != 2:
                raise ValueError('invalid pool storage change')
            key, raw = change
            if not isinstance(key, str) or key not in wanted or key in result:
                raise ValueError('unexpected pool storage key')
            _, value_type, default = self.entries[wanted[key]]
            if raw is None:
                result[key] = default
            elif isinstance(raw, str) and HEX.fullmatch(raw) and len(raw) > 2:
                result[key] = self.chain.decode_scale(
                    value_type, raw, block_hash=block)
            else:
                raise ValueError('invalid pool storage value')
        if set(result) != set(wanted):
            raise ValueError('incomplete pool storage response')
        return result

    def active(self, block):
        prefix = self.entries['NetworksAdded'][0]
        after, active = None, set()
        # More than 4,096 network slots requires an explicit implementation
        # review, never an apparently complete truncated active inventory.
        for _ in range(9):
            keys = self._rpc('state_getKeysPaged',
                             [prefix, 512, after, block])
            if (not isinstance(keys, list) or len(keys) > 512
                    or not all(isinstance(k, str) and HEX.fullmatch(k)
                               and k.startswith(prefix)
                               and len(k) == len(prefix) + 4 for k in keys)
                    or keys != sorted(set(keys))
                    or (after and keys and keys[0] <= after)):
                raise ValueError('invalid active subnet page')
            if not keys:
                return active
            values = self._values(block, dict.fromkeys(keys, 'NetworksAdded'))
            for key, enabled in values.items():
                if type(enabled) is not bool:
                    raise ValueError('invalid network enabled flag')
                if enabled:
                    active.add(int.from_bytes(
                        bytes.fromhex(key[len(prefix):]), 'little'))
            after = keys[-1]
        raise ValueError('active subnet page limit reached')

    def pools(self, block, pools):
        result = {}
        ordered = sorted(pools, key=lambda p: (p[1], p[0]))
        for offset in range(0, len(ordered), 256):
            wanted, pairs = {}, {}
            for hotkey, netuid in ordered[offset:offset + 256]:
                keys = []
                for name in ('TotalHotkeyAlpha', 'TotalHotkeySharesV2'):
                    # The suffix comes from an already validated AlphaV2 raw
                    # key: hashed hotkey (48 bytes) plus netuid (2 bytes).
                    # create_storage_key() calls init_runtime() at HEAD even
                    # when the caller previously pinned metadata.
                    key = self.entries[name][0] + pools[hotkey, netuid]
                    wanted[key] = name
                    keys.append(key)
                pairs[hotkey, netuid] = keys
            values = self._values(block, wanted)
            for pair, (alpha, shares) in pairs.items():
                result[pair] = dict(alpha_units=values[alpha],
                                    shares=values[shares])
        return result


def _research(databases, stamp, excluded):
    """Use the queue's attributed-history rules without needing a registry."""
    inspected, screened = set(), set(excluded)
    waiting, attempted = set(), set()
    now = _time(stamp)
    if now is None:
        raise ValueError('invalid research timestamp')
    for db in databases:
        if _has_table(db, 'holder_research_attempts'):
            for address, when, status in db.execute(
                    'SELECT address,checked_at,status '
                    'FROM holder_research_attempts'):
                checked = _time(when)
                if checked is None or checked > now:
                    continue
                attempted.add(address)
                if status == 'inspected':
                    inspected.add(address)
                elif status == 'screened':
                    screened.add(address)
                elif (status == 'unavailable'
                      and now < checked + timedelta(hours=24)):
                    waiting.add(address)
        if _has_table(db, 'wallet_performance'):
            for address, when, raw in db.execute(
                    'SELECT address,checked_at,payload '
                    'FROM wallet_performance'):
                checked = _time(when)
                if checked is None or checked > now:
                    continue
                try:
                    wallet = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if (not isinstance(wallet, dict)
                        or wallet.get('address') != address):
                    continue
                status = _coverage_status(wallet.get('coverage'))
                if status == 'inspected':
                    inspected.add(address)
                elif status == 'screened':
                    screened.add(address)
                if wallet.get('screens'):
                    screened.add(address)
    waiting -= inspected | screened
    return inspected, screened, waiting, attempted


def _alpha_units(mantissa, exponent, pool):
    shares = _shares(dict(mantissa=int(mantissa), exponent=exponent))
    denominator = _shares(pool['shares'])
    amount = pool['alpha_units']
    if (type(amount) is not int or not 0 <= amount < 2**64
            or denominator < 0 or shares < 0):
        raise ValueError('invalid native share pool')
    if denominator == 0:
        return 0  # Closed pools have no spendable member value.
    # SharePool::get_value floors the ratio and caps rounding drift at the
    # pool's real value. All arithmetic stays rational/integer until display.
    return min(amount, int(shares * amount / denominator))


def _decimal(units):
    whole, fraction = divmod(units, 10**9)
    return str(whole) if not fraction else (
        '%d.%09d' % (whole, fraction)).rstrip('0')


def prioritize(db, source, *, history_dbs=(), limit=128, stamp=None,
               exclude_addresses=()):
    """Select unique, uninspected wallets in rounds over all active subnets."""
    _local_only()
    if type(limit) is not int or not 1 <= limit <= 512:
        raise ValueError('use 1..512 candidate wallets')
    stamp = stamp or datetime.now(timezone.utc).isoformat()
    try:
        pin = db.execute('SELECT block_hash,height,complete '
                         'FROM native_holder_scan WHERE id=1').fetchone()
        if not pin or not HASH.fullmatch(pin[0]):
            raise ValueError('native discovery pin required')
        block, height, complete = pin
        rows = db.execute('SELECT storage_key,hotkey,coldkey,netuid,'
                          'share_mantissa,share_exponent '
                          'FROM native_holder_positions '
                          "WHERE status='candidate'").fetchall()
        pairs = {}
        seen = set()
        for key, hot, address, net, _, _ in rows:
            if key in seen or decode_key(key) != (hot, address, net):
                raise ValueError('native candidate identity mismatch')
            seen.add(key)
            tail = key[len(PREFIX):]
            pairs[hot, net] = tail[:96] + tail[-4:]
        source.prepare(block)
        active = source.active(block)
        if (not isinstance(active, set)
                or any(type(n) is not int or not 0 <= n <= 65535
                       for n in active)):
            raise ValueError('invalid active subnet inventory')
        pairs = {pair: tail for pair, tail in pairs.items()
                 if pair[1] in active}
        pools = source.pools(block, pairs)
        if not isinstance(pools, dict) or set(pools) != set(pairs):
            raise ValueError('incomplete native pool valuation')
        units, position_counts = defaultdict(int), defaultdict(int)
        inactive = 0
        for key, hot, address, net, mantissa, exponent in rows:
            if net not in active:
                inactive += 1
                continue
            units[net, address] += _alpha_units(
                mantissa, exponent, pools[hot, net])
            position_counts[net, address] += 1
        inspected, screened, waiting, attempted = _research(
            [db, *history_dbs], stamp, exclude_addresses)
        excluded = inspected | screened | waiting
        wallets = {n: {a for net, a in units if net == n} for n in active}
        served = inspected | screened | attempted
        order = sorted(active, key=lambda n: (len(wallets[n] & served), n))
        queues = {}
        ranks = {}
        for n in order:
            ranked = sorted(wallets[n], key=lambda a: (-units[n, a], a))
            ranks.update({(n, a): i + 1 for i, a in enumerate(ranked)})
            queues[n] = deque(sorted(
                (a for a in wallets[n] if a not in excluded and units[n, a]),
                key=lambda a: (a in attempted, -units[n, a], a)))
        chosen, selected_for = [], {}
        rounds = deque(n for n in order if queues[n])
        while rounds and len(chosen) < limit:
            net = rounds.popleft()
            while queues[net] and queues[net][0] in selected_for:
                queues[net].popleft()
            if queues[net]:
                address = queues[net].popleft()
                chosen.append(address)
                selected_for[address] = net
            if queues[net]:
                rounds.append(net)
        selected = set(chosen)
        coverage = []
        for net in sorted(active):
            found = wallets[net]
            coverage.append(dict(
                netuid=net,
                observed_positions=sum(
                    position_counts[net, a] for a in found),
                observed_wallets=len(found),
                positive_valued_wallets=sum(
                    bool(units[net, a]) for a in found),
                zero_valued_wallets=sum(not units[net, a] for a in found),
                inspected_wallets=len(found & inspected),
                screened_wallets=len(found & screened),
                retry_wait_wallets=len(found & waiting),
                eligible_wallets=sum(a not in excluded and bool(units[net, a])
                                     for a in found),
                selected_wallets=len(found & selected)))
        candidates = []
        for address in chosen:
            positions = [dict(
                netuid=n, observed_alpha_units=str(units[n, address]),
                observed_alpha=_decimal(units[n, address]),
                observed_positions=position_counts[n, address],
                observed_alpha_rank=ranks[n, address], tao_value=None)
                for n in sorted(active) if address in wallets[n]]
            candidates.append(dict(address=address,
                                   selected_for_netuid=selected_for[address],
                                   positions=positions))
        return dict(
            source='local-finney-alpha-v2-pools', block_hash=block,
            finalized_block=height, checked_at=stamp,
            active_subnet_inventory_complete=True,
            active_subnets=sorted(active), subnet_coverage=coverage,
            active_subnets_with_selected_wallets=sum(
                bool(r['selected_wallets']) for r in coverage),
            native_candidate_positions=len(rows), valued_pools=len(pools),
            inactive_observed_positions=inactive,
            alpha_v2_traversal_complete=bool(complete),
            holder_census_complete=False, addresses=chosen,
            candidates=candidates, tao_valuation_available=False,
            minimum_tao_filter_applied=False,
            selection='Largest observed alpha subtotals within each active '
                      'subnet, round-robin, excluding inspected, screened '
                      'and retry-wait wallets; untouched precede retries.',
            limitation='Holdings are research priority, not evidence of '
                       'profit. This partial AlphaV2 discovery excludes '
                       'unmigrated legacy Alpha; observed subtotals and ranks '
                       'may miss other positions. Alpha quantities are not '
                       'comparable across subnets. TAO values, the 10-TAO '
                       'minimum and unobserved exchange identities remain '
                       'unknown. Root subnet is included when active.')
    except Exception:
        raise WatchError('Native prioritization stopped; no partial '
                         'valuation published.') from None


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf8', dir=path.parent,
                prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--history-db', type=Path, action='append', default=[])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=128)
    parser.add_argument('--exclude-address', action='append', default=[])
    parser.add_argument('--endpoint-file', type=Path,
                        default=Path('dwellir.txt'))
    parser.add_argument('--rpc-limit', type=int, default=120)
    args = parser.parse_args(argv)
    chain = None
    previous_logging = logging.root.manager.disable
    try:
        _local_only()
        if not 1 <= args.limit <= 512 or not 10 <= args.rpc_limit <= 120:
            raise WatchError('Use 1..512 wallets and 10..120 RPC requests.')
        inputs = [args.db, args.endpoint_file, *args.history_db]
        if args.out.resolve() in {p.resolve() for p in inputs}:
            raise WatchError('Public output must differ from private inputs.')
        logging.disable(logging.CRITICAL)
        chain = _connect(_file_endpoint(args.endpoint_file), args.rpc_limit)
        with ExitStack() as stack:
            databases = [stack.enter_context(closing(sqlite3.connect(
                p.resolve().as_uri() + '?mode=ro', uri=True)))
                for p in [args.db, *args.history_db]]
            result = prioritize(
                databases[0], PoolSource(chain), history_dbs=databases[1:],
                limit=args.limit, exclude_addresses=args.exclude_address)
        _write(args.out, result)
        print(json.dumps(dict(
            finalized_block=result['finalized_block'],
            active_subnets=len(result['active_subnets']),
            represented_subnets=result['active_subnets_with_selected_wallets'],
            selected_wallets=len(result['addresses']),
            valued_pools=result['valued_pools'], tao_valuation=False)))
        return 0
    except WatchError as exc:
        print(exc, file=sys.stderr)
    except Exception:
        print('Native prioritization stopped; private connection '
              'details withheld.', file=sys.stderr)
    finally:
        if chain is not None:
            with suppress(Exception):
                chain.close()
        logging.disable(previous_logging)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
