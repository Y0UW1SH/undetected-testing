"""Read the account tabs' own React Query results after the page loads.

Read-only access to the table behind the visible pager preserves event IDs,
full addresses and integer amounts which the grid shortens for display.
"""
import json
import math
import re
import time
from datetime import datetime, timezone

from seo.sources.taostats_wallets import BASE, account_url, _ss58, rao_to_tao

# The site's verification widget can take over ten seconds to initialize.
PAGE_POLLS = 40
POLL_INTERVAL = 0.5

# Select only the current page's public table query. No direct API requests.
TABLE_QUERY = """
(() => {
  const root = document.querySelector('.ag-root-wrapper');
  if (!root) return null;
  let fiber = root[Object.keys(root).find(k => k.startsWith('__reactFiber'))];
  for (; fiber; fiber = fiber.return) {
    for (const client of [fiber.memoizedProps?.value,
                          fiber.memoizedProps?.client]) {
      if (!client?.getQueryCache) continue;
      const query = client.getQueryCache().getAll().find(q => {
        const [name, p] = q.queryKey;
        return name === NAME && (p?.address || p?.nominator || null) === OWNER
          && p.order === "timestamp_desc"
          && Number(p?.page || 1) === PAGE && q.getObserversCount() > 0
          && !p.netuid && !p.action && !p.amount_min && !p.amountMin;
      });
      if (!query) continue;
      return {status: query.state.status, data: query.state.data ?? null};
    }
  }
  return null;
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


def fetch_history(session, address, tab, pages):
    name = "delegate" if tab == "transactions" else "transfers"
    session.goto(account_url(address) + "/" + tab, check_google=False)
    return _read_pages(session, name, address, tab, pages)


def fetch_recent_trades(session, pages=1):
    """Read recent buyers and sellers from the chain-wide delegation grid."""
    session.goto(BASE + "/delegation", check_google=False)
    rows, coverage = _read_pages(session, "delegate", None, "transactions",
                                 pages)
    return [r for r in rows if r["kind"] in ("buy", "sell")], coverage


def _read_pages(session, name, address, tab, pages):
    events = {}
    incomplete = False
    for page in range(1, pages + 1):
        js = TABLE_QUERY.replace("NAME", json.dumps(name)).replace(
            "OWNER", json.dumps(address)).replace("PAGE", str(page))
        result = None
        for _ in range(PAGE_POLLS):
            state = session.sb.evaluate(js)
            if isinstance(state, dict) and state.get("status") == "success":
                result = state.get("data")
                break
            if isinstance(state, dict) and state.get("status") == "error":
                break
            time.sleep(POLL_INTERVAL)
        if not isinstance(result, dict) or not isinstance(
                result.get("data"), list):
            status = "partial" if page > 1 else "unavailable"
            return list(events.values()), status
        pagination = result.get("pagination") or {}
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
        if page == pages or not session.sb.evaluate(NEXT):
            break
        session.pause()
    return list(events.values()), "partial"
