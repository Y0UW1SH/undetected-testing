"""Bounded, resumable reads of the account table's observed source history.

The table uses mutable page numbers, not an immutable API cursor. A checkpoint
therefore records event anchors as well as a page hint. Every run refreshes the
head and validates an overlapping frontier before extending older coverage.
The caller must commit returned events and the checkpoint in one transaction.
``complete`` describes the observed table, never an independent chain census.
"""
from copy import deepcopy
import math
import re
import time

from seo.sources import tao_history as history
from seo.sources.tao_holders import FILL_JUMP, CONFIRM_JUMP
from seo.sources.taostats_wallets import account_url


SELECT_PAGE = """
(() => {
  const target = PAGE;
  for (let n = document.querySelector('.ag-root-wrapper'); n;
       n = n.parentElement) {
    const buttons = [...n.querySelectorAll('button')];
    if (!buttons.some(b => b.innerText.trim() === 'Next')) continue;
    const page = buttons.find(b => b.innerText.trim() === String(target)
      && b.getAttribute('aria-haspopup') !== 'menu' && !b.disabled
      && b.getClientRects().length);
    if (page) { page.click(); return true; }
    const jump = buttons.find(b => b.innerText.trim() === '...');
    if (!jump || jump.disabled) return false;
    jump.click(); return 'jump';
  }
  return false;
})()
"""


def _integer(value):
    if isinstance(value, bool) or not str(value).isdigit():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _ids(rows):
    return [row['id'] for row in rows]


def _valid_ids(ids):
    return (isinstance(ids, list) and bool(ids)
            and all(isinstance(i, str) for i in ids))


def _metadata(result, number, previous=None):
    raw = result.get('data')
    paging = result.get('pagination')
    if not isinstance(raw, list) or not isinstance(paging, dict):
        return None
    total = _integer(paging.get('total_items'))
    current = _integer(paging.get('current_page'))
    last = _integer(paging.get('total_pages'))
    if total is None or current != number:
        return None
    size_raw = paging.get('per_page')
    if size_raw is None:
        size_raw = paging.get('limit')
    size = _integer(size_raw)
    if size_raw is None:
        if previous is not None:
            size = previous['page_size']
        elif number == 1 and last is not None:
            # A full first page plus matching source counts establishes its
            # observed width without assuming the site's usual ten rows.
            observed = len(raw)
            if observed and math.ceil(total / observed) == last:
                size = observed
            elif total == 0 and not raw and last in (0, 1):
                size = 1  # Empty source: no offsets will use this width.
    if not size:
        return None
    calculated = math.ceil(total / size)
    if last is None:
        last = calculated
    if last != calculated and not (total == 0 and last == 1):
        return None
    last = max(1, last)
    if number > last:
        return None
    return dict(page_size=size, total_items=total, total_pages=last)


def _page(result, number, address, tab, meta):
    """A malformed or partially normalized page cannot extend coverage."""
    if meta is None:
        return None
    raw = result['data']
    size, total = meta['page_size'], meta['total_items']
    expected = min(size, max(0, total - (number - 1) * size))
    if len(raw) != expected:
        return None
    rows = [history.normalize(row, tab, address) for row in raw]
    if any(row is None for row in rows):
        return None
    ids = _ids(rows)
    times = [row['time'] for row in rows]
    if len(set(ids)) != len(ids) or times != sorted(times, reverse=True):
        return None
    return rows


def _pagination_diagnostic(result):
    paging = result.get('pagination')
    if not isinstance(paging, dict):
        return dict(keys=[], integer_fields={})
    keys = sorted({str(key) if re.fullmatch(r'[A-Za-z_]\w{0,59}', str(key))
                   else '<other>' for key in paging})[:30]
    integers = {key: _integer(paging.get(key)) for key in (
        'current_page', 'total_pages', 'total_items', 'per_page', 'limit')}
    observed = {key: value for key, value in integers.items()
                if value is not None}
    return dict(keys=keys, integer_fields=observed)


