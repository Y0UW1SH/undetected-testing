"""Export public subnet lifecycle/liquidity context using local pinned reads.

The endpoint is read only from the local endpoint file, never exported. Context
uses a fresh finalized pin unless --block-hash requests an earlier finalized
block. --db supplies historical netuids, not the context pin. Raw pool reserves
are evidence about liquidity, not an executable exit quote or wallet profit.
"""
import argparse
from contextlib import closing, suppress
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import sys

from seo.tao_local_watch import WatchError, _connect, _file_endpoint
from seo.tao_native_holders import ChainSource, HASH, HEX, PALLET, _local_only
from seo.tao_native_priority import _decimal, _write


SOURCE = 'local-finney-subnet-context'
ACCURACY = 10**18
SPECS = {
    'NetworksAdded': (PALLET, 'Identity'),
    'NetworkRegisteredAt': (PALLET, 'Identity'),
    'RegisteredSubnetCounter': (PALLET, 'Identity'),
    'SubnetState': (PALLET, 'Identity'),
    'SubnetMechanism': (PALLET, 'Identity'),
    'SubnetTAO': (PALLET, 'Identity'),
    'SubnetAlphaIn': (PALLET, 'Identity'),
    'SubtokenEnabled': (PALLET, 'Identity'),
    'NetworkImmunityPeriod': (PALLET, None),
    'DissolveCleanupQueue': (PALLET, None),
    'Now': ('Timestamp', None),
    'SwapBalancer': ('Swap', 'Twox64Concat'),
    'PalSwapInitialized': ('Swap', 'Twox64Concat'),
    'FeeRate': ('Swap', 'Twox64Concat'),
}
INVENTORIES = ('NetworksAdded', 'NetworkRegisteredAt',
               'RegisteredSubnetCounter', 'SubnetState')
REFERENCES = [
    'https://www.bittensor.com/docs/concepts/staking-pools',
    'https://guides.learnbittensor.org/subtensor-api/storage',
    'https://raw.githubusercontent.com/opentensor/subtensor/main/'
    'pallets/subtensor/src/coinbase/root.rs',
    'https://raw.githubusercontent.com/opentensor/subtensor/main/'
    'pallets/subtensor/src/subnets/subnet.rs',
    'https://raw.githubusercontent.com/opentensor/subtensor/main/'
    'pallets/swap/src/pallet/balancer.rs',
]


def _uint(value, bits=64):
    if value is not None and (type(value) is not int
                              or not 0 <= value < 2**bits):
        raise ValueError('invalid unsigned storage value')
    return value


def _bool(value):
    if value is not None and type(value) is not bool:
        raise ValueError('invalid boolean storage value')
    return value


def _time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone required')
    return parsed.astimezone(timezone.utc)


def _prefix(pallet, name):
    # Offline hashing; create_storage_key() would initialize HEAD metadata.
    from substrateinterface.utils.hasher import xxh128
    return '0x' + (xxh128(pallet.encode()) + xxh128(name.encode())).hex()


def _suffix(netuid, hasher):
    raw = netuid.to_bytes(2, 'little')
    if hasher == 'Twox64Concat':
        from substrateinterface.utils.hasher import two_x64_concat
        raw = two_x64_concat(raw)
    return raw.hex()


