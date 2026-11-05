"""Resumable census of the rows exposed by Taostats' public Accounts grid."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from seo.browser import Session
from seo.models import MissingDependency
from seo.sources import tao_holders
from seo.sources.taostats_wallets import BASE, _ss58
from seo.tao_wallets import ADDRESS_RE, delay_range

URL = BASE + "/accounts"
PAGE_SIZES = (10, 25, 50, 100)
MAX_PAGES = 100

# Read only the active, unfiltered leaderboard query used by this grid.
READ_PAGE = """
(() => {
  const grid = [...document.querySelectorAll('.ag-root-wrapper')].find(g =>
    g.querySelector('[role=columnheader][col-id=balance_total]') &&
    g.querySelector('[role=columnheader][col-id=address]'));
  const widget = document.querySelector('#cf-turnstile');
  const box = widget?.getBoundingClientRect();
  const hit = box && document.elementFromPoint(box.left+20, box.top+32);
  const diagnostic = {grid:!!grid, control:!!(box && box.width>=200 &&
    box.height>=50 && widget.contains(hit)), verified:
    !!document.querySelector('[name=cf-turnstile-response]')?.value};
  if (!grid) return {diagnostic};
  let fiber=grid[Object.keys(grid).find(k=>k.startsWith('__reactFiber'))];
  for(;fiber;fiber=fiber.return) {
    const clients=[fiber.memoizedProps?.value,fiber.memoizedProps?.client];
    for(const client of clients) {
      if(!client?.getQueryCache) continue;
      const q=client.getQueryCache().getAll().find(q=>{
        const [name,p]=q.queryKey;
        return name==='account' && p && Object.keys(p).every(
          k=>['page','limit','order'].includes(k)) &&
          p.order==='balance_total_desc' && Number(p.page||1)===PAGE &&
          (!SIZE || Number(p.limit)===SIZE) && q.getObserversCount()>0;
      });
      if(!q) continue;
      const data=q.state.data;
      if(q.state.status!=='success' || !Array.isArray(data?.data))
        return {diagnostic,status:q.state.status};
      return {diagnostic,status:'success',page:{pagination:data.pagination,
        data:data.data.map(r=>({address:r.address,network:r.network,
          block_number:r.block_number,rank:r.rank,
          balance_total:r.balance_total,
          balance_staked:r.balance_staked,
          balance_staked_alpha_as_tao:r.balance_staked_alpha_as_tao}))}};
    }
  }
  return {diagnostic};
})()
"""
OPEN_SIZE = """
(() => {
  const btn=[...document.querySelectorAll('button[aria-haspopup=menu]')].find(
    b=>b.getClientRects().length &&
       /^(10|25|50|100)$/.test(b.innerText.trim()));
  if(!btn) return false;
  btn.dispatchEvent(new PointerEvent('pointerdown',{button:0,bubbles:true,
    cancelable:true,pointerType:'mouse'}));
  return true;
})()
"""
PICK_SIZE = """
(() => {
  const item=[...document.querySelectorAll('[role=menuitem]')].find(
    e=>e.innerText.trim()===String(SIZE));
  if(!item) return false;
  item.click(); return true;
})()
"""


class DirectoryError(ValueError):
    """A page could not be verified, leaving the checkpoint available."""


class AccountsDirectory:
    def __init__(self, session, size):
        self.session = session
        self.size = size
        self.current = None
        self.first = None

    def _read(self, page, size):
        js = READ_PAGE.replace("PAGE", str(page)).replace("SIZE", str(size))
        clicked = False
        for tick in range(70):
            state = self.session.sb.evaluate(js) or {}
            result = state.get("page") or {}
            pager = result.get("pagination") or {}
            if (state.get("status") == "success"
                    and str(pager.get("current_page")) == str(page)
                    and (not size or str(pager.get("per_page")) == str(size))):
                return result
            diag = state.get("diagnostic", {})
            if (not clicked and tick >= 6 and diag.get("control")
                    and not diag.get("verified")):
                clicked = True
                self.session.sb.click_with_offset(
                    "#cf-turnstile", 20, 32, scroll=False)
            elif state.get("status") == "error" and tick >= 8:
                break
            time.sleep(.5)
        raise DirectoryError("Accounts page %d did not return readable rows "
                             "(status %s)" % (page, state.get("status",
                                                              "unavailable")))

    def _jump(self, target):
        select = tao_holders.SELECT_PAGE.replace("[col-id=balance]",
                                                 "[col-id=balance_total]")
        action = self.session.sb.evaluate(select.replace("PAGE", str(target)))
        if action == "jump":
            for _ in range(20):
                if self.session.sb.evaluate(tao_holders.FILL_JUMP.replace(
                        "PAGE", str(target))):
                    break
                time.sleep(.1)
            else:
                raise DirectoryError("Accounts pager did not open jump field")
            action = self.session.sb.evaluate(tao_holders.CONFIRM_JUMP)
        if not action:
            raise DirectoryError("Could not select Accounts page %d" % target)

    def __call__(self, page):
        if self.current is None:
            self.session.goto(URL, check_google=False)
            self.first = self._read(1, 0)
            self.current = 1
            if int(self.first["pagination"]["per_page"]) != self.size:
                self.session.pause()
                if not self.session.sb.evaluate(OPEN_SIZE):
                    raise DirectoryError("Accounts Rows menu is unavailable")
                for _ in range(20):
                    if self.session.sb.evaluate(PICK_SIZE.replace(
                            "SIZE", str(self.size))):
                        break
                    time.sleep(.1)
                else:
                    raise DirectoryError("Accounts page size unavailable")
                self.first = self._read(1, self.size)
        if self.current != page:
            self.session.pause()
            self._jump(page)
            result = self._read(page, self.size)
            self.current = page
            return result
        if page == 1:
            return self.first
        return self._read(page, self.size)


def _int(value, name, *, optional=False):
    if value is None and optional:
        return None
    if (isinstance(value, bool) or not str(value).isascii()
            or not str(value).isdigit() or int(value) > 2**63 - 1):
        raise DirectoryError("invalid Accounts %s" % name)
    return int(value)


def _verified(data, page):
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise DirectoryError("missing Accounts rows")
    p = data.get("pagination")
    if not isinstance(p, dict):
        raise DirectoryError("missing Accounts pagination")
    size = _int(p.get("per_page"), "per_page")
    total = _int(p.get("total_items"), "total_items")
    last = _int(p.get("total_pages"), "total_pages")
    expected = min(size, total - (page - 1) * size)
    current = _int(p.get("current_page"), "current_page")
    following = _int(p.get("next_page"), "next_page", optional=True)
    if (not size or not total or page > last or current != page
            or last != (total + size - 1) // size
            or following != (page + 1 if page < last else None)
            or len(data["data"]) != expected):
        raise DirectoryError("inconsistent Accounts page %d" % page)
    rows = []
    identities = set()
    for raw in data["data"]:
        if not isinstance(raw, dict):
            raise DirectoryError("invalid Accounts row")
        address = _ss58(raw.get("address"))
        net = raw.get("network")
        if (not isinstance(address, str) or not address.startswith("5")
                or not ADDRESS_RE.fullmatch(address)
                or not isinstance(net, str) or not net
                or (address, net) in identities):
            raise DirectoryError("invalid/repeated account on page %d" % page)
        identities.add((address, net))
        rows.append(dict(address=address, network=net, rank=_int(
            raw.get("rank"), "rank", optional=True), block_number=_int(
            raw.get("block_number"), "block", optional=True),
            balance_total=_int(raw.get("balance_total"), "balance_total"),
            balance_staked=_int(raw.get("balance_staked"), "balance_staked",
                                optional=True),
            alpha_as_tao=_int(raw.get("balance_staked_alpha_as_tao"),
                              "alpha_as_tao", optional=True)))
    return size, total, last, rows


def _schema(db):
    db.row_factory = sqlite3.Row
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS census_state ("
                   "id INTEGER PRIMARY KEY CHECK (id=1), page_size INTEGER, "
                   "first_total INTEGER, latest_total INTEGER, "
                   "latest_checked_at TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS census_pages ("
                   "page INTEGER PRIMARY KEY,row_count INTEGER,total INTEGER,"
                   "min_block INTEGER,max_block INTEGER,"
                   "missing_blocks INTEGER,"
                   "first_rank INTEGER,last_rank INTEGER,first_address TEXT,"
                   "last_address TEXT,checked_at TEXT,visits INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS census_accounts ("
                   "address TEXT NOT NULL,network TEXT NOT NULL,"
                   "first_page INTEGER,last_page INTEGER,rank INTEGER,"
                   "first_seen INTEGER,last_seen INTEGER,block_number INTEGER,"
                   "balance_total_rao INTEGER,balance_staked_rao INTEGER,"
                   "staked_alpha_as_tao_rao INTEGER,"
                   "PRIMARY KEY(address,network)) WITHOUT ROWID")


def _observe_total(db, size, total, stamp):
    state = db.execute("SELECT page_size FROM census_state").fetchone()
    if state and state[0] != size:
        raise DirectoryError("page size differs from this checkpoint; "
                             "use its saved page size or a separate database")
    db.execute("INSERT INTO census_state VALUES(1,?,?,?,?) ON CONFLICT(id) "
               "DO UPDATE SET latest_total=excluded.latest_total,"
               "latest_checked_at=excluded.latest_checked_at",
               (size, total, total, stamp))


def _save_page(db, page, verified, stamp):
    size, total, _, rows = verified
    when = int(datetime.fromisoformat(stamp).timestamp())
    blocks = [r["block_number"] for r in rows if r["block_number"] is not None]
    with db:
        _observe_total(db, size, total, stamp)
        db.executemany("INSERT INTO census_accounts VALUES("
                       "?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(address,network) "
                       "DO UPDATE SET last_page=excluded.last_page,"
                       "rank=excluded.rank,last_seen=excluded.last_seen,"
                       "block_number=excluded.block_number,"
                       "balance_total_rao=excluded.balance_total_rao,"
                       "balance_staked_rao=excluded.balance_staked_rao,"
                       "staked_alpha_as_tao_rao="
                       "excluded.staked_alpha_as_tao_rao",
                       [(r["address"], r["network"], page, page, r["rank"],
                         when, when, r["block_number"], r["balance_total"],
                         r["balance_staked"], r["alpha_as_tao"])
                        for r in rows])
        visits = db.execute("SELECT visits FROM census_pages WHERE page=?",
                            (page,)).fetchone()
        db.execute("INSERT OR REPLACE INTO census_pages VALUES("
                   "?,?,?,?,?,?,?,?,?,?,?,?)", (
                       page, len(rows), total, min(blocks, default=None),
                       max(blocks, default=None), len(rows) - len(blocks),
                       rows[0]["rank"], rows[-1]["rank"], rows[0]["address"],
                       rows[-1]["address"], stamp,
                       1 + (visits[0] if visits else 0)))


def _next_page(db, size, total):
    last = (total + size - 1) // size
    needed = 1
    for page, count in db.execute("SELECT page,row_count FROM census_pages "
                                  "WHERE page<=? ORDER BY page", (last,)):
        if page != needed or count != min(size, total - (page - 1) * size):
            break
        needed += 1
    return needed if needed <= last else None


def _report(db, sampled, error, anchor_block=None):
    state = db.execute("SELECT * FROM census_state WHERE id=1").fetchone()
    count, unique, networks = db.execute(
        "SELECT COUNT(*),COUNT(DISTINCT address),COUNT(DISTINCT network) "
        "FROM census_accounts").fetchone()
    result = dict(source=URL, coverage="All Amounts / All Networks; balances "
                  "ranked by total TAO", status="unavailable",
                  verified_pages_this_run=sampled, error=error,
                  latest_anchor_block=anchor_block,
                  unique_coldkeys=unique, network_accounts=count,
                  wallet_histories_checked=0, transfer_ties_checked=0)
    if not state:
        return result
    size, total = state["page_size"], state["latest_total"]
    last = (total + size - 1) // size
    cursor = _next_page(db, size, total)
    pages, low, high, no_block, totals, visits = db.execute(
        "SELECT COUNT(*),min(min_block),max(max_block),sum(missing_blocks),"
        "COUNT(DISTINCT total),max(visits) FROM census_pages WHERE page<=?",
        (last,)).fetchone()
    first = db.execute("SELECT * FROM census_pages WHERE page=1").fetchone()
    end = db.execute("SELECT * FROM census_pages WHERE page=?",
                     (last,)).fetchone()
    exact = (cursor is None and state["first_total"] == count == total
             and low is not None and low == high == anchor_block
             and not no_block and totals == 1
             and visits == 1 and networks == 1)
    status = "partial"
    if not error and cursor is None:
        status = "complete_single_block" if exact else "traversed_live"
    result.update(status=status, page_size=size, total_reported=total,
                  first_reported_total=state["first_total"], total_pages=last,
                  pages_stored=pages, next_page=cursor, min_row_block=low,
                  max_row_block=high, checked_at=state["latest_checked_at"],
                  first_page_sample=dict(first) if first else None,
                  last_page_sample=dict(end) if end else None)
    return result


def scan(db, fetch, pages, *, sample_last=False, progress=None):
    """Commit each verified page, advancing only over contiguous pages."""
    if not 1 <= pages <= MAX_PAGES:
        raise ValueError("pages must be between 1 and 100")
    _schema(db)
    saved = []
    error = None
    anchor_block = None
    try:
        first = _verified(fetch(1), 1)
        size, total, last, _ = first
        anchor_blocks = {r["block_number"] for r in first[3]}
        if len(anchor_blocks) == 1:
            anchor_block = anchor_blocks.pop()
        stamp = datetime.now(timezone.utc).isoformat()
        with db:
            _observe_total(db, size, total, stamp)
        for _ in range(pages):
            page = _next_page(db, size, total)
            if page is None:
                break
            data = first if page == 1 else _verified(fetch(page), page)
            if data[0] != size:
                raise DirectoryError("Accounts page size changed during scan")
            _save_page(db, page, data, datetime.now(timezone.utc).isoformat())
            saved.append(page)
            if progress:
                progress(page, len(data[3]), data[2])
            total, last = data[1:3]
        observed_last = db.execute(
            "SELECT row_count FROM census_pages WHERE page=?",
            (last,)).fetchone()
        last_count = total - (last - 1) * size
        if (sample_last and last not in saved
                and (not observed_last or observed_last[0] != last_count)):
            data = _verified(fetch(last), last)
            _save_page(db, last, data, datetime.now(timezone.utc).isoformat())
            saved.append(last)
            if progress:
                progress(last, len(data[3]), data[2])
    except DirectoryError as exc:
        error = str(exc)
    return _report(db, saved, error, anchor_block)


def _pages(value):
    n = int(value)
    if not 1 <= n <= MAX_PAGES:
        raise argparse.ArgumentTypeError("use 1..100 pages per pass")
    return n


def _progress(page, rows, total):
    print("Verified Accounts page %d/%d (%d rows)." % (page, total, rows),
          flush=True)


def _write_report(out, result):
    with tempfile.NamedTemporaryFile(dir=out.parent, mode="w",
                                     encoding="utf8", delete=False) as fh:
        tmp = Path(fh.name)
        try:
            json.dump(result, fh, indent=2)
            fh.write("\n")
        except BaseException:
            fh.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/tao_census.sqlite")
    p.add_argument("--out", default="data/tao_census.json")
    p.add_argument("--pages", type=_pages, default=10,
                   help="maximum sequential pages to save per pass (1..100)")
    p.add_argument("--page-size", type=int, choices=PAGE_SIZES, default=100)
    p.add_argument("--delay", type=delay_range, default="2-4",
                   help="seconds between page changes (single value or range)")
    p.add_argument("--sample-last", action="store_true",
                   help="also verify the current terminal page if needed")
    args = p.parse_args(argv)
    path, out = Path(args.db), Path(args.out)
    if path.resolve() == out.resolve():
        p.error("the database and JSON report need separate paths")
    path.parent.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=30)) as db:
        try:
            with Session(ad_block=False, min_pause=args.delay[0],
                         max_pause=args.delay[1]) as browser:
                directory = AccountsDirectory(browser, args.page_size)

                def fetch(n):
                    try:
                        return directory(n)
                    except DirectoryError:
                        raise
                    except Exception as exc:
                        raise DirectoryError("Accounts page %d failed (%s)" % (
                            n, type(exc).__name__)) from exc

                result = scan(db, fetch, args.pages,
                              sample_last=args.sample_last, progress=_progress)
        except MissingDependency as exc:
            _schema(db)
            result = _report(db, [], "Browser unavailable: %s" % exc)
    _write_report(out, result)
    print("Census: %s; coldkeys=%s, listed accounts=%s, next page=%s" % (
        result["status"], result["unique_coldkeys"],
        result.get("total_reported"), result.get("next_page")))
    if result["error"]:
        print("Coverage error: " + result["error"])
    return 1 if result["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
