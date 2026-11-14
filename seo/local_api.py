"""Separate local API evidence with bounded coverage and no ledger writes."""
import argparse
from decimal import Decimal, InvalidOperation
import html
import json
from pathlib import Path
import re

from seo import local_state as state
from seo.sources import tao_api

NOTICE = ('Bounded source observations only. History and trading P&L are not '
          'verified. Account transactions can omit legs of multi-leg '
          'extrinsics. No wallet ledger is changed; no ranking is inferred.')


def bounded(low, high):
    def parse(value):
        try:
            result = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError('Choose an integer.')
        if not low <= result <= high:
            raise argparse.ArgumentTypeError('Choose %d-%d.' % (low, high))
        return result
    return parse


def address_arg(value):
    if not re.fullmatch(r'5[1-9A-HJ-NP-Za-km-z]{47}', value):
        raise argparse.ArgumentTypeError(
            'Use a complete SS58 account address.')
    return value


def _amount(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise InvalidOperation
    except InvalidOperation:
        raise tao_api.SourceError('Provider amount is missing or invalid.')


class RecordingClient:
    def __init__(self, client, folder):
        self.client, self.folder, self.count = client, folder, 0
        self.folder.mkdir()

    def get(self, path, params=None):
        receipt = dict(self.client.get(path, params))
        raw = receipt.pop('body_text').encode('utf8')
        self.count += 1
        file = self.folder / ('%03d.json' % self.count)
        state.atomic_write(file, raw)
        if state.digest(file) != receipt['body_sha256']:
            raise tao_api.SourceError('Response checksum mismatch.')
        receipt['raw_file'] = 'responses/' + file.name
        state.atomic_write(file.with_suffix('.meta.json'),
                           json.dumps({k: v for k, v in receipt.items()
                                       if k != 'data'}, indent=2))
        return receipt


def observe(client, *, netuid, address, pages, page_size):
    price = client.get('/tao/price/latest')
    _amount(price['data'].get('price_usd'))
    if not price['data'].get('updated_at'):
        raise tao_api.SourceError('Provider price timestamp is missing.')
    path = '/bittensor/subnets/%d' % netuid
    subnet = client.get(path)
    data = subnet['data']
    if (data.get('netuid') != netuid
            or not isinstance(data.get('financials'), dict)
            or not data.get('updated_at')):
        raise tao_api.SourceError('Provider subnet schema changed.')
    _amount(data['financials'].get('price_tao'))
    trades = tao_api.pages(client, path + '/trades', 'trades',
                           page_count=pages, page_size=page_size)
    wallet = None
    if address:
        path = '/accounts/' + address
        balance = client.get(path + '/balance/latest')
        data = balance['data']
        if (data.get('address') != address or not data.get('last_updated')
                or not isinstance(data.get('stake_positions'), list)
                or not re.fullmatch(
                    r'\d+', str(data.get('total_balance_rao')))):
            raise tao_api.SourceError(
                'Provider account balance schema changed.')
        transactions = tao_api.pages(client, path + '/transactions',
                                     'transactions', page_count=pages,
                                     page_size=page_size)
        wallet = dict(address=address, balance=balance,
                      transactions=transactions)
    return dict(schema_version=1, source='TAO.com', price=price, subnet=subnet,
                trades=trades, wallet=wallet, history_complete=False,
                ledger_modified=False, notice=NOTICE)


def render(report):
    def esc(value):
        return html.escape(str(value))

    subnet = report['subnet']['data']
    parts = ['<!doctype html><meta charset="utf-8">'
             '<title>Local TAO API</title>',
             '<h1>TAO.com local observation</h1>',
             '<p><strong>%s</strong></p>' % esc(NOTICE),
             '<p>Run: %s</p>' % esc(report['run']),
             '<p>TAO/USD: %s (provider timestamp %s)</p>' % (
                 esc(report['price']['data']['price_usd']),
                 esc(report['price']['data']['updated_at'])),
             '<p>SN%s %s: %s TAO (provider timestamp %s)</p>' % (
                 esc(subnet['netuid']), esc(subnet.get('name', '')),
                 esc(subnet['financials']['price_tao']),
                 esc(subnet['updated_at']))]
    for title, feed in [('Subnet trades', report['trades'])] + (
            [('Account transactions', report['wallet']['transactions'])]
            if report['wallet'] else []):
        parts.append('<p>%s: %s rows in %s pages. Provider has more: %s. '
                     'Historical coverage is not verified.</p>' % (
                         title, feed['rows_observed'], len(feed['receipts']),
                         feed['provider_has_more']))
    parts.append('<p>The matching JSON report contains raw units, timestamps '
                 'and evidence references. Repeated pages may include '
                 'duplicates. Counts are observations, not unique trades.</p>')
    return '\n'.join(parts)


def run(root, *, netuid, address=None, pages=1, page_size=10, wait=0,
        client=None):
    # Validate direct callers as well as argparse before initializing state.
    bounded(0, 65535)(str(netuid))
    bounded(1, 5)(str(pages))
    bounded(1, 100)(str(page_size))
    if address:
        address_arg(address)
    from seo.local_runner import initialize
    root = Path(root).resolve()
    initialize(root)
    with state.lease(root, timeout=wait):
        staging = root / 'snapshots/api' / (state.stamp() + '-api')
        staging.mkdir(parents=True)
        evidence_directory = staging.relative_to(root).as_posix()
        status = dict(job='api-collect', run=staging.name, status='running',
                      evidence_directory=evidence_directory,
                      started_at=state.stamp())
        status_path = root / 'status/api-collect.json'
        state.atomic_write(status_path, json.dumps(status, indent=2))
        try:
            recorder = RecordingClient(client or tao_api.Client(),
                                       staging / 'responses')
            report = observe(recorder, netuid=netuid, address=address,
                             pages=pages, page_size=page_size)
            report.update(run=staging.name, observed_at=state.stamp(),
                          evidence_directory=evidence_directory)
            data = json.dumps(report, indent=2, ensure_ascii=False,
                              allow_nan=False)
            rendered = render(report)
            state.atomic_write(staging / 'tao_api.json', data)
            state.atomic_write(staging / 'tao_api.html', rendered)
            files = {p.relative_to(staging).as_posix(): state.digest(p)
                     for p in sorted(staging.rglob('*')) if p.is_file()}
            state.atomic_write(staging / 'manifest.json', json.dumps(
                dict(schema_version=1, files=files), indent=2))
            state.atomic_write(root / 'reports/tao_api.json', data)
            status.update(status='observed', exit_code=0)
            print(
                'API observations saved: %s' %
                (staging / 'tao_api.html'))
            print(NOTICE)
        except (tao_api.SourceError, OSError) as exc:
            status.update(status='failed', exit_code=1,
                          error=(str(exc)
                                 if isinstance(exc, tao_api.SourceError)
                                 else 'Could not save API evidence.'))
            print(status['error'])
        except KeyboardInterrupt:
            status.update(status='interrupted', exit_code=130)
        finally:
            status['finished_at'] = state.stamp()
            state.atomic_write(status_path, json.dumps(status, indent=2))
        return status['exit_code']