class SubnetSource(ChainSource):
    """Use raw paged keys and batched values at one validated metadata pin."""
    def pin(self, requested=None):
        block, height = self.finalized()
        if requested is None or requested == block:
            return block, height
        if not HASH.fullmatch(requested):
            raise ValueError('invalid context block hash')
        header = self._rpc('chain_getHeader', [requested])
        historical_height = int(header['number'], 16)
        canonical = self._rpc('chain_getBlockHash', [historical_height])
        if historical_height > height or canonical != requested:
            raise ValueError('context block is not finalized canonical')
        return requested, historical_height

    def prepare(self, block):
        self.missing = []
        for name, (pallet, hasher) in SPECS.items():
            meta = self.chain.get_metadata_storage_function(
                pallet, name, block_hash=block)
            if meta is None:
                if name in ('NetworksAdded', 'Now'):
                    raise ValueError('required context metadata missing')
                self.missing.append(name)
                continue
            types = ['u16'] if hasher else []
            storage = self.chain.metadata.get_metadata_pallet(pallet)
            if (meta.get_param_info() != types
                    or (hasher and meta.get_param_hashers() != [hasher])
                    or storage.value['storage']['prefix'] != pallet
                    or meta.value['name'] != name):
                raise ValueError('unsupported context storage metadata')
            value_type = meta.get_value_type_string()
            default = None
            if meta.value.get('modifier') == 'Default':
                raw = bytes(meta.value_object['default'].value_object)
                default = self.chain.decode_scale(
                    value_type, '0x' + raw.hex(), block_hash=block)
            self.entries[name] = (_prefix(pallet, name), value_type,
                                  default, hasher)

    def values(self, block, wanted):
        response = self._rpc('state_queryStorageAt', [list(wanted), block])
        if (not isinstance(response, list) or len(response) != 1
                or not isinstance(response[0], dict)
                or response[0].get('block') != block
                or not isinstance(response[0].get('changes'), list)):
            raise ValueError('invalid pinned context response')
        result = {}
        for change in response[0]['changes']:
            if not isinstance(change, (list, tuple)) or len(change) != 2:
                raise ValueError('invalid context storage change')
            key, raw = change
            if not isinstance(key, str) or key not in wanted or key in result:
                raise ValueError('unexpected context storage key')
            if raw is None:
                # Presence matters: absent registration does not mean genesis.
                result[key] = None
            elif isinstance(raw, str) and HEX.fullmatch(raw) and len(raw) > 2:
                result[key] = self.chain.decode_scale(
                    self.entries[wanted[key]][1], raw, block_hash=block)
            else:
                raise ValueError('invalid context storage bytes')
        if set(result) != set(wanted):
            raise ValueError('incomplete context storage response')
        return result

    def inventory(self, block, name):
        prefix, _, _, hasher = self.entries[name]
        if hasher != 'Identity':
            raise ValueError('unsupported context inventory hasher')
        after, found = None, set()
        for _ in range(9):
            keys = self._rpc('state_getKeysPaged', [prefix, 512, after, block])
            if (not isinstance(keys, list) or len(keys) > 512
                    or not all(isinstance(k, str) and HEX.fullmatch(k)
                               and k.startswith(prefix)
                               and len(k) == len(prefix) + 4 for k in keys)
                    or keys != sorted(set(keys))
                    or (after and keys and keys[0] <= after)):
                raise ValueError('invalid context inventory page')
            if not keys:
                return found
            found.update(int.from_bytes(bytes.fromhex(k[len(prefix):]),
                                        'little') for k in keys)
            after = keys[-1]
        raise ValueError('context inventory page bound reached')

    def read(self, block, known):
        self.prepare(block)
        global_keys = {prefix: name for name, (prefix, _, _, hasher)
                       in self.entries.items() if not hasher}
        globals_ = {}
        for key, value in self.values(block, global_keys).items():
            name = global_keys[key]
            globals_[name] = (self.entries[name][2]
                              if value is None else value)
        cleanup = globals_.get('DissolveCleanupQueue')
        if cleanup is not None and (
                not isinstance(cleanup, list)
                or any(_uint(n, 16) is None for n in cleanup)):
            raise ValueError('unsupported cleanup queue representation')
        retained = set()
        for name in INVENTORIES:
            if name in self.entries:
                retained.update(self.inventory(block, name))
        retained.update(cleanup or [])
        netuids = retained | set(known)
        if len(netuids) > 4096:
            raise ValueError('context netuid bound exceeded')
        rows = {n: {} for n in sorted(netuids)}
        wanted, lookup = {}, {}
        for name, (prefix, _, _, hasher) in self.entries.items():
            if not hasher:
                continue
            for netuid in sorted(netuids):
                key = prefix + _suffix(netuid, hasher)
                wanted[key] = name
                lookup[key] = netuid
        decoded = {}
        ordered = list(wanted)
        for offset in range(0, len(ordered), 512):
            decoded.update(self.values(block, {
                k: wanted[k] for k in ordered[offset:offset + 512]}))
        for key, value in decoded.items():
            name, netuid = wanted[key], lookup[key]
            if value is None and name in (
                    'FeeRate', 'SubtokenEnabled', 'PalSwapInitialized'):
                value = self.entries[name][2]
            rows[netuid][name] = value
        if cleanup is not None:
            for netuid, row in rows.items():
                row['DissolveCleanupQueued'] = netuid in cleanup
        return dict(rows=rows, retained=retained,
                    immunity=globals_.get('NetworkImmunityPeriod'),
                    timestamp_ms=globals_.get('Now'),
                    metadata_missing=self.missing)


def _generation(netuid, registration):
    # Counters were not always stored. An actually present registration block
    # identifies the generation without making later counter availability
    # look like a new registration.
    return (f'finney:{netuid}:registration:{registration}'
            if registration is not None else None)


