"""Replay saved wallet data with explicit local native evidence, offline."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from seo import tao_collect, tao_ledger, tao_performance, tao_view
from seo.sources import tao_prices
from seo.storage import Storage
from seo.tao_native_evidence import growth_evidence, stake_units
from seo.tao_wallets import _parse_ts


def _history(db, address, end, cutoff):
    rows = db.execute('SELECT checked_at,payload FROM wallet_performance '
                      'WHERE address=? ORDER BY checked_at DESC', (address,))
    for stamp, raw in rows:
        wallet = json.loads(raw)
        coverage = wallet.get('coverage') or {}
        if (wallet.get('address') == address
                and coverage.get('transactions') == 'complete'
                and _parse_ts(stamp) <= _parse_ts(cutoff)
                and _parse_ts(stamp) >= _parse_ts(end)):
            return dict(coverage, address=address, checked_at=stamp)
    raise ValueError('Complete attributed history missing for growth proof')


def _growth(db, states, events, stamp):
    groups = {}
    for row in states:
        groups.setdefault((row['coldkey'], row['hotkey'], row['netuid']),
                          []).append(row)
    result = []
    for rows in groups.values():
        ordered = sorted(rows, key=lambda r: r['block'])
        for before, after in zip(ordered, ordered[1:]):
            if _parse_ts(after['time']) > _parse_ts(stamp):
                raise ValueError('Growth evidence is later than the snapshot')
            if before['storage'].get('AlphaV2') != after['storage'].get(
                    'AlphaV2') or not stake_units(before):
                continue
            if stake_units(after) <= stake_units(before):
                continue
            result.append(growth_evidence(
                before, after, events,
                _history(db, before['coldkey'], after['time'], stamp)))
    if states and not result:
        raise ValueError('No supported growth interval in native states')
    return result


def _prices(store, report):
    # No ambient price database: preserve the source artifact's own quotes.
    tao_prices._schema(store.conn)
    chart = tao_prices._stored(store.conn)
    registered = {p['netuid']: p['registered_at'] for p in chart}
    marks = [dict(p, registered_at=registered.get(p['netuid']))
             for p in store.rows('subnet_stats')
             if p['netuid'] not in registered
             or _parse_ts(p['fetched_at']) >= _parse_ts(
                 registered[p['netuid']])]
    by_key = {(p['netuid'], p['fetched_at']): p for p in marks}
    for p in chart:
        by_key.setdefault((p['netuid'], p['fetched_at']), p)
    for wallet in report['performance']['wallets']:
        for pos in wallet.get('positions', []):
            if pos.get('price_tao') is not None and pos.get('quote_at'):
                key = pos['netuid'], pos['quote_at']
                by_key[key] = dict(by_key.get(key, {}), netuid=key[0],
                                   fetched_at=key[1],
                                   price_tao=pos['price_tao'])
    return [p for p in by_key.values() if _parse_ts(p['fetched_at'])
            <= _parse_ts(report['checked_at'])]


def replay(report_path, db_path, output, states, moves, anchors=()):
    """Never alter downloaded artifacts or represent a replay as a new scan.

