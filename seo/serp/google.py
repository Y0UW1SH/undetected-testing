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
from seo.local.maps import _card_details, _tokens
from seo.textutil import paren_count, parse_count

SEARCH_URL = "https://www.google.com/search"
GOOGLE_BASE = "https://www.google.com"
GOOGLE_HOSTS = ("google.", "googleusercontent.", "gstatic.", "youtube.")
# Google now links results through opaque redirects such as
# /goto?url=<token>; the destination is only visible in the <cite> text.
REDIRECT_PREFIXES = ("/goto?", "/url?", "/aclk?", "/interstitial?")
RATING_RE = re.compile(
    r"Rated\s+([\d.]+)\s+out of\s+5,?\s*"
    r"(?:([\d.,]+\s*[KkMm]?)\s+(?:user\s+)?reviews?)?", re.I
)
CITE_URL_RE = re.compile(r"https?://[^\s\u203a]+")
SHARED_IDS = ("rso", "search", "center_col", "main", "rcnt")


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


def _result_link(href):
    """(url, is_redirect) for a result anchor, or (None, False) when the
    anchor is navigation rather than a result. A Google redirect is made
    absolute so it still works when clicked."""
    if not href:
        return None, False
    if href.startswith("/url?"):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        real = (qs.get("q") or qs.get("url") or [None])[0]
        if real and real.startswith(("http://", "https://")):
            return real, False
        return GOOGLE_BASE + href, True
    if href.startswith(REDIRECT_PREFIXES):
        return GOOGLE_BASE + href, True
    if href.startswith(("http://", "https://")):
        if _is_google_host(href):
            return None, False
        return href, False
    return None, False


def _cite_url(block):
    """The visible URL Google prints under a result, if any."""
    for cite in block.select("cite"):
        m = CITE_URL_RE.search(cite.get_text(" ", strip=True))
        if m:
            return m.group(0).rstrip("/.,")
    return None


def _source_name(anchor, h3):
    """Site name Google shows next to the favicon ("Roto-Rooter",
    "Reddit · r/Austin"); the first anchor text line that is neither
    the title nor a URL fragment."""
    title = h3.get_text(strip=True)
    for line in anchor.get_text("\n", strip=True).split("\n"):
        line = line.strip()
        if not line or line == title or line.startswith(("http", "\u203a")):
            continue
        return line.split("\u00b7")[0].strip() or None
    return None


def _text(node, limit=400):
    if node is None:
        return None
    text = " ".join(node.get_text(" ", strip=True).split())
    return text[:limit] if text else None


def _result_block(anchor):
    """Smallest ancestor that holds this result but no other result's
    title link, so a result without a description never borrows the
    snippet of a neighbour."""
    block = anchor
    for _ in range(6):
        parent = block.parent
        if parent is None or parent.name in ("body", "html"):
            break
        if parent.get("id") in SHARED_IDS:
            break
        if len(parent.select("a[href] h3")) > 1:
            break
        block = parent
    return block


def _snippet_for(anchor):
    block = _result_block(anchor)
    node = block.select_one(
        '[data-sncf], .VwiC3b, div[style*="line-clamp"], '
        'div[data-content-feature="1"]'
    )
    if node is not None:
        return _text(node)
    text = _text(block)
    title = anchor.get_text(strip=True)
    if text and title in text:
        text = text.replace(title, "", 1).strip()
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
            url, redirect = _result_link(a.get("href"))
            title = a.select_one('[role="heading"], h3, span')
            if not url or title is None:
                continue
            cite = _cite_url(_result_block(a))
            if redirect and cite:
                url = cite
            if url in ad_urls:
                continue
            ad_urls.add(url)
            out["ads"].append(models.SerpResult(
                position=len(out["ads"]) + 1, result_type="ad",
                url=url, domain=domain_of(url) if not (redirect and not cite)
                else None, title=_text(title), snippet=None, page=page,
                **common))

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
    cards = soup.select("[data-cid]") or _label_cards(soup)
    for card in cards:
        entry = _local_entry(card, len(out["local_pack"]) + 1, common)
        if entry is not None:
            out["local_pack"].append(entry)

    # ---- organic -------------------------------------------------------
    root = soup.select_one("#rso") or soup.select_one("#search") or soup
    seen = {r.url for r in out["featured"] if r.url}
    for h3 in root.select("a[href] h3"):
        a = h3.find_parent("a")
        url, redirect = _result_link(a.get("href"))
        if not url or url in ad_urls:
            continue
        if a.find_parent(attrs={"data-q": True}) is not None:
            continue  # PAA expansion content
        if a.find_parent(attrs={"data-cid": True}) is not None:
            continue  # local pack
        if a.find_parent(id=re.compile("^(tads|tadsb|bottomads)$")):
            continue
        block = _result_block(a)
        cite = _cite_url(block)
        if redirect and cite:
            url, domain = cite, domain_of(cite)
        elif redirect:
            domain = None
        else:
            domain = domain_of(url)
        key = url if not redirect or cite else _text(h3)
        if key in seen:
            continue
        seen.add(key)
        out["organic"].append(models.SerpResult(
            position=len(out["organic"]) + 1, result_type="organic",
            url=url, domain=domain, title=_text(h3),
            snippet=_snippet_for(a), source=_source_name(a, h3),
            page=page, **common))
    return out


def _label_cards(soup):
    """Local-pack cards located from their "Rated X out of 5" labels when
    Google omits data-cid: the smallest ancestor of a label that holds a
    heading. Each card carries two labels (full and short), so the climb
    gives up when it would swallow a neighbouring card."""
    cards, seen = [], set()
    for label in soup.select("[aria-label^='Rated ']"):
        node = label
        card = None
        for _ in range(10):
            node = node.parent
            if node is None or node.name in ("body", "html"):
                break
            if len(node.select("[aria-label^='Rated ']")) > 2:
                break
            if node.select_one('[role="heading"]') is not None:
                card = node
                break
        if card is not None and id(card) not in seen:
            seen.add(id(card))
            cards.append(card)
    return cards


def _local_entry(card, position, common):
    heading = card.select_one('[role="heading"], .dbg0pd, span.OSrXXb')
    name = _text(heading) if heading else None
    if not name:
        return None
    rating = reviews = None
    for node in card.select("[aria-label*='out of 5']"):
        m = RATING_RE.search(node.get("aria-label", ""))
        if m:
            rating = float(m.group(1))
            if m.group(2):
                reviews = parse_count(m.group(2))
                break
    if reviews is None:
        reviews = paren_count(card)
    tokens = _tokens(card)
    category, address, status, phone = _card_details(card, tokens)
    is_ad = card.select_one("[data-text-ad]") is not None or (
        "Sponsored" in tokens)
    return models.LocalPackEntry(
        position=position, name=name, rating=rating, reviews=reviews,
        category=category, address=address, phone=phone, status=status,
        is_ad=is_ad, **common)


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