def _checkpoint(value, address, tab):
    initial = dict(version=1, address=address, tab=tab, frontier_page=0,
                   anchor_ids=[], head_anchor_ids=[], complete=False)
    if not isinstance(value, dict):
        return initial, None
    if (type(value.get('version')) is not int or value.get('version') != 1
            or value.get('address') != address
            or value.get('tab') != tab):
        return initial, 'checkpoint_identity_mismatch'
    frontier = _integer(value.get('frontier_page'))
    if frontier == 0:
        return initial, None
    anchors, head = value.get('anchor_ids'), value.get('head_anchor_ids')
    size = _integer(value.get('page_size'))
    total = _integer(value.get('total_items'))
    last = _integer(value.get('total_pages'))
    empty_complete = (total == 0 and frontier == 1 and anchors == []
                      and head == [] and value.get('complete') is True)
    if (not frontier or not size or total is None or not last
            or frontier > last
            or not (empty_complete
                    or (_valid_ids(anchors) and _valid_ids(head)))):
        return initial, 'checkpoint_invalid_restart'
    expected_anchor = min(size, max(0, total - (frontier - 1) * size))
    if (last != max(1, math.ceil(total / size))
            or len(anchors) != expected_anchor
            or len(head) != min(size, total)
            or len(set(anchors)) != len(anchors)
            or len(set(head)) != len(head)):
        return initial, 'checkpoint_invalid_restart'
    return deepcopy(value), None


