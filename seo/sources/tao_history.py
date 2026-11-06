"""Read the account tabs' own React Query results after the page loads.

Read-only access to the table behind the visible pager preserves event IDs,
full addresses and integer amounts which the grid shortens for display.
"""
import json
import math
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from datetime import datetime, timezone

from seo.sources.taostats_wallets import BASE, account_url, _ss58, rao_to_tao

# The site's verification widget can take over ten seconds to initialize.
PAGE_POLLS = 40
POLL_INTERVAL = 0.5

# Select only the current page's public table query. No direct API requests.
TABLE_QUERY = """
(() => {
  const root = document.querySelector('.ag-root-wrapper');
  const widget = document.querySelector('.cf-turnstile,'
    + 'iframe[src*="challenges.cloudflare.com"],'
    + '[name="cf-turnstile-response"]');
  const reply = document.querySelector('[name="cf-turnstile-response"]');
  const host = reply?.parentElement || widget;
  const rect = host?.getBoundingClientRect();
  const control = document.querySelector('#cf-turnstile');
  const box = control?.getBoundingClientRect();
  const hit = box && document.elementFromPoint(box.left + 20, box.top + 32);
  const visible = !!(box && box.width >= 200 && box.height >= 50
                     && control.contains(hit));
  const diagnostic = {
    grid_present: !!root,
    rendered_cells: root?.querySelectorAll('[role=gridcell]').length || 0,
    widget_present: !!widget, widget_completed: !!reply?.value,
    widget_host_size: rect ? [Math.round(rect.width),
                              Math.round(rect.height)] : null,
    taostats_control_visible: visible,
    page_ready: document.readyState, query_found: false,
    active_table_queries: 0,
  };
  if (!root) return {diagnostic};
  let fiber = root[Object.keys(root).find(k => k.startsWith('__reactFiber'))];
  for (; fiber; fiber = fiber.return) {
    for (const client of [fiber.memoizedProps?.value,
                          fiber.memoizedProps?.client]) {
      if (!client?.getQueryCache) continue;
      const queries = client.getQueryCache().getAll().filter(q =>
        q.queryKey[0] === NAME && q.getObserversCount() > 0);
      diagnostic.active_table_queries = queries.length;
      const query = queries.find(q => {
        const p = q.queryKey[1];
        return (p?.address || p?.nominator || null) === OWNER
          && p?.order === "timestamp_desc" && Number(p?.page || 1) === PAGE
          && !p.netuid && !p.action && !p.amount_min && !p.amountMin;
      });
      if (!query) continue;
      const failure = String(query.state.error?.message || '');
      const categories = [
        [/turnstile|captcha|challenge|verif/i, 'verification'],
        [/timeout|timed out/i, 'timeout'],
        [/rate limit|429/i, 'rate_limit'],
        [/auth|forbidden|403|401/i, 'authorization'],
        [/fetch|network|connect/i, 'network'],
      ];
      Object.assign(diagnostic, {
        failure_category: categories.find(([match]) =>
          match.test(failure))?.[1] || null,
        query_found: true, query_status: query.state.status,
        fetch_status: query.state.fetchStatus,
        failed_attempts: query.state.fetchFailureCount,
        failure_type: query.state.error?.name || null,
        http_status: Number(query.state.error?.status ||
          query.state.error?.response?.status) || null,
      });
      return {diagnostic, status: query.state.status,
              data: query.state.data ?? null};
    }
  }
  return {diagnostic};
})()
"""
RESOURCE_STATE = """
(() => {
  const result = {verification_assets: 0, table_requests: 0,
                  table_http_statuses: []};
  for (const entry of performance.getEntriesByType('resource')) {
    const u = new URL(entry.name, location.href);
    if (u.hostname === 'challenges.cloudflare.com') {
      result.verification_assets++;
    } else if (/(^|\\.)taostats\\.io$/.test(u.hostname) &&
               ['fetch', 'xmlhttprequest'].includes(entry.initiatorType) &&
               /delegat|transfer/i.test(u.pathname)) {
      result.table_requests++;
      if (entry.responseStatus) result.table_http_statuses.push(
        entry.responseStatus);
    }
  }
  return result;
})()
"""
NEXT = """
(() => {
  let node = document.querySelector('.ag-root-wrapper');
  while (node) {
    const btn = [...node.querySelectorAll('button')].find(
      e => e.textContent.trim() === 'Next');
    if (btn) {
      if (btn.disabled || btn.getAttribute('aria-disabled') === 'true')
        return false;
      btn.click(); return true;
    }
    node = node.parentElement;
  }
  return false;
})()
"""


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else None
    except (AttributeError, ValueError):
        return None


