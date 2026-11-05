"""taostats.io: Bittensor subnet stats and prices.

The parser is structural: it looks for the data table (a <table>, or a
[role="table"]/[role="grid"] made of [role="row"] cells) whose header
mentions a price, maps columns by fuzzy header names, and keeps the
whole row as JSON so nothing is lost when the site adds columns."""
import json
import time
import re

from bs4 import BeautifulSoup

from seo import models

SUBNETS_URL = "https://taostats.io/subnets"
BLOCK_MARKERS = ("Just a moment", "Attention Required", "Access denied",
                 "Access Denied", "cf-chl-widget", "challenge-platform")

# field -> header regex, checked in order; the first match wins.
HEADER_MAP = (
    ("price_usd", re.compile(r"price.*(usd|\$)|usd.*price", re.I)),
    ("price_tao", re.compile(r"price", re.I)),
    ("change_24h_pct", re.compile(r"\b(24\s*h|1\s*d|daily)\b", re.I)),
    ("change_7d_pct", re.compile(r"\b7\s*d\b|week", re.I)),
    ("market_cap_tao", re.compile(r"market\s*cap|mcap", re.I)),
    ("emission_pct", re.compile(r"emission", re.I)),
    ("volume_24h_tao", re.compile(r"\bvol", re.I)),
    ("tao_in_pool", re.compile(r"pool|liquidity|reserve", re.I)),
    ("netuid", re.compile(r"^(net\s*uid|#|id|uid|sn)$", re.I)),
    ("name", re.compile(r"^(name|subnet(\s*name)?)$", re.I)),
)
NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?|[-+]?\.\d+")
SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}
NAME_PREFIX_RE = re.compile(
    r"^(?:SN|#|subnet)?\s*(\d{1,3})\s*[·•:\-–|/]\s*(.+)$",
    re.I)
NAME_SUFFIX_RE = re.compile(r"^(.*?)\s*[·•(\-]\s*(?:SN|#)\s*(\d{1,3})\)?$",
                            re.I)


def build_url(netuid=None):
    if netuid is None:
        return SUBNETS_URL
    return "%s/%d" % (SUBNETS_URL, int(netuid))


def is_blocked(url, title, html):
    title = title or ""
    sample = (html or "")[:30000]
    for marker in BLOCK_MARKERS:
        if marker in title or marker in sample:
            return "taostats.io bot wall (%s)" % marker
    return None


def parse_number(text):
    """'τ0.0423' -> 0.0423, '+12.4%' -> 12.4, '−3.1%' -> -3.1,
    'τ1.2M' -> 1200000.0, '$4,201' -> 4201.0, '-' -> None."""
    if text is None:
        return None
    s = str(text).replace("−", "-").replace("–", "-")
    s = s.replace("TAO", "").replace("τ", "").replace("$", "")
    s = s.replace("%", "").replace("\xa0", " ").strip()
    m = NUMBER_RE.search(s)
    if not m:
        return None
    try:
        value = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    rest = s[m.end():].strip().lower()
    if rest[:1] in SUFFIX and (len(rest) == 1 or not rest[1].isalpha()):
        value *= SUFFIX[rest[:1]]
    return value


def _text(node):
    return " ".join(node.get_text(" ", strip=True).split())


def _tables(soup):
    """Yield (headers, rows) for every table-like structure."""
    for table in soup.select("table"):
        trs = table.select("tr")
        if not trs:
            continue
        header_tr = next((tr for tr in trs if tr.select("th")), trs[0])
        headers = [_text(c) for c in header_tr.find_all(["th", "td"])]
        rows = []
        for tr in trs:
            if tr is header_tr:
                continue
            cells = [_text(c) for c in tr.find_all(["td", "th"])]
            if any(cells):
                rows.append(cells)
        yield headers, rows
    for grid in soup.select('[role="table"], [role="grid"]'):
        role_rows = grid.select('[role="row"]')
        if not role_rows:
            continue
        header_row = next(
            (r for r in role_rows if r.select('[role="columnheader"]')),
            role_rows[0])
        headers = [_text(c) for c in header_row.select(
            '[role="columnheader"], [role="cell"], [role="gridcell"]')]
        rows = []
        for r in role_rows:
            if r is header_row:
                continue
            cells = [_text(c) for c in r.select(
                '[role="cell"], [role="gridcell"]')]
            if any(cells):
                rows.append(cells)
        yield headers, rows


