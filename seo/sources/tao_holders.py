"""Sample coldkeys from the public Holders tab on a Taostats subnet chart."""
import time

from seo.sources.taostats_wallets import BASE, _ss58
from seo.tao_wallets import ADDRESS_RE

SELECT_HOLDERS = r"""
(() => {
  const buttons = [...document.querySelectorAll('button')];
  const tab = buttons.find(b => !b.disabled && b.getClientRects().length &&
    /^holders(?:\s*\(|$)/i.test(b.innerText.trim()));
  if (!tab) return false;
  tab.click(); return true;
})()
"""
READ_PAGE = r"""
(() => {
  const grid = [...document.querySelectorAll('.ag-root-wrapper')].find(
    g => g.querySelector('[role=columnheader][col-id=coldkey]') &&
         g.querySelector('[role=columnheader][col-id=balance]'));
  const tab = [...document.querySelectorAll('button')].find(b =>
    b.getClientRects().length && /^holders(?:\s*\(|$)/i.test(
      b.innerText.trim()));
  const control = document.querySelector('#cf-turnstile');
  const box = control?.getBoundingClientRect();
  const hit = box && document.elementFromPoint(box.left + 20, box.top + 32);
  const reply = document.querySelector('[name="cf-turnstile-response"]');
  const diagnostic = {grid_present: !!grid, query_found: false,
    cells: grid?.querySelectorAll('[role=gridcell]').length || 0,
    widget_present: !!(control || reply), widget_completed: !!reply?.value,
    control_visible: !!(box && box.width >= 200 && box.height >= 50 &&
                        control.contains(hit))};
  const anchor = grid || tab;
  if (!anchor) return {diagnostic};
  const key = Object.keys(anchor).find(k => k.startsWith('__reactFiber'));
  let fiber = anchor[key];
  for (; fiber; fiber = fiber.return) {
    for (const client of [fiber.memoizedProps?.value,
                          fiber.memoizedProps?.client]) {
      if (!client?.getQueryCache) continue;
      const q = client.getQueryCache().getAll().find(q => {
        const [name, p] = q.queryKey;
        return name === 'stakeBalance' && Number(p?.netuid) === NETUID
          && Number(p?.page || 1) === PAGE && q.getObserversCount() > 0;
      });
      if (q) {
        diagnostic.query_found = true;
        diagnostic.fetch_status = q.state.fetchStatus;
        diagnostic.http_status = Number(q.state.error?.status ||
          q.state.error?.response?.status) || null;
        return {diagnostic, status: q.state.status, data: q.state.data};
      }
    }
  }
  return {diagnostic};
})()
"""
SELECT_PAGE = """
(() => {
  const target = PAGE;
  const grids = [...document.querySelectorAll('.ag-root-wrapper')];
  const column = '[role=columnheader][col-id=balance]';
  const grid = grids.find(g => g.querySelector(column));
  for (let n = grid; n; n = n.parentElement) {
    const buttons = [...n.querySelectorAll('button')];
    if (!buttons.some(b => b.innerText.trim() === 'Next')) continue;
    const page = buttons.find(b => b.innerText.trim() === String(target));
    if (page && !page.disabled) { page.click(); return true; }
    const jump = buttons.find(b => b.innerText.trim() === '...');
    if (!jump || jump.disabled) return false;
    jump.click(); return 'jump';
  }
  return false;
})()
"""
FILL_JUMP = """
(() => {
  const input = document.activeElement;
  if (!input?.matches('input[inputmode="numeric"]')) return false;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,
                                                 'value').set;
  setter.call(input, 'PAGE');
  input.dispatchEvent(new Event('input', {bubbles: true}));
  return true;
})()
"""
CONFIRM_JUMP = """
(() => {
  const input = document.activeElement;
  if (!input?.matches('input[inputmode="numeric"]')) return false;
  input.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Enter', code: 'Enter', bubbles: true}));
  return true;
})()
"""


def _ranked(rows, netuid):
    ranks = {}
    valid = True
    for r in rows:
        if not isinstance(r, dict) or str(r.get("netuid")) != str(netuid):
            valid = False
            continue
        addr = _ss58(r.get("coldkey"))
        balance = str(r.get("balance"))
        rank = str(r.get("subnet_rank"))
        if (not isinstance(addr, str) or not ADDRESS_RE.fullmatch(addr)
                or not balance.isdigit() or not rank.isdigit()
                or int(rank) < 1):
            valid = False
            continue
        if int(balance) > 0:
            ranks[addr] = min(ranks.get(addr, int(rank)), int(rank))
    return sorted(ranks, key=lambda addr: (ranks[addr], addr)), valid


