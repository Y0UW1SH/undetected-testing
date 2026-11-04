"""Fetch and parse Google web-search results pages.

Parsing is structural rather than class-based wherever possible, because
Google's class names are hashed and rotate. The anchors that matter are:

* organic results: an <a href> that contains an <h3>
* ads: anything under #tads / #tadsb / #bottomads or [data-text-ad]
* People Also Ask: elements carrying a data-q attribute
* local pack: elements carrying a data-cid attribute (one per business)
* featured snippet: [data-tts="answers"] or [data-attrid="wa:/description"]
"""
import re
import urllib.parse

from bs4 import BeautifulSoup

from seo import models
from seo.geo import uule

SEARCH_URL = "https://www.google.com/search"
GOOGLE_HOSTS = ("google.", "googleusercontent.", "gstatic.", "youtube.")
RATING_RE = re.compile(
    r"Rated\s+([\d.]+)\s+out of\s+5(?:,)?\s*(?:([\d,]+)\s+reviews?)?", re.I
)
PAREN_COUNT_RE = re.compile(r"\(([\d,]+)\)")


def build_url(query, canonical_location=None, device="desktop", page=1,
              num=10, hl="en", gl="us"):
    params = {"q": query, "hl": hl, "gl": gl, "num": num, "pws": "0"}
    if page > 1:
        params["start"] = (page - 1) * num
    if canonical_location:
        params["uule"] = uule(canonical_location)
    # uule is already percent-encoded; keep it out of urlencode's hands.
    uule_value = params.pop("uule", None)
    url = "%s?%s" % (SEARCH_URL, urllib.parse.urlencode(params))
    if uule_value:
        url += "&uule=" + uule_value
    return url


def domain_of(url):
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return None
    return host[4:] if host.startswith("www.") else host or None


def _clean_href(href):
    """Google sometimes wraps links as /url?q=<real>&sa=..."""
    if not href:
        return None
    if href.startswith("/url?"):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        href = (qs.get("q") or qs.get("url") or [None])[0]
    if not href or not href.startswith(("http://", "https://")):
        return None
    return href


def _is_google_host(url):
    host = domain_of(url) or ""
    return any(host.startswith(g) or ("." + g) in host for g in GOOGLE_HOSTS)


def _text(node, limit=400):
    if node is None:
        return None
    text = " ".join(node.get_text(" ", strip=True).split())
    return text[:limit] if text else None


def _snippet_for(anchor):
    """Walk up from the title link to the result block and pull the
    description text out of it."""
    for depth, ancestor in enumerate(anchor.parents):
        if depth > 6 or ancestor.name in ("body", "html", None):
            break
        node = ancestor.select_one(
            '[data-sncf], .VwiC3b, div[style*="line-clamp"], '
            'div[data-content-feature="1"]'
        )
        if node is not None:
            return _text(node)
    # Fallback: block text minus the title.
    block = anchor.parent
    for _ in range(3):
        if block is not None and len(block.get_text(strip=True)) > 80:
            break
        block = block.parent if block is not None else None
    text = _text(block)
    if text and anchor.get_text(strip=True) in text:
        text = text.replace(anchor.get_text(strip=True), "", 1).strip()
    return text or None