def _column_map(headers):
    mapping = {}
    for idx, header in enumerate(headers):
        for field, pattern in HEADER_MAP:
            if field in mapping:
                continue
            if pattern.search(header or ""):
                mapping[field] = idx
                break
    return mapping


def _netuid_and_name(cells, mapping):
    netuid = name = None
    if "netuid" in mapping and mapping["netuid"] < len(cells):
        m = re.search(r"\d+", cells[mapping["netuid"]])
        if m:
            netuid = int(m.group(0))
    if "name" in mapping and mapping["name"] < len(cells):
        name = cells[mapping["name"]].strip() or None
    if name:
        m = NAME_PREFIX_RE.match(name)
        if m and (netuid is None or int(m.group(1)) == netuid):
            netuid = int(m.group(1))
            name = m.group(2).strip()
        else:
            m = NAME_SUFFIX_RE.match(name)
            if m and (netuid is None or int(m.group(2)) == netuid):
                netuid = int(m.group(2))
                name = m.group(1).strip()
    return netuid, name


# taostats.io renders the subnet list with AG Grid: no <table>, cells
# carry col-id attributes, each row is split between a pinned-left
# container (rank, "name SN<netuid>") and the centre container (data),
# joined by row-index, and the row-id is "row-<netuid>".
AG_FIELDS = {
    "price_tao": "price",
    "price_usd": "price_usd",
    "change_24h_pct": "price_change_one_day",
    "change_7d_pct": "price_change_one_week",
    "market_cap_tao": "market_cap",
    "emission_pct": "emission_total_pct",
    "volume_24h_tao": "tao_volume_one_day",
    "tao_in_pool": "liquidity",
}
AG_ROW_ID_RE = re.compile(r"row-(\d+)$")
AG_NAME_RE = re.compile(r"^(.*?)\s*SN\s*(\d{1,3})\s*$")


def _ag_grid(soup):
    """(headers, rows) from the AG Grid that has a price column, or
    (None, []) when the page has no such grid."""
    grid = None
    for root in soup.select(".ag-root-wrapper, .ag-root"):
        if root.select_one('[role="columnheader"][col-id="price"]'):
            grid = root
            break
    if grid is None:
        if soup.select_one('[role="columnheader"][col-id="price"]'):
            grid = soup
        else:
            return None, []
    headers = {h.get("col-id"): _text(h)
               for h in grid.select('[role="columnheader"][col-id]')}
    rows = {}
    for row in grid.select('[role="row"][row-index]'):
        if row.find_parent(class_=re.compile(r"ag-(floating|header)")):
            continue                      # totals row / header row
        rec = rows.setdefault(row.get("row-index"),
                              {"_row_id": row.get("row-id")})
        for cell in row.select('[role="gridcell"][col-id]'):
            rec[cell["col-id"]] = _text(cell)
    return headers, list(rows.values())


def _ag_value(text):
    if text is None:
        return None
    cleaned = text.replace("Bittensor", "").replace("τ", "").strip()
    if cleaned in ("", "-", "—"):
        return None
    value = parse_number(cleaned)
    return round(value, 6) if value is not None else None   # 130.98K*1e3