def _sample_ranks(ranked, limit):
    """Evenly space selections over the unique ranks actually observed."""
    if len(ranked) <= limit:
        return ranked
    if limit == 1:
        return [ranked[(len(ranked) - 1) // 2]]
    return [ranked[round(i * (len(ranked) - 1) / (limit - 1))]
            for i in range(limit)]


def _read(session, netuid, page):
    js = READ_PAGE.replace("NETUID", str(netuid)).replace("PAGE", str(page))
    waited, limit = 0, 40
    action = "none"
    while waited < limit:
        state = session.sb.evaluate(js) or {}
        diag = state.get("diagnostic") or {}
        if state.get("status") == "success":
            data = state.get("data")
            if (isinstance(data, dict) and isinstance(data.get("data"), list)
                    and str((data.get("pagination") or {}).get(
                        "current_page")) == str(page)):
                return data
        if (page == 1 and waited in (3, 7, 13, 21) and diag
                and not diag.get("grid_present") and not diag.get(
                    "query_found")):
            # A server-rendered tab can accept a click before hydration and
            # leave Transactions selected. Stop reselecting once the UI
            # mounts the holder grid or subscribes to the holder query.
            session.sb.evaluate(SELECT_HOLDERS)
        ready_to_retry = waited >= 6 or state.get("status") == "error"
        if (action == "none" and ready_to_retry
                and diag.get("control_visible")
                and not diag.get("widget_completed")):
            action = "control_attempted"
            try:
                session.sb.click_with_offset("#cf-turnstile", 20, 32,
                                             scroll=False)
            except Exception:
                action = "control_failed"
            limit = max(limit, waited + 30)
        elif state.get("status") == "error":
            break
        waited += 1
        time.sleep(.5)
    verification = ("complete" if diag.get("widget_completed") else
                    "pending" if diag.get("widget_present") else "absent")
    print("SN%d holders page %d unavailable: status=%s, holder_grid=%s, "
          "query=%s, cells=%s, verification=%s, control=%s, http=%s" % (
              netuid, page, state.get("status", "missing"),
              bool(diag.get("grid_present")), bool(diag.get("query_found")),
              diag.get("cells", 0), verification, action,
              diag.get("http_status")))
    return None


def fetch_holders(session, netuid, limit=6, pages=1):
    """Return (coldkeys, coverage) from up to five pages of ranked holders."""
    if not 0 <= pages <= 5 or not 0 <= limit <= 100 or (
            type(netuid) is not int or not 0 <= netuid <= 65535):
        raise ValueError("use 0..5 holder pages, 0..100 addresses, valid ID")
    if not pages or not limit:
        return [], "disabled"
    rows = []
    fully_read = False
    try:
        session.pause()
        session.goto("%s/subnets/%d/chart" % (BASE, netuid),
                     check_google=False)
        for _ in range(20):
            if session.sb.evaluate(SELECT_HOLDERS):
                break
            time.sleep(.5)
        else:
            print("SN%d holders unavailable: no visible Holders tab" % netuid)
            return [], "unavailable"
        data = _read(session, netuid, 1)
        if not data:
            return [], "unavailable"
        rows.extend(data["data"])
        paging = data["pagination"]
        last = int(paging["total_pages"])
        # Sample across the ranking using the native page-jump field. The
        # 75th percentile adds smaller positions without selecting only dust.
        targets = range(2, last + 1) if last <= pages else (
            round(1 + (last - 1) * .75 * i / (pages - 1))
            for i in range(1, pages))
        seen = {1}
        for n in targets:
            session.pause()
            jump = session.sb.evaluate(SELECT_PAGE.replace("PAGE", str(n)))
            if not jump:
                continue
            if jump == "jump":
                for _ in range(10):
                    if session.sb.evaluate(FILL_JUMP.replace("PAGE", str(n))):
                        break
                    time.sleep(.1)
                else:
                    continue
                if not session.sb.evaluate(CONFIRM_JUMP):
                    continue
            data = _read(session, netuid, n)
            if data:
                seen.add(n)
                rows.extend(data["data"])
        fully_read = len(seen) >= last and len(rows) == int(paging[
            "total_items"])
    except Exception as exc:
        print("SN%d holders could not finish (%s)" % (netuid,
                                                      type(exc).__name__))
        if not rows:
            return [], "unavailable"
    ranks, valid = _ranked(rows, netuid)
    sample = _sample_ranks(ranks, limit)
    if fully_read and valid and len(sample) == len(ranks):
        return sample, "complete"
    return sample, "partial"
