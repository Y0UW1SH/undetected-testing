"""Bounded TAO.com HTTP observations, not reconciled wallet history."""
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPRedirectHandler, Request

BASE = 'https://api.tao.com/data/v1'


class SourceError(ValueError):
    """A failed or unsupported observation, never zero activity."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceError('Duplicate JSON field from provider.')
        result[key] = value
    return result


def _constant(value):
    raise SourceError('Non-finite JSON number from provider.')


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise SourceError('Non-finite JSON number from provider.')
    return result


class Client:
    def __init__(self, *, open_url=None, timeout=30, delay=1,
                 max_bytes=5 * 1024 * 1024):
        self.open_url = open_url or build_opener(NoRedirect()).open
        self.timeout, self.delay, self.max_bytes = timeout, delay, max_bytes
        self.last_request = None

    def get(self, path, params=None):
        if (not re.fullmatch(r'/[A-Za-z0-9/_-]+', path)
                or '//' in path):
            raise SourceError('Only fixed API paths are allowed.')
        url = BASE + path
        if params:
            url += '?' + urlencode(params)
        if self.last_request is not None:
            time.sleep(max(0, self.delay - (time.monotonic()
                                            - self.last_request)))
        started = time.monotonic()
        self.last_request = started
        try:
            request = Request(url, headers={
                'Accept': 'application/hal+json, application/json',
                'User-Agent': 'LocalTaoObserver/1.0'})
            with self.open_url(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise SourceError('Provider HTTP %s.' % response.status)
                kind = response.headers.get('Content-Type', '').lower()
                if 'json' not in kind:
                    raise SourceError('Provider did not return JSON.')
                raw = response.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise SourceError('Provider response exceeds size limit.')
            body_text = raw.decode('utf8')
            data = json.loads(body_text, parse_constant=_constant,
                              parse_float=_float,
                              object_pairs_hook=_pairs)
            if not isinstance(data, dict):
                raise SourceError('Provider JSON must be an object.')
        except HTTPError as exc:
            raise SourceError('Provider HTTP %s; prior report kept.' %
                              exc.code) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise SourceError('Provider request failed; prior report kept.')
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, SourceError):
                raise
            raise SourceError('Invalid provider JSON; prior report kept.')
        return dict(url=url,
                    received_at=datetime.now(timezone.utc).isoformat(),
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    http_status=200,
                    body_sha256=hashlib.sha256(raw).hexdigest(),
                    body_text=body_text, data=data)


def pages(client, path, collection, *, page_count, page_size):
    """Keep page receipts. Feed exhaustion is not history verification."""
    if not 1 <= page_count <= 5 or not 1 <= page_size <= 100:
        raise SourceError('Choose 1-5 pages and 1-100 rows per page.')
    if collection not in ('trades', 'transactions'):
        raise SourceError('Unsupported collection.')
    receipts, seen, cursor = [], set(), None
    count = 0
    for index in range(page_count):
        params = {'limit': page_size}
        if collection == 'transactions':
            params.update(offset=index * page_size, type='all')
        elif cursor is not None:
            params['cursor'] = cursor
        receipt = client.get(path, params)
        data = receipt['data']
        embedded = data.get('_embedded')
        pagination = data.get('pagination')
        if not isinstance(embedded, dict) or not isinstance(pagination, dict):
            raise SourceError('Provider page schema changed.')
        rows = embedded.get(collection)
        if (not isinstance(rows, list) or len(rows) > page_size
                or not all(isinstance(row, dict) for row in rows)
                or pagination.get('limit') != page_size):
            raise SourceError('Provider page rows or limit invalid.')
        if collection == 'trades':
            if 'next_cursor' not in pagination:
                raise SourceError('Provider cursor is missing.')
            cursor = pagination['next_cursor']
            if cursor is not None:
                if not isinstance(cursor, str) or not 0 < len(cursor) <= 4096:
                    raise SourceError('Provider cursor invalid.')
                if cursor in seen:
                    raise SourceError('Provider repeated cursor.')
                seen.add(cursor)
            more = cursor is not None
        else:
            more = pagination.get('has_more')
            if (type(more) is not bool
                    or pagination.get('offset') != index * page_size
                    or pagination.get('count') != len(rows)):
                raise SourceError('Provider transaction pagination invalid.')
        if more and not rows:
            raise SourceError('Empty provider page claims more data.')
        receipts.append(receipt)
        count += len(rows)
        if not more:
            break
    return dict(receipts=receipts, rows_observed=count,
                provider_has_more=more, history_complete=False,
                coverage='bounded_observation',
                note='No snapshot guarantee or ledger reconciliation.')
