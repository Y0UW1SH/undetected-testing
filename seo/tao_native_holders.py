"""Discover AlphaV2 share candidates at a pinned Finney block, locally only.

Run with ``python -m seo.tao_native_holders --db try_tao_out/holders.sqlite``.
Resume with the same DB; use a different DB for a fresh block. Completion means
the AlphaV2 storage map was traversed, not a complete holder census: unmigrated
legacy Alpha rows are excluded. Shares are never recorded as alpha balances.
"""
import argparse
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys

from seo.tao_local_watch import WatchError, _addr, _connect, _file_endpoint


PALLET = 'SubtensorModule'
PREFIX = ('0x658faa385070e074c85bf6b568cf0555'
          '421d8174603a6a6036511a7619b402f0')
HEX = re.compile(r'0x(?:[0-9a-f]{2})*')
HASH = re.compile(r'0x[0-9a-f]{64}')
UNRANKED = 2147483647


def _local_only():
    if os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS'):
        raise WatchError('Native Dwellir holder discovery runs only locally.')


def decode_key(key):
    """Validate both Blake2_128Concat accounts and the Identity u16 netuid."""
    if (not isinstance(key, str) or not HEX.fullmatch(key)
            or not key.startswith(PREFIX)):
        raise ValueError('invalid AlphaV2 storage key')
    tail = bytes.fromhex(key[len(PREFIX):])
    if len(tail) != 98:
        raise ValueError('invalid AlphaV2 key length')
    for offset in (0, 48):
        digest = tail[offset:offset + 16]
        account = tail[offset + 16:offset + 48]
        if hashlib.blake2b(account, digest_size=16).digest() != digest:
            raise ValueError('invalid AlphaV2 account hash')
    return (_addr(tail[16:48]), _addr(tail[64:96]),
            int.from_bytes(tail[96:], 'little'))


class ChainSource:
    """Bounded transport is supplied by tao_local_watch._connect's meter."""
    def __init__(self, chain):
        self.chain = chain
        self.entries = {}

    def _rpc(self, method, params):
        result = self.chain.rpc_request(method, params)
        if (not isinstance(result, dict) or 'error' in result
                or 'result' not in result):
            raise ValueError('invalid RPC envelope')
        return result['result']

    def finalized(self):
        block = self._rpc('chain_getFinalizedHead', [])
        if not isinstance(block, str) or not HASH.fullmatch(block):
            raise ValueError('invalid finalized block')
        header = self._rpc('chain_getHeader', [block])
        return block, int(header['number'], 16)

    def prepare(self, block):
        for name, types, hashers in (
                ('AlphaV2', ['AccountId', 'AccountId', 'u16'],
                 ['Blake2_128Concat', 'Blake2_128Concat', 'Identity']),
                ('AlphaShareEpoch', ['AccountId', 'AccountId', 'u16'],
                 ['Blake2_128Concat', 'Blake2_128Concat', 'Identity']),
                ('AlphaSharePoolEpoch', ['AccountId', 'u16'],
                 ['Blake2_128Concat', 'Identity'])):
            meta = self.chain.get_metadata_storage_function(
                PALLET, name, block_hash=block)
            if (meta is None or meta.get_param_info() != types
                    or meta.get_param_hashers() != hashers):
                raise ValueError('unsupported share storage metadata')
            storage = self.chain.create_storage_key(PALLET, name)
            prefix = storage.to_hex()
            if not isinstance(prefix, str) or not HASH.fullmatch(prefix):
                raise ValueError('invalid storage prefix')
            if name == 'AlphaV2' and prefix != PREFIX:
                raise ValueError('unexpected AlphaV2 prefix')
            if name != 'AlphaV2' and storage.decode_scale_value().value != 0:
                raise ValueError('unsupported epoch default')
            self.entries[name] = (prefix, meta.get_value_type_string())

    def page(self, block, after, limit):
        return self._rpc('state_getKeysPaged', [PREFIX, limit, after, block])

    def values(self, block, keys):
        # One batch includes each row's shares and epoch plus distinct pool
        # epochs. Construct from validated raw key bytes, never query_map's
        # incomplete tuple decoding of this three-component NMap.
        wanted, lookups = {}, {}
        for key in keys:
            decode_key(key)
            tail = key[len(PREFIX):]
            epoch = self.entries['AlphaShareEpoch'][0] + tail
            pool = (self.entries['AlphaSharePoolEpoch'][0]
                    + tail[:96] + tail[-4:])
            lookups[key] = (epoch, pool)
            wanted.update({key: 'AlphaV2', epoch: 'AlphaShareEpoch',
                           pool: 'AlphaSharePoolEpoch'})
        response = self._rpc('state_queryStorageAt', [list(wanted), block])
        if (not isinstance(response, list) or len(response) != 1
                or not isinstance(response[0], dict)
                or response[0].get('block') != block
                or not isinstance(response[0].get('changes'), list)):
            raise ValueError('invalid pinned storage response')
        decoded = {}
        for change in response[0]['changes']:
            if not isinstance(change, (list, tuple)) or len(change) != 2:
                raise ValueError('invalid storage change')
            key, raw = change
            if not isinstance(key, str) or key not in wanted or key in decoded:
                raise ValueError('unexpected storage change')
            name = wanted[key]
            if raw is None and name != 'AlphaV2':
                decoded[key] = 0  # Metadata default checked in prepare().
            elif isinstance(raw, str) and HEX.fullmatch(raw) and len(raw) > 2:
                decoded[key] = self.chain.decode_scale(
                    self.entries[name][1], raw, block_hash=block)
            else:
                raise ValueError('missing share or invalid storage value')
        if set(decoded) != set(wanted):
            raise ValueError('incomplete storage values')
        return {key: dict(shares=decoded[key], row_epoch=decoded[epoch],
                          pool_epoch=decoded[pool])
                for key, (epoch, pool) in lookups.items()}


