"""The HTTP path preserves provenance without touching recovered ledgers."""
import hashlib
import json

import pytest

from seo import local_api, local_runner
from seo.sources import tao_api

ADDRESS = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'


class Client:
    def get(self, path, params=None):
        data = {'updated_at': '2026-11-09T00:00:00Z'}
        if path == '/tao/price/latest':
            data['price_usd'] = '200.12345'
        elif path.endswith('/trades'):
            data = {'_embedded': {'trades': [{'event_id': '1:2:3'}]},
                    'pagination': {'limit': params['limit'],
                                   'next_cursor': None}}
        elif path.endswith('/transactions'):
            data = {'_embedded': {'transactions': []},
                    'pagination': {'limit': params['limit'],
                                   'offset': 0, 'has_more': False, 'count': 0}}
        elif path.endswith('/balance/latest'):
            data = {'address': ADDRESS, 'total_balance_rao': '1234567890123',
                    'last_updated': '2026-11-09T00:00:00Z',
                    'stake_positions': []}
        else:
            data.update(netuid=1, name='<script>unsafe</script>',
                        financials={'price_tao': '0.005'})
        text = json.dumps(data)
        return dict(url=tao_api.BASE + path, http_status=200,
                    received_at='2026-11-09T00:00:01Z', body_text=text,
                    body_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    data=data)


def test_api_observation_preserves_ledger_and_source_receipts(tmp_path):
    local_runner.initialize(tmp_path)
    ledger = tmp_path / 'wallets.sqlite'
    ledger.write_bytes(b'existing-data-do-not-touch')
    assert local_api.run(tmp_path, netuid=1, address=ADDRESS, pages=1,
                         page_size=3, client=Client()) == 0
    assert ledger.read_bytes() == b'existing-data-do-not-touch'
    report = json.loads((tmp_path / 'reports/tao_api.json').read_text())
    assert report['ledger_modified'] is False
    assert report['history_complete'] is False
    assert report['wallet']['balance']['data']['total_balance_rao'] == (
        '1234567890123')
    assert 'body_text' not in report['price']
    run = tmp_path / report['evidence_directory']
    manifest = json.loads((run / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        assert hashlib.sha256((run / name).read_bytes()
                              ).hexdigest() == expected
    assert len(list((run / 'responses').glob('[0-9][0-9][0-9].json'))) == 5
    rendered = (run / 'tao_api.html').read_text()
    assert '<script>unsafe</script>' not in rendered
    assert '&lt;script&gt;' in rendered
    assert 'not verified' in rendered.lower()


def test_api_failed_run_keeps_last_good_report_and_receipts(tmp_path):
    local_runner.initialize(tmp_path)
    path = tmp_path / 'reports/tao_api.json'
    path.write_text('{"old":"good"}')

    class Broken(Client):
        def get(self, path, params=None):
            if path.endswith('/trades'):
                raise tao_api.SourceError('Provider HTTP 429.')
            return super().get(path, params)
    assert local_api.run(tmp_path, netuid=1, pages=1, page_size=3,
                         client=Broken()) == 1
    assert path.read_text() == '{"old":"good"}'
    status = json.loads((tmp_path / 'status/api-collect.json').read_text())
    assert status['status'] == 'failed'
    assert len(list((tmp_path / status['evidence_directory']
                     / 'responses').glob('[0-9][0-9][0-9].json'))) == 2


def test_no_implicit_wallet_is_loaded(tmp_path):
    local_runner.initialize(tmp_path)
    assert local_api.run(tmp_path, netuid=1, pages=1, page_size=3,
                         client=Client()) == 0
    report = json.loads((tmp_path / 'reports/tao_api.json').read_text())
    assert report['wallet'] is None


def test_api_command_passes_only_explicit_bounded_options(
        tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(local_api, 'run', lambda *a, **k:
                        calls.append((a, k)) or 0)
    assert local_runner.main(['--root', str(tmp_path), 'api-collect',
                              '--netuid', '1', '--pages', '2',
                              '--page-size', '3']) == 0
    assert calls[0][1] == dict(netuid=1, address=None, pages=2, page_size=3,
                               wait=0)


@pytest.mark.parametrize('option,value', [('--pages', '0'), ('--pages', '6'),
                                          ('--page-size', '101'),
                                          ('--netuid', '-1'),
                                          ('--address', '../elsewhere')])
def test_invalid_arguments_fail_before_network(tmp_path, option, value):
    with pytest.raises(SystemExit):
        local_runner.main(['--root', str(tmp_path), 'api-collect',
                           option, value])
    assert not (tmp_path / 'runs').exists()


def test_backup_and_restore_keep_api_receipts(tmp_path):
    from seo import local_state
    root, restored = tmp_path / 'active', tmp_path / 'restored'
    assert local_api.run(root, netuid=1, pages=1, page_size=3,
                         client=Client()) == 0
    saved = local_state.backup(root)
    local_state.restore(saved, restored)
    report = json.loads((restored / 'reports/tao_api.json').read_text())
    receipt = report['price']
    raw = restored / report['evidence_directory'] / receipt['raw_file']
    assert hashlib.sha256(
        raw.read_bytes()).hexdigest() == receipt['body_sha256']


def test_only_one_current_report_is_published(tmp_path, monkeypatch):
    from seo import local_state
    original = local_state.atomic_write

    def reject_second_current_file(path, data):
        if str(path).endswith('/reports/tao_api.html'):
            raise OSError('Two-file publication is not atomic')
        return original(path, data)
    monkeypatch.setattr(local_state, 'atomic_write',
                        reject_second_current_file)
    assert local_api.run(tmp_path, netuid=1, pages=1, page_size=3,
                         client=Client()) == 0
    report = json.loads((tmp_path / 'reports/tao_api.json').read_text())
    assert (tmp_path / report['evidence_directory'] / 'tao_api.html').is_file()