def parse_serp(html, query, location="", device="desktop", page=1):
    """Return a dict with lists of SerpResult and LocalPackEntry."""
    soup = BeautifulSoup(html, "html.parser")
    out = {"organic": [], "ads": [], "paa": [], "featured": [],
           "local_pack": []}
    common = dict(query=query, location=location, device=device)

    # ---- ads -----------------------------------------------------------
    ad_urls = set()
    for container in soup.select("#tads, #tadsb, #bottomads, [data-text-ad]"):
        for a in container.select("a[href]"):
            url = _clean_href(a.get("href"))
            if not url or _is_google_host(url) or url in ad_urls:
                continue
            title = a.select_one('[role="heading"], h3, span')
            if title is None:
                continue
            ad_urls.add(url)
            out["ads"].append(models.SerpResult(
                position=len(out["ads"]) + 1, result_type="ad",
                url=url, domain=domain_of(url), title=_text(title),
                snippet=None, page=page, **common))

    # ---- featured snippet ----------------------------------------------
    fs = soup.select_one(
        '[data-tts="answers"], [data-attrid="wa:/description"]'
    )
    if fs is not None:
        block = fs
        link = None
        for _ in range(8):
            link = block.select_one("a[href] h3") if block else None
            if link is not None or block is None:
                break
            block = block.parent
        url = _clean_href(link.find_parent("a").get("href")) if link else None
        out["featured"].append(models.SerpResult(
            position=1, result_type="featured_snippet", url=url,
            domain=domain_of(url) if url else None,
            title=_text(link) if link else None, snippet=_text(fs),
            page=page, **common))

    # ---- People Also Ask -----------------------------------------------
    seen_q = set()
    for node in soup.select("[data-q]"):
        question = (node.get("data-q") or "").strip()
        if question and question not in seen_q:
            seen_q.add(question)
            out["paa"].append(models.SerpResult(
                position=len(out["paa"]) + 1, result_type="paa", url=None,
                domain=None, title=question, page=page, **common))

    # ---- local pack ----------------------------------------------------
    for card in soup.select("[data-cid]"):
        heading = card.select_one('[role="heading"], .dbg0pd, span.OSrXXb')
        name = _text(heading) if heading else None
        if not name:
            continue
        rating = reviews = None
        label_node = card.select_one("[aria-label*='out of 5']")
        if label_node is not None:
            m = RATING_RE.search(label_node.get("aria-label", ""))
            if m:
                rating = float(m.group(1))
                if m.group(2):
                    reviews = int(m.group(2).replace(",", ""))
        if reviews is None:
            m = PAREN_COUNT_RE.search(card.get_text(" ", strip=True))
            if m:
                reviews = int(m.group(1).replace(",", ""))
        lines = [ln.strip() for ln in card.get_text("\n").split("\n")
                 if ln.strip()]
        category = address = None
        for ln in lines:
            if "·" in ln:
                left, right = [p.strip() for p in ln.split("·", 1)]
                if not category and not any(ch.isdigit() for ch in left):
                    category = left
                    address = right
                    break
        card_text = card.get_text(" ", strip=True)
        is_ad = card.select_one("[data-text-ad]") is not None or (
            " Sponsored" in card_text or card_text.startswith("Ad "))
        out["local_pack"].append(models.LocalPackEntry(
            position=len(out["local_pack"]) + 1, name=name, rating=rating,
            reviews=reviews, category=category, address=address,
            is_ad=is_ad, **common))

    # ---- organic -------------------------------------------------------
    root = soup.select_one("#rso") or soup.select_one("#search") or soup
    seen = {r.url for r in out["featured"] if r.url}
    for h3 in root.select("a[href] h3"):
        a = h3.find_parent("a")
        url = _clean_href(a.get("href"))
        if not url or url in seen or url in ad_urls or _is_google_host(url):
            continue
        if a.find_parent(attrs={"data-q": True}) is not None:
            continue  # PAA expansion content
        if a.find_parent(attrs={"data-cid": True}) is not None:
            continue  # local pack
        if a.find_parent(id=re.compile("^(tads|tadsb|bottomads)$")):
            continue
        seen.add(url)
        out["organic"].append(models.SerpResult(
            position=len(out["organic"]) + 1, result_type="organic",
            url=url, domain=domain_of(url), title=_text(h3),
            snippet=_snippet_for(a), page=page, **common))
    return out


def fetch_serp(session, query, canonical_location=None, device="desktop",
               page=1, location_label=""):
    """Load one results page in the given seo.browser.Session and parse."""
    url = build_url(query, canonical_location, device=device, page=page)
    session.goto(url)
    session.scroll(times=3, amount=15)
    html = session.html()
    return parse_serp(html, query, location_label or canonical_location or "",
                      device, page)


def flatten(parsed):
    """All SerpResult / LocalPackEntry records from parse_serp() output."""
    records = []
    for key in ("organic", "ads", "paa", "featured", "local_pack"):
        records.extend(parsed.get(key, []))
    return records