Native states and receipt files are explicitly selected local evidence. An
anchor is a previously verified native account/subnet inventory checkpoint.
This command does not query Dwellir or dispatch GitHub Actions.
"""
    if os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Native evidence replay is local only')
    report_path, db_path, output = map(Path, (report_path, db_path, output))
    target = output / 'wallets.sqlite'
    json_path = output / 'tao_collect.json'
    if target.resolve() == db_path.resolve() or (
            json_path.resolve() == report_path.resolve()):
        raise ValueError('Replay output must differ from downloaded inputs')
    data = tao_view.load_snapshot(report_path)
    output.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro',
                                 uri=True)) as source:
        with closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
    store = Storage(str(target))
    try:
        events, checkpoints = tao_collect._read(store, explicit=moves)
        stamp = data['checked_at']
        cutoff = _parse_ts(stamp)
        if 'events' in data:
            ids = {e[k] for e in data['events']
                   for k in ('id', 'paired_event_id') if e.get(k)}
            events = [e for e in events if e['id'] in ids]
        else:
            events = [e for e in events if _parse_ts(e['time']) <= cutoff]
        # Account snapshots can be observed minutes after collection starts.
        # Preserve the source batch, while excluding any later downloaded run.
        checkpoints = [p for p in checkpoints if p.get('batch') == stamp
                       or (_parse_ts(p['time']) <= cutoff
                           and (not p.get('batch') or _parse_ts(
                               p['batch']) <= cutoff))]
        for p in anchors:
            if (p.get('source') != 'local-finney-storage'
                    or _parse_ts(p['time']) > cutoff
                    or not p.get('netuid_scope')):
                raise ValueError('Invalid explicit native inventory anchor')
            anchor = dict(p, batch='native:' + str(p['block_number']),
                          units={int(n): q for n, q in p['units'].items()})
            checkpoints.append(anchor)
        growth = _growth(store.conn, states, events, stamp)
        supplied = {p.get('extrinsic_id') for p in moves}
        verified = {e.get('extrinsic') for e in events
                    if e.get('evidence') == 'native_move'} & supplied
        inferred = tao_ledger.inferred_links(checkpoints, events)
        sales, transfers, subnet_stats = [], [], {}
        positions, stats = tao_ledger.portfolio(
            events + inferred, checkpoints, growth_events=growth,
            subnet_stats=subnet_stats, sale_details=sales,
            transfer_details=transfers)
        prices = _prices(store, data)
        performance = tao_performance.record_run(
            store, stamp, positions, stats, subnet_stats, events + inferred,
            prices, data['coverage'], **data['performance']['settings'])
        traders = [dict(address=a, **s, win_rate=s['wins'] / s['priced_sales'])
                   for a, s in stats.items() if s['priced_sales']]
        traders.sort(key=lambda w: (w['realized_tao'], w['priced_sales']),
                     reverse=True)
        result = dict(
            data, performance=performance, positions=list(positions.values()),
            events=events, traders=traders, sale_details=sales,
            transfer_details=transfers,
            timing_evidence=tao_prices.timing_evidence(events, prices, stamp),
            local_evidence=dict(
                verified_at=datetime.now(timezone.utc).isoformat(),
                source_snapshot=stamp,
                source_report_sha256=hashlib.sha256(
                    report_path.read_bytes()).hexdigest(),
                growth_intervals=growth, verified_moves=len(verified),
                supplied_move_receipts=len(moves),
                inventory_anchors=len(anchors),
                scope='Local accounting replay at original inventory and '
                      'quote timestamps. No new balance or price scan. '
                      'Native share growth is checked against complete '
                      'indexed Taostats history; it is not a zero-cost buy.'))
        store.conn.commit()
        tao_view._write(json_path, json.dumps(result, indent=2,
                                              allow_nan=False))
        tao_view._write(output / 'wallets.html', tao_view.render(result))
        return result
    finally:
        store.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--db', type=Path, required=True)
    p.add_argument('--states', type=Path)
    p.add_argument('--moves', type=Path)
    p.add_argument('--anchor', action='append', type=Path, default=[])
    p.add_argument('--out', type=Path, default=Path('github_out/local-native'))
    args = p.parse_args(argv)

    def read(path):
        return json.loads(path.read_text(encoding='utf8'))

    result = replay(args.report, args.db, args.out,
                    read(args.states) if args.states else [],
                    read(args.moves) if args.moves else [],
                    [read(path) for path in args.anchor])
    print(json.dumps(dict(snapshot=result['checked_at'],
                          growth_intervals=len(result['local_evidence'][
                              'growth_intervals']),
                          report=str((args.out / 'wallets.html').resolve()))))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