def _quantity(raw):
    amount = rao_to_tao(raw)
    return amount if (amount is not None and math.isfinite(amount)
                      and amount >= 0) else None


def _index(raw):
    if str(raw.get("event_index")).isdigit():
        return int(raw["event_index"])
    # Legacy event IDs end in "<block>-<event index>".
    for key in ("event_id", "id"):
        m = re.search(r"(?:^|-)(\d+)-(\d+)$", str(raw.get(key)))
        if m and m[1] == str(raw.get("block_number")):
            return int(m[2])
    return 0


def normalize(raw, tab, address):
    """One movement from a table. TAO and alpha amounts are in base units."""
    if not isinstance(raw, dict):
        return None
    when = timestamp(raw.get("timestamp"))
    extrinsic = raw.get("extrinsic_id")
    if not when or not extrinsic:
        return None
    amount = _quantity(raw.get("amount"))
    alpha = netuid = side = None
    if tab == "transfers":
        src, dst = _ss58(raw.get("from")), _ss58(raw.get("to"))
        if not src or not dst or address not in (src, dst) or amount is None:
            return None
        kind = "tao_transfer"
    else:
        owner = _ss58(raw.get("nominator"))
        if owner != address:
            return None
        action = str(raw.get("action")).lower()
        netuid = raw.get("netuid")
        alpha = _quantity(raw.get("alpha"))
        if (action not in ("delegate", "undelegate")
                or not str(netuid).isdigit() or alpha is None or alpha <= 0):
            return None
        netuid = int(netuid)
        incoming = action == "delegate"
        peer = _ss58(raw.get("transfer_address"))
        src, dst = (None, owner) if incoming else (owner, None)
        if raw.get("is_transfer"):
            kind = "stake_transfer"
            side = "in" if incoming else "out"
            src, dst = (peer, owner) if incoming else (owner, peer)
        else:
            kind = "buy" if incoming else "sell"
            if amount is None or amount <= 0:
                return None
    # Matching in/out legs have different event IDs in some extrinsics.
    # Retain both raw legs in storage; the ledger pairs them before replay.
    identity = raw.get("id") or raw.get("event_id")
    key = (tab, extrinsic, address, identity)
    if identity is None:
        key += (raw.get("event_index"), raw.get("action"), src, dst, netuid,
                _ss58(raw.get("delegate")), alpha, amount)
    return dict(id=json.dumps(key, separators=(",", ":")), time=when,
                extrinsic=extrinsic, event_index=_index(raw), owner=address,
                kind=kind, src=src, dst=dst, alpha=alpha, tao=amount,
                netuid=netuid, transfer_side=side, evidence="transaction")


def fetch_history(session, address, tab, pages, *, window=None,
                  checkpoint=None, known_ids=(), progress=None):
    """Read history, optionally resuming a durable per-account/tab frontier.

    ``progress`` opts into bounded backfill; persist its checkpoint together
    with the returned events. Existing callers retain their page-limit API.
    """
    if progress is not None:
        from seo.sources.tao_wallet_history import fetch_resumable
        return fetch_resumable(
            session, address, tab, pages, window=window,
            checkpoint=checkpoint, known_ids=known_ids, progress=progress)
    name = "delegate" if tab == "transactions" else "transfers"
    session.goto(account_url(address) + "/" + tab, check_google=False)
    return _read_pages(session, name, address, tab, pages, window)