def _past(document, height, stamp):
    """Retain typed lifecycle evidence, never arbitrary prior-file payloads."""
    if (document.get('source') != SOURCE
            or not HASH.fullmatch(document['block_hash'])
            or _uint(document['finalized_block']) > height
            or _time(document['checked_at']) > _time(stamp)):
        raise ValueError('invalid previous context')
    rows, seen = [], set()
    for row in document['subnets']:
        netuid = _uint(row['netuid'], 16)
        if netuid is None or netuid in seen:
            raise ValueError('invalid previous subnet inventory')
        seen.add(netuid)
        registration = _uint(row.get('registration_block'))
        counter = _uint(row.get('registered_subnet_counter'))
        rows.append(dict(netuid=netuid, active=_bool(row['active']),
                         registration_block=registration,
                         registered_subnet_counter=counter,
                         generation_id=_generation(netuid, registration)))
    if len(rows) > 4096:
        raise ValueError('previous context bound exceeded')
    return dict(source=SOURCE, checked_at=_time(
        document['checked_at']).isoformat(), block_time=_time(
            document['block_time']).isoformat(),
        block_hash=document['block_hash'],
        finalized_block=document['finalized_block'], subnets=rows)


def _row(netuid, values, retained, height, immunity, prior):
    enabled = _bool(values.get('NetworksAdded'))
    active = enabled is True
    registered = _uint(values.get('NetworkRegisteredAt'))
    counter = _uint(values.get('RegisteredSubnetCounter'))
    if registered is not None and registered > height:
        raise ValueError('registration beyond context pin')
    generation = _generation(netuid, registered)
    old_generation = prior.get('generation_id') if prior else None
    changed = (generation != old_generation
               if generation and old_generation else None)
    old_counter = prior.get('registered_subnet_counter') if prior else None
    inconsistent = (changed is False and counter is not None
                    and old_counter is not None and counter != old_counter)
    age = height - registered if registered is not None and active else None
    remaining = (max(0, min(2**64 - 1, registered + immunity) - height)
                 if netuid != 0 and active and registered is not None
                 and immunity is not None else None)
    tao = _uint(values.get('SubnetTAO'))
    alpha = _uint(values.get('SubnetAlphaIn'))
    mechanism = _uint(values.get('SubnetMechanism'), 16)
    initialized = _bool(values.get('PalSwapInitialized'))
    quote = None
    balancer = values.get('SwapBalancer')
    if balancer is not None:
        if not isinstance(balancer, dict) or set(balancer) != {'quote'}:
            raise ValueError('unsupported balancer weight representation')
        quote = _uint(balancer['quote'])
        if quote is None or not 0 < quote < ACCURACY:
            raise ValueError('invalid balancer weight')
    model = ('root_no_pool' if netuid == 0 else 'weighted_balancer'
             if mechanism == 1 and initialized is True and quote is not None
             else 'unknown')
    if netuid == 0:
        tao, alpha, quote = None, None, None
    state = values.get('SubnetState')
    # Do not serialize unknown enum structures or arbitrary strings.
    if isinstance(state, dict) and len(state) == 1:
        state = next(iter(state))
    states = ('Registered', 'Started', 'PendingDissolution', 'Dissolving')
    state = state if state in states else None
    queued = _bool(values.get('DissolveCleanupQueued'))
    retiring = state in ('PendingDissolution', 'Dissolving')
    dissolving = (True if queued or retiring
                  else False if queued is False or state is not None else None)
    return dict(
        netuid=netuid, active=active,
        lifecycle_status='active' if active else 'inactive_retained'
        if netuid in retained else 'inactive_previous_only',
        chain_lifecycle_state=state, is_dissolving=dissolving,
        registration_block=registered, registered_subnet_counter=counter,
        generation_id=generation, generation_changed=changed,
        generation_basis='registration_block' if generation else None,
        generation_counter_inconsistent=inconsistent,
        previous_generation_id=old_generation,
        age_blocks=age, subnet_immunity_period_blocks=immunity,
        subnet_immunity_remaining_blocks=remaining,
        subnet_immune=remaining > 0 if remaining is not None else None,
        trading_enabled=_bool(values.get('SubtokenEnabled')),
        liquidity=dict(
            tao_reserve_rao=str(tao) if tao is not None else None,
            tao_reserve_tao=_decimal(tao) if tao is not None else None,
            alpha_reserve_units=str(alpha) if alpha is not None else None,
            model=model, mechanism=mechanism,
            pool_initialized=initialized,
            tao_weight_perquintill=str(quote) if quote is not None else None,
            alpha_weight_perquintill=str(ACCURACY - quote)
            if quote is not None else None,
            fee_rate_u16=_uint(values.get('FeeRate'), 16),
            spot_price_tao_per_alpha=None, exit_quote_tao=None,
            exit_quote_status='not_requested',
            reserve_source='SubtensorModule.SubnetTAO/SubnetAlphaIn',
            weight_source='Swap.SwapBalancer'))