def schema(db):
    # Keep the established queue schema without importing the browser stack
    # into the standalone substrate-interface watcher environment.
    db.execute('''CREATE TABLE IF NOT EXISTS holder_registry (
        netuid INTEGER NOT NULL, address TEXT NOT NULL, rank INTEGER NOT NULL,
        balance_units TEXT NOT NULL, first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL, PRIMARY KEY (netuid,address))''')
    db.execute('''CREATE TABLE IF NOT EXISTS native_holder_scan (
        id INTEGER PRIMARY KEY CHECK(id=1), block_hash TEXT NOT NULL,
        height INTEGER NOT NULL, cursor TEXT, pages INTEGER NOT NULL,
        complete INTEGER NOT NULL, started TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS native_holder_positions (
        storage_key TEXT PRIMARY KEY, hotkey TEXT NOT NULL,
        coldkey TEXT NOT NULL, netuid INTEGER NOT NULL,
        share_mantissa TEXT NOT NULL, share_exponent INTEGER NOT NULL,
        pool_epoch TEXT NOT NULL, row_epoch TEXT NOT NULL,
        status TEXT NOT NULL)''')


def _position(key, value):
    hot, cold, netuid = decode_key(key)
    share = value['shares']
    mantissa, exponent = share['mantissa'], share['exponent']
    pool, epoch = value['pool_epoch'], value['row_epoch']
    if (set(share) != {'mantissa', 'exponent'}
            or type(mantissa) is not int or not 0 <= mantissa < 2**128
            or type(exponent) is not int or not -(2**31) <= exponent < 2**31
            or type(pool) is not int or not 0 <= pool < 2**64
            or type(epoch) is not int or not 0 <= epoch < 2**64):
        raise ValueError('invalid share or epoch')
    status = ('zero_shares' if mantissa == 0 else
              'retired' if pool != 0 and epoch != pool else 'candidate')
    return (key, hot, cold, netuid, str(mantissa), exponent,
            str(pool), str(epoch), status)


def summary(db, reads=0):
    block, height, pages, complete = db.execute(
        'SELECT block_hash,height,pages,complete FROM native_holder_scan '
        'WHERE id=1').fetchone()
    counts = dict(db.execute('SELECT status,COUNT(*) FROM '
                             'native_holder_positions GROUP BY status'))
    wallets = db.execute("SELECT COUNT(DISTINCT coldkey) "
                         "FROM native_holder_positions "
                         "WHERE status='candidate'").fetchone()[0]
    pairs = db.execute("SELECT COUNT(*) FROM (SELECT DISTINCT netuid,coldkey "
                       "FROM native_holder_positions "
                       "WHERE status='candidate')").fetchone()[0]
    return dict(source='Finney SubtensorModule.AlphaV2', block_hash=block,
                finalized_block=height, pages=pages, page_reads_this_run=reads,
                complete=bool(complete), full_holder_census=False,
                storage_rows=sum(counts.values()), candidate_wallets=wallets,
                candidate_subnet_wallets=pairs,
                candidate_positions=counts.get('candidate', 0),
                retired_positions=counts.get('retired', 0),
                zero_share_positions=counts.get('zero_shares', 0),
                scope='Positive current-epoch AlphaV2 shares identify '
                      'research '
                      'candidates only. Legacy Alpha rows, alpha quantities, '
                      'holder rankings, valuation and history '
                      'are not covered.')