def _parse_ag_grid(soup, source_url):
    headers, rows = _ag_grid(soup)
    results = {}
    for rec in rows:
        netuid = name = None
        m = AG_ROW_ID_RE.search(rec.get("_row_id") or "")
        if m:
            netuid = int(m.group(1))
        label = rec.get("netuid") or ""
        m = AG_NAME_RE.match(label)
        if m:
            name = m.group(1).strip() or None
            netuid = netuid if netuid is not None else int(m.group(2))
        elif label:
            name = label.strip()
        if netuid is None or netuid in results:
            continue
        values = {field: _ag_value(rec.get(col))
                  for field, col in AG_FIELDS.items()}
        raw = {headers.get(k, k): v for k, v in rec.items()
               if k != "_row_id"}
        results[netuid] = models.SubnetStat(
            netuid=netuid, name=name, source_url=source_url,
            raw=json.dumps(raw, ensure_ascii=False), **values)
    return results


def parse_subnets(html, source_url=SUBNETS_URL):
    soup = BeautifulSoup(html, "html.parser")
    results = _parse_ag_grid(soup, source_url)
    if results:
        return [results[k] for k in sorted(results)]
    for headers, rows in _tables(soup):
        if not any(re.search(r"price", h or "", re.I) for h in headers):
            continue
        mapping = _column_map(headers)
        for cells in rows:
            netuid, name = _netuid_and_name(cells, mapping)
            if netuid is None or netuid in results:
                continue
            values = {}
            for field in ("price_tao", "price_usd", "change_24h_pct",
                          "change_7d_pct", "market_cap_tao",
                          "emission_pct", "volume_24h_tao", "tao_in_pool"):
                idx = mapping.get(field)
                values[field] = parse_number(cells[idx]) \
                    if idx is not None and idx < len(cells) else None
            raw = json.dumps(dict(zip(headers, cells)), ensure_ascii=False)
            results[netuid] = models.SubnetStat(
                netuid=netuid, name=name, raw=raw, source_url=source_url,
                **values)
    return [results[k] for k in sorted(results)]


NEXT_PAGE_JS = (
    # The site renders its own pager (Previous, 1..N, Next) beside each
    # grid; AG Grid's built-in paging panel is present but inert. Find
    # the grid that has the price column, walk up to the container that
    # holds a "Next" button, and click it unless it is disabled.
    "(function(){var grids=Array.from(document.querySelectorAll("
    "'.ag-root-wrapper'));var grid=grids.find(function(g){return "
    "g.querySelector('[role=columnheader][col-id=\"price\"]');});"
    "if(!grid){return 'none';}var node=grid,btn=null;"
    "for(var i=0;i<8&&node;i++){node=node.parentElement;if(!node){break;}"
    "btn=Array.from(node.querySelectorAll('button')).find(function(b){"
    "return (b.innerText||'').trim()==='Next';});if(btn){break;}}"
    "if(!btn){var ag=document.querySelector("
    "'.ag-paging-panel [aria-label=\"Next Page\"]');if(!ag){return 'none';}"
    "if(ag.classList.contains('ag-disabled')){return 'disabled';}"
    "ag.click();return 'clicked';}"
    "if(btn.disabled||btn.getAttribute('aria-disabled')==='true'){"
    "return 'disabled';}btn.click();return 'clicked';})()"
)


def fetch_subnets(session, max_rounds=40):
    """Load the subnets page and collect every row. The AG Grid is
    paginated (25 rows per page, aria-rowcount gives the total), so the
    "Next Page" button is clicked until it is disabled and the pages are
    merged by netuid. Window scrolling is the fallback for a plain
    table that loads more rows as you scroll."""
    session.goto(SUBNETS_URL, check_google=False, blocked=is_blocked)
    session.scroll(times=3, amount=15)
    merged = {s.netuid: s for s in parse_subnets(session.html())}
    stale = 0
    for _ in range(max_rounds):
        try:
            state = session.sb.evaluate(NEXT_PAGE_JS)
        except Exception:
            state = "none"
        if state == "clicked":
            time.sleep(1.0)
        elif state == "disabled":
            break
        else:
            session.scroll(times=4, amount=20)
        before = len(merged)
        for stat in parse_subnets(session.html()):
            merged.setdefault(stat.netuid, stat)
        if len(merged) == before:
            stale += 1
            if stale >= 2:
                break
        else:
            stale = 0
    return [merged[k] for k in sorted(merged)]