def collect(source, *, block_hash=None, known_netuids=(), previous=None,
            stamp=None):
    _local_only()
    try:
        stamp = stamp or datetime.now(timezone.utc).isoformat()
        stamp = _time(stamp).isoformat()
        block, height = source.pin(block_hash)
        if not HASH.fullmatch(block) or _uint(height) is None:
            raise ValueError('invalid context pin')
        history, old = [], {}
        if previous is not None:
            documents = [*previous.get('history', [])[-31:], previous]
            for document in documents:
                past = _past(document, height, stamp)
                if (past['finalized_block'] == height
                        and past['block_hash'] != block):
                    raise ValueError('previous context conflicts with pin')
                history.append(past)
            old = {r['netuid']: r for r in history[-1]['subnets']}
        known = set(known_netuids) | set(old)
        for netuid in known:
            if _uint(netuid, 16) is None:
                raise ValueError('invalid retained netuid')
        data = source.read(block, known)
        immunity = _uint(data['immunity'])
        timestamp = _uint(data['timestamp_ms'])
        if timestamp is None:
            raise ValueError('context block timestamp missing')
        block_time = datetime.fromtimestamp(timestamp / 1000,
                                            timezone.utc).isoformat()
        if _time(block_time) > _time(stamp):
            raise ValueError('context timestamp is in the future')
        rows = [_row(_uint(n, 16), values, data['retained'], height, immunity,
                     old.get(n)) for n, values in sorted(data['rows'].items())]
        return dict(
            schema_version=1, source=SOURCE, checked_at=stamp,
            block_time=block_time, block_hash=block, finalized_block=height,
            network_immunity_period_blocks=immunity,
            active_subnet_inventory_complete=True,
            retained_inventory_sources=[*INVENTORIES, 'DissolveCleanupQueue'],
            historical_subnet_inventory_complete=False,
            metadata_missing=data['metadata_missing'],
            subnets=rows, history=history, references=REFERENCES,
            limitations=[
                'Current lifecycle maps plus supplied prior IDs; fully purged '
                'historical generations require historical chain evidence.',
                'Immunity applies to automated subnet pruning, not financial '
                'safety or guaranteed returns.',
                'Reserves and weighted AMM metadata do not quote a realizable '
                'exit. No constant-product slippage or guaranteed recovery.',
                'Context time differs from wallet valuation time. Missing '
                'holder samples do not establish an empty subnet.',
                'Prior context history is bounded to 32 snapshots.'
            ])
    except Exception:
        raise WatchError('Subnet context stopped; no partial context '
                         'published.') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--block-hash')
    parser.add_argument('--endpoint-file', type=Path,
                        default=Path('dwellir.txt'))
    parser.add_argument('--rpc-limit', type=int, default=120)
    args = parser.parse_args(argv)
    chain = None
    previous_logging = logging.root.manager.disable
    try:
        _local_only()
        if not 10 <= args.rpc_limit <= 120:
            raise ValueError('invalid RPC limit')
        private_inputs = (args.endpoint_file, args.db)
        if args.out.resolve() in {p.resolve() for p in private_inputs if p}:
            raise ValueError('public output must differ from private inputs')
        previous = (json.loads(args.previous.read_text(encoding='utf8'))
                    if args.previous else None)
        known = set()
        if args.db:
            with closing(sqlite3.connect(args.db.resolve().as_uri()
                                         + '?mode=ro', uri=True)) as db:
                known = {r[0] for r in db.execute(
                    'SELECT DISTINCT netuid FROM native_holder_positions')}
        logging.disable(logging.CRITICAL)
        chain = _connect(_file_endpoint(args.endpoint_file), args.rpc_limit)
        result = collect(SubnetSource(chain), block_hash=args.block_hash,
                         known_netuids=known, previous=previous)
        _write(args.out, result)
        print(json.dumps(dict(
            finalized_block=result['finalized_block'],
            active_subnets=sum(r['active'] for r in result['subnets']),
            retained_inactive=sum(not r['active'] for r in result['subnets']),
            immune_subnets=sum(r['subnet_immune'] is True
                               for r in result['subnets']),
            rpc_requests=chain.requests)))
        return 0
    except Exception:
        print('Subnet context stopped; private connection details withheld.',
              file=sys.stderr)
    finally:
        if chain is not None:
            with suppress(Exception):
                chain.close()
        logging.disable(previous_logging)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