def fetch_recent_trades(session, pages=6):
    """Read recent buyers and sellers from the chain-wide delegation grid."""
    session.goto(BASE + "/delegation", check_google=False)
    rows, coverage = _read_pages(session, "delegate", None, "transactions",
                                 pages)
    return [r for r in rows if r["kind"] in ("buy", "sell")], coverage


def _wait_table(session, js):
    state = {}
    for _ in range(PAGE_POLLS):
        result = session.sb.evaluate(js)
        state = result if isinstance(result, dict) else {}
        fetching = (state.get("diagnostic") or {}).get("fetch_status")
        if (state.get("status") in ("success", "error")
                and fetching not in ("fetching", "paused")):
            break
        time.sleep(POLL_INTERVAL)
    return state


def _read_pages(session, name, address, tab, pages, window=None, *,
                start_page=1, raw_page=None):
    events = {}
    incomplete = False
    last_page = start_page + pages - 1
    for page in range(start_page, last_page + 1):
        js = TABLE_QUERY.replace("NAME", json.dumps(name)).replace(
            "OWNER", json.dumps(address)).replace("PAGE", str(page))
        state = _wait_table(session, js)
        diag = state.get("diagnostic", {})
        if (page == 1 and state.get("status") == "pending"
                and diag.get("widget_present")
                and not diag.get("widget_completed")):
            # Use the browser session's existing normal verification control
            # once, only after the page has had time to finish on its own.
            try:
                gui = getattr(session.sb, "gui_click_captcha", None)
                if diag.get("taostats_control_visible"):
                    # The generic helper otherwise matches AG Grid's spacer.
                    session.sb.click_with_offset("#cf-turnstile", 20, 32,
                                                 scroll=False)
                    attempt = "taostats_control_attempted"
                elif gui and not getattr(session, "headless", False):
                    gui()
                    attempt = "desktop_control_attempted"
                else:
                    session.sb.solve_captcha()
                    attempt = "browser_control_attempted"
            except Exception:
                attempt = "helper_error"
            state = _wait_table(session, js)
            diag = state.get("diagnostic", {})
            diag["verification_control"] = attempt
            folder = os.environ.get("TAO_DIAGNOSTIC_DIR")
            if folder and state.get("status") != "success":
                path = Path(folder) / "wallet-verification.png"
                with suppress(OSError, RuntimeError):
                    if not path.exists():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        session.sb.save_screenshot(str(path))
        if window is not None:
            window["diagnostic"] = diag
        result = (state.get("data") if state.get("status") == "success"
                  and diag.get("fetch_status") not in ("fetching", "paused")
                  else None)
        if not isinstance(result, dict) or not isinstance(
                result.get("data"), list):
            if window is not None:
                resources = session.sb.evaluate(RESOURCE_STATE)
                if isinstance(resources, dict):
                    window.setdefault("diagnostic", {}).update(resources)
            status = "partial" if page > 1 else "unavailable"
            return list(events.values()), status
        pagination = result.get("pagination") or {}
        if raw_page is not None:
            raw_page.update(result)
        if str(pagination.get("current_page", page)) != str(page):
            break
        for raw in result["data"]:
            owner = address
            if owner is None and isinstance(raw, dict):
                owner = _ss58(raw.get("nominator"))
            event = normalize(raw, tab, owner) if owner else None
            if event and event["id"] not in events:
                events[event["id"]] = event
            else:
                incomplete = True
        if window is not None and not incomplete and events:
            times = [e["time"] for e in events.values()]
            if times == sorted(times, reverse=True):
                window["since"] = times[-1]
        total_pages = pagination.get("total_pages")
        try:
            if total_pages is None and "total_items" in pagination:
                limit = int(pagination.get("per_page")
                            or pagination.get("limit", 10))
                total = int(pagination["total_items"])
                total_pages = (total + limit - 1) // limit
            if total_pages is not None and page >= int(total_pages):
                return list(events.values()), ("partial" if incomplete else
                                               "complete")
        except (TypeError, ValueError, ZeroDivisionError):
            break
        if page == last_page or not session.sb.evaluate(NEXT):
            break
        session.pause()
    return list(events.values()), "partial"