def scan(db, source, page_size=128, max_pages=4, stamp=None):
    """Commit each validated page and its queue candidates atomically."""
    _local_only()
    if (type(page_size) is not int or not 1 <= page_size <= 1000
            or type(max_pages) is not int or not 0 <= max_pages <= 1000):
        raise ValueError('use 1..1000 keys and 0..1000 pages')
    stamp = stamp or datetime.now(timezone.utc).isoformat()
    schema(db)
    saved = db.execute('SELECT block_hash,height,cursor,complete '
                       'FROM native_holder_scan WHERE id=1').fetchone()
    if saved and saved[3]:
        return summary(db)
    reads = 0
    try:
        if saved:
            block, height, cursor, _ = saved
        else:
            block, height = source.finalized()
            cursor = None
        if (not isinstance(block, str) or not HASH.fullmatch(block)
                or type(height) is not int or height < 0):
            raise ValueError('invalid pinned block')
        source.prepare(block)
        if not saved:
            db.execute('SAVEPOINT native_holder_pin')
            db.execute('INSERT INTO native_holder_scan VALUES '
                       '(1,?,?,NULL,0,0,?)',
                       (block, height, stamp))
            db.execute('RELEASE native_holder_pin')
        for _ in range(max_pages):
            keys = source.page(block, cursor, page_size)
            reads += 1
            if (not isinstance(keys, list) or len(keys) > page_size
                    or not all(isinstance(k, str) for k in keys)
                    or keys != sorted(set(keys))
                    or (cursor is not None and keys and keys[0] <= cursor)):
                raise ValueError('invalid ordered storage page')
            for key in keys:
                decode_key(key)
            values = source.values(block, keys) if keys else {}
            if not isinstance(values, dict) or set(values) != set(keys):
                raise ValueError('incomplete share page')
            rows = [_position(key, values[key]) for key in keys]
            db.execute('SAVEPOINT native_holder_page')
            try:
                db.executemany('INSERT INTO native_holder_positions '
                               'VALUES (?,?,?,?,?,?,?,?,?)', rows)
                for _, _, cold, netuid, _, _, _, _, status in rows:
                    if status == 'candidate':
                        db.execute('''INSERT INTO holder_registry
                            (netuid,address,rank,balance_units,first_seen,last_seen)
                            VALUES (?,?,?,'unknown',?,?)
                            ON CONFLICT(netuid,address) DO NOTHING''',
                                   (netuid, cold, UNRANKED, stamp, stamp))
                db.execute('UPDATE native_holder_scan SET cursor=?,'
                           'pages=pages+?,complete=? WHERE id=1',
                           (keys[-1] if keys else cursor,
                            int(bool(keys)), int(not keys)))
            except Exception:
                db.execute('ROLLBACK TO native_holder_page')
                db.execute('RELEASE native_holder_page')
                raise
            db.execute('RELEASE native_holder_page')
            if not keys:
                break
            cursor = keys[-1]
    except Exception:
        # Even remote exception messages and data can contain endpoint secrets.
        raise WatchError('Native holder scan stopped; '
                         'last verified page retained.') from None
    return summary(db, reads)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--endpoint-file', type=Path,
                        default=Path('dwellir.txt'))
    parser.add_argument('--page-size', type=int, default=128)
    parser.add_argument('--max-pages', type=int, default=4)
    parser.add_argument('--rpc-limit', type=int, default=80)
    args = parser.parse_args(argv)
    chain = None
    previous_logging = logging.root.manager.disable
    try:
        _local_only()
        if (not 1 <= args.page_size <= 1000 or not 1 <= args.max_pages <= 1000
                or not 10 <= args.rpc_limit <= 2000):
            raise WatchError('Use 1..1000 keys/pages '
                             'and 10..2000 RPC requests.')
        logging.disable(logging.CRITICAL)
        endpoint = _file_endpoint(args.endpoint_file)
        chain = _connect(endpoint, args.rpc_limit)
        args.db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(args.db) as db:
            result = scan(db, ChainSource(chain), args.page_size,
                          args.max_pages)
        print(json.dumps(result))
        return 0
    except WatchError as exc:
        print(exc, file=sys.stderr)
    except Exception:
        print('Native holder discovery stopped; '
              'no private connection details logged.', file=sys.stderr)
    finally:
        if chain is not None:
            with suppress(Exception):
                chain.close()
        logging.disable(previous_logging)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
