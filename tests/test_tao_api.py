"""Bounded public API observations, separate from the wallet ledger."""
import io
import json
from urllib.error import HTTPError

import pytest

from seo.sources import tao_api as api


class Response(io.BytesIO):
    status = 200
    headers = {'Content-Type': 'application/hal+json'}


def client_for(body, **kw):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return api.Client(open_url=lambda request, timeout: Response(raw),
                      delay=0, **kw)


def test_receipt_preserves_json_units_and_hash():
    data = {'amount_rao': '9007199254740993'}
    receipt = client_for(data).get('/tao/price/latest')
    assert receipt['data'] == data
    assert receipt['url'] == api.BASE + '/tao/price/latest'
    assert len(receipt['body_sha256']) == 64
    assert receipt['http_status'] == 200


@pytest.mark.parametrize('body', [b'[]', b'<html>no</html>',
                                  b'{"amount":NaN}', b'{"amount":1e999}',
                                  b'{"x":1,"x":2}'])
def test_bad_json_is_not_success(body):
    with pytest.raises(api.SourceError):
        client_for(body).get('/tao/price/latest')


def test_oversized_response_is_rejected():
    with pytest.raises(api.SourceError, match='size'):
        client_for({'x': 'a' * 100}, max_bytes=50).get('/tao/price/latest')


@pytest.mark.parametrize('path', ['https://elsewhere/x', '//elsewhere/x',
                                  '/../../secret', '/tao/price?secret=x'])
def test_fixed_origin_cannot_be_overridden(path):
    with pytest.raises(api.SourceError):
        client_for({}).get(path)


def test_http_failure_is_not_empty_data():
    def fail(request, timeout):
        raise HTTPError(request.full_url, 429, 'rate limited', {}, None)
    client = api.Client(open_url=fail, delay=0)
    with pytest.raises(api.SourceError, match='429'):
        client.get('/tao/price/latest')


def test_redirect_handler_refuses_to_follow():
    handler = api.NoRedirect()
    assert handler.redirect_request(None, None, 302, 'moved', {},
                                    'https://elsewhere/') is None


def trade_page(cursor, ids):
    return {'_embedded': {'trades': [dict(event_id=i) for i in ids]},
            'pagination': {'limit': 2, 'next_cursor': cursor}}


class Feed:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return {'data': next(self.values)}


def test_cursor_is_encoded_and_request_count_is_bounded():
    client = Feed([trade_page('opaque/+=', ['1', '2']),
                   trade_page('later', ['3'])])
    result = api.pages(client, '/bittensor/subnets/1/trades', 'trades',
                       page_count=2, page_size=2)
    assert len(result['receipts']) == 2
    assert result['rows_observed'] == 3
    assert result['provider_has_more'] is True
    assert result['history_complete'] is False
    assert client.calls[1][1] == {'limit': 2, 'cursor': 'opaque/+='}


def test_exhausted_feed_still_does_not_claim_complete_history():
    result = api.pages(Feed([trade_page(None, [])]), '/x', 'trades',
                       page_count=2, page_size=2)
    assert result['provider_has_more'] is False
    assert result['history_complete'] is False
    assert len(result['receipts']) == 1


def test_repeated_cursor_is_failure():
    with pytest.raises(api.SourceError, match='cursor'):
        api.pages(Feed([trade_page('again', ['1']),
                        trade_page('again', ['2'])]), '/x', 'trades',
                  page_count=3, page_size=2)


def test_schema_failure_is_not_zero_activity():
    with pytest.raises(api.SourceError):
        api.pages(Feed([{'error': 'oops'}]), '/x', 'trades',
                  page_count=1, page_size=2)


def test_transaction_offset_stays_bounded_and_no_links_are_followed():
    def page(offset):
        return {'_embedded': {'transactions': [{'hash': 'x'}]},
                '_links': {'next': {'href': 'https://bad/'}},
                'pagination': {'limit': 2, 'offset': offset,
                               'count': 1, 'has_more': True}}
    client = Feed([page(0), page(2)])
    result = api.pages(client, '/accounts/example/transactions',
                       'transactions', page_count=2, page_size=2)
    assert client.calls[1][1] == {'limit': 2, 'offset': 2, 'type': 'all'}
    assert result['history_complete'] is False


def test_truncated_http_body_is_source_failure():
    from http.client import IncompleteRead

    class BrokenResponse(Response):
        def read(self, size):
            raise IncompleteRead(b'{"partial":')
    client = api.Client(open_url=lambda *a, **k: BrokenResponse(), delay=0)
    with pytest.raises(api.SourceError, match='request failed'):
        client.get('/tao/price/latest')