class _Read:
    def __init__(self, session, address, tab, pages, checkpoint, known,
                 window):
        self.session, self.address, self.tab = session, address, tab
        self.budget = max(0, int(pages))
        # Head refresh and at most two frontier-overlap reads are additional
        # to the older-page budget. A large head gap shares this absolute cap.
        self.limit = self.budget + 3
        self.known = {i for i in known if isinstance(i, str)}
        self.window = window
        self.checkpoint, error = _checkpoint(checkpoint, address, tab)
        self.errors = [error] if error else []
        self.cache, self.events = {}, {}
        self.meta = None
        self.pagination_observed = None
        self.current, self.reads = 1, 0
        self.failed = self.count_uncertain = self.complete = False

    def error(self, code):
        if code not in self.errors:
            self.errors.append(code)

    def jump(self, number):
        if number == self.current:
            return True
        if number == self.current + 1:
            moved = self.session.sb.evaluate(history.NEXT)
        else:
            moved = self.session.sb.evaluate(
                SELECT_PAGE.replace('PAGE', str(number)))
            if moved == 'jump':
                for _ in range(10):
                    if self.session.sb.evaluate(FILL_JUMP.replace(
                            'PAGE', str(number))):
                        break
                    time.sleep(.1)
                else:
                    return False
                moved = self.session.sb.evaluate(CONFIRM_JUMP)
        if moved:
            self.current = number
            self.session.pause()
        return bool(moved)

    def read(self, number):
        if number in self.cache:
            return self.cache[number]
        if self.reads >= self.limit:
            return None
        self.reads += 1
        raw, window = {}, {}
        try:
            if not self.jump(number):
                self.error('page_navigation_failed')
                self.failed = True
                return None
            name = 'delegate' if self.tab == 'transactions' else 'transfers'
            normalized, _ = history._read_pages(
                self.session, name, self.address, self.tab, 1, window,
                start_page=number, raw_page=raw)
        except Exception:
            # Persist earlier verified pages; do not leak browser exception
            # text which may contain request URLs or credentials.
            self.error('page_read_failed')
            self.failed = True
            return None
        if number == 1 and self.window is not None:
            self.window['diagnostic'] = window.get('diagnostic', {})
        if number == 1:
            self.pagination_observed = _pagination_diagnostic(raw)
        previous_ids = set(self.events)
        # Row facts remain useful even when a malformed sibling or drifting
        # page makes it impossible to advance the coverage checkpoint.
        self.events.update((row['id'], row) for row in normalized)
        meta = _metadata(raw, number, self.meta)
        rows = _page(raw, number, self.address, self.tab, meta)
        if rows is None:
            self.error('unsupported_pagination' if raw and meta is None else
                       'page_rows_invalid' if meta else 'page_unavailable')
            if number == 1:
                self.meta = meta
            self.failed = True
            return None
        if self.meta is not None and meta != self.meta:
            self.error('pagination_changed_during_read')
            self.count_uncertain = self.failed = True
            return None
        if any(row['id'] in previous_ids for row in rows):
            self.error('page_overlap_changed_during_read')
            self.count_uncertain = self.failed = True
            return None
        previous, following = self.cache.get(number - 1), self.cache.get(
            number + 1)
        if rows and ((previous and previous[-1]['time'] < rows[0]['time'])
                     or (following
                         and rows[-1]['time'] < following[0]['time'])):
            self.error('page_order_changed_during_read')
            self.count_uncertain = self.failed = True
            return None
        self.meta = meta
        self.cache[number] = rows
        self.events.update((row['id'], row) for row in rows)
        return rows

    def frontier(self, number):
        self.checkpoint.update(
            self.meta, frontier_page=number,
            anchor_ids=_ids(self.cache[number]),
            head_anchor_ids=_ids(self.cache[1]), complete=False)

    def terminal(self):
        if self.checkpoint['frontier_page'] != self.meta['total_pages']:
            return
        if self.count_uncertain:
            return
        if len(self.known | self.events.keys()) == self.meta['total_items']:
            self.complete = True
            self.checkpoint['complete'] = True
        else:
            self.error('terminal_count_mismatch')
            # A previously migrated or changed source can have holes despite
            # a terminal cursor. Revisit contiguous pages on following runs.
            self.frontier(1)

    def extend(self):
        for _ in range(self.budget):
            number = self.checkpoint['frontier_page'] + 1
            if number > self.meta['total_pages']:
                break
            if self.read(number) is None:
                return
            self.frontier(number)
        self.terminal()

    def collect(self):
        head = self.read(1)
        if head is None:
            return
        old = deepcopy(self.checkpoint)
        self.frontier(1)
        if old['frontier_page'] == 0:
            self.extend()
            return
        changed = (old['page_size'] != self.meta['page_size']
                   or self.meta['total_items'] < old['total_items'])
        if changed:
            self.error('pagination_changed_restart')
            self.count_uncertain = True
            self.extend()
            return
        # An empty old source has no event anchor. Its entire new history
        # must be traversed from the head before it can be complete again.
        if not old['head_anchor_ids']:
            self.extend()
            return
        head_ids = set(old['head_anchor_ids'])
        number = 1
        while not head_ids.intersection(_ids(self.cache[number])):
            following = number + 1
            if (following > self.meta['total_pages']
                    or self.read(following) is None):
                if self.failed:
                    self.checkpoint = old
                else:
                    # An unusually large arrival batch is itself resumable:
                    # restart at the verified head frontier, not a distant
                    # old page separated by an unobserved gap.
                    self.frontier(number)
                    self.error('head_gap_restart')
                return
            number = following
        self.checkpoint = old
        self.checkpoint.update(head_anchor_ids=_ids(head), complete=False)
        anchor = old['anchor_ids'][-1]
        found = next((n for n, rows in self.cache.items()
                      if anchor in _ids(rows)), None)
        if found is None:
            size, total = old['page_size'], old['total_items']
            shift = self.meta['total_items'] - total
            position = min(old['frontier_page'] * size, total) + shift
            predicted = max(1, math.ceil(position / size))
            # Backtrack one page to validate continuity at page boundaries.
            for candidate in sorted({max(1, predicted - 1), predicted}):
                rows = self.read(candidate)
                if rows is None:
                    return
                if anchor in _ids(rows):
                    found = candidate
                    break
        if found is None:
            self.frontier(1)
            self.error('frontier_anchor_missing_restart')
            return
        self.frontier(found)
        self.extend()

    def result(self, progress):
        cumulative = len(self.known | self.events.keys())
        total = self.meta['total_items'] if self.meta is not None else None
        if total is not None and cumulative > total:
            self.error('stored_count_exceeds_source')
        remaining = (max(0, total - cumulative)
                     if total is not None and cumulative <= total
                     and not self.count_uncertain else None)
        number, since = 1, None
        while number in self.cache:
            rows = self.cache[number]
            if rows:
                since = rows[-1]['time']
            number += 1
        if since is not None and self.window is not None:
            self.window['since'] = since
        status = ('complete' if self.complete else 'partial'
                  if self.meta is not None or self.events else 'unavailable')
        progress.update(
            checkpoint=deepcopy(self.checkpoint), source_total=total,
            captured_this_run=len(self.events),
            new_rows=len(self.events.keys() - self.known),
            cumulative=cumulative, remaining=remaining, status=status,
            pages_read=self.reads, errors=self.errors, head_since=since,
            pagination_observed=self.pagination_observed,
            completion_tier='observed_source')
        return list(self.events.values()), status


def fetch_resumable(session, address, tab, pages, *, window=None,
                    checkpoint=None, known_ids=(), progress):
    """Fetch a fresh head plus bounded, anchor-validated older pages.

    ``pages`` budgets new older pages; at most ``pages + 3`` logical pages
    are read including head/overlap validation. ``captured_this_run`` counts
    distinct valid fetched IDs including overlap; ``new_rows`` excludes IDs
    supplied in ``known_ids``. Cumulative counts use durable per-tab IDs.
    """
    reader = _Read(session, address, tab, pages, checkpoint, known_ids, window)
    if window is not None:
        window.pop('since', None)
    try:
        session.goto(account_url(address) + '/' + tab, check_google=False)
    except Exception:
        reader.error('page_navigation_failed')
    else:
        reader.collect()
    return reader.result(progress)
