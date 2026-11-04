"""Google Maps: search-results feed and place-page scraping."""
import re
import time
import urllib.parse

from bs4 import BeautifulSoup

from seo import models

MAPS_SEARCH = "https://www.google.com/maps/search/{q}/@{lat},{lng},{zoom}z"
MAPS_SEARCH_NOGEO = "https://www.google.com/maps/search/{q}"
STARS_RE = re.compile(r"([\d.]+)\s+stars?\s*(?:([\d,]+)\s+reviews?)?", re.I)
PAREN_RE = re.compile(r"\(([\d,]+)\)")
PHONE_RE = re.compile(r"^\+?[\d(][\d\s().-]{6,}\d$")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")
COORD_RE = re.compile(r"!3d(-?[\d.]+)!4d(-?[\d.]+)")
FEATURE_RE = re.compile(r"!1s(0x[0-9a-f]+):(0x[0-9a-f]+)", re.I)
END_OF_LIST = "You've reached the end of the list"


def build_maps_url(query, lat=None, lng=None, zoom=14, hl="en"):
    q = urllib.parse.quote(query)
    if lat is None or lng is None:
        url = MAPS_SEARCH_NOGEO.format(q=q)
    else:
        url = MAPS_SEARCH.format(q=q, lat=lat, lng=lng, zoom=zoom)
    return url + "?hl=%s" % hl


def cid_from_href(href):
    """The second hex value of the feature id is the CID in decimal."""
    m = FEATURE_RE.search(href or "")
    return str(int(m.group(2), 16)) if m else None


def coords_from_href(href):
    m = COORD_RE.search(href or "")
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def _text(node, limit=300):
    if node is None:
        return None
    text = " ".join(node.get_text(" ", strip=True).split())
    return text[:limit] or None


def _rating_reviews(card):
    rating = reviews = None
    for node in card.select("[aria-label]"):
        m = STARS_RE.search(node.get("aria-label", ""))
        if m:
            rating = float(m.group(1))
            if m.group(2):
                reviews = int(m.group(2).replace(",", ""))
            break
    if reviews is None:
        m = PAREN_RE.search(card.get_text(" ", strip=True))
        if m:
            reviews = int(m.group(1).replace(",", ""))
    return rating, reviews


def _tokens(card):
    """Text lines of a card with every "·" separator as its own token,
    whether Google rendered it inline ("Plumber · 1 Main St") or as a
    standalone element between two lines."""
    tokens = []
    for ln in card.get_text("\n").split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        if "·" in ln:
            for j, part in enumerate(ln.split("·")):
                if j:
                    tokens.append("·")
                if part.strip():
                    tokens.append(part.strip())
        else:
            tokens.append(ln)
    return tokens


def _card_details(card):
    """category, address, status, phone from the "a · b" pairs."""
    tokens = _tokens(card)
    category = address = status = phone = None
    for i, tok in enumerate(tokens):
        if tok != "·" or i == 0 or i + 1 >= len(tokens):
            continue
        left, right = tokens[i - 1], tokens[i + 1]
        if phone is None and PHONE_RE.match(right):
            phone, status = right, left
        elif category is None and not any(c.isdigit() for c in left) \
                and left != "Sponsored":
            category, address = left, right
    return category, address, status, phone


def _is_ad(card, tokens):
    if card.select_one('button[aria-label*="Why this ad"]') is not None:
        return True
    return "Sponsored" in tokens[:3]


def _website(card):
    node = card.select_one(
        'a[aria-label^="Visit "], a[data-value="Website"]')
    return node.get("href") if node is not None else None


def parse_feed(html, query, location=""):
    """Businesses in the left-hand results feed, in display order."""
    soup = BeautifulSoup(html, "html.parser")
    feed = soup.select_one('div[role="feed"]') or soup
    results, seen = [], set()
    for a in feed.select('a[href*="/maps/place/"]'):
        name = (a.get("aria-label") or "").strip() or _text(a)
        href = a.get("href") or ""
        key = cid_from_href(href) or href or name
        if not name or key in seen:
            continue
        seen.add(key)
        card = a.parent if a.parent is not None else a
        for _ in range(2):
            if card.parent is not None and card.parent.name == "div" and \
                    card.parent.get("role") != "feed":
                card = card.parent
            else:
                break
        rating, reviews = _rating_reviews(card)
        category, address, status, phone = _card_details(card)
        lat, lng = coords_from_href(href)
        results.append(models.Business(
            query=query, location=location, position=len(results) + 1,
            name=name, rating=rating, reviews=reviews, category=category,
            address=address, phone=phone, website=_website(card),
            maps_url=href, cid=cid_from_href(href), lat=lat, lng=lng,
            is_ad=_is_ad(card, _tokens(card)), status=status))
    return results


def parse_place(html):
    """Details from a single place page. Any field may be None."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one('div[role="main"]') or soup
    info = {}
    h1 = main.select_one("h1")
    info["name"] = _text(h1)
    rating, reviews = _rating_reviews(main)
    if reviews is None:
        node = main.select_one(
            '[aria-label$="reviews"], [aria-label$="review"]'
        )
        if node is not None:
            m = re.search(r"([\d,]+)\s+review", node.get("aria-label", ""))
            if m:
                reviews = int(m.group(1).replace(",", ""))
    info["rating"], info["reviews"] = rating, reviews
    info["category"] = _text(main.select_one('button[jsaction*="category"]'))
    addr = main.select_one('button[data-item-id="address"]')
    info["address"] = None
    if addr is not None:
        label = addr.get("aria-label") or _text(addr) or ""
        info["address"] = label.split(":", 1)[-1].strip() or None
    phone = main.select_one('button[data-item-id^="phone:tel:"]')
    info["phone"] = None
    if phone is not None:
        info["phone"] = phone.get("data-item-id")[len("phone:tel:"):]
    site = main.select_one('a[data-item-id="authority"]')
    info["website"] = site.get("href") if site is not None else None
    info["hours"] = _hours(main)
    return info


def _hours(main):
    rows = []
    for tr in main.select("table tr"):
        cells = [_text(td) for td in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if len(cells) >= 2 and cells[0].lower().startswith(WEEKDAYS):
            rows.append("%s: %s" % (cells[0], cells[1]))
    if rows:
        return "; ".join(rows)
    node = main.select_one('[jsaction*="openhours"] [aria-label]')
    if node is not None:
        return node.get("aria-label")
    return None


def scroll_feed(session, max_results=20, max_rounds=15, pause=1.2):
    """Scroll the results feed until enough entries load or it ends."""
    js_scroll = (
        "(function(){var f=document.querySelector('div[role=\"feed\"]');"
        "if(f){f.scrollBy(0, 3000);}return f?f.querySelectorAll("
        "'a[href*=\"/maps/place/\"]').length:0;})()"
    )
    last = -1
    for _ in range(max_rounds):
        count = session.sb.evaluate(js_scroll) or 0
        time.sleep(pause)
        if count >= max_results or END_OF_LIST in session.html():
            break
        if count == last:
            break
        last = count


def fetch_listings(session, query, lat=None, lng=None, location_label="",
                   max_results=20, details=False, zoom=14):
    """Search Maps and return Business records, optionally enriched by
    visiting each place page (slow: one page load per business)."""
    session.goto(build_maps_url(query, lat, lng, zoom))
    scroll_feed(session, max_results=max_results)
    businesses = parse_feed(session.html(), query, location_label)
    businesses = businesses[:max_results]
    if details:
        for biz in businesses:
            if not biz.maps_url:
                continue
            session.goto(biz.maps_url)
            info = parse_place(session.html())
            biz.hours = info.get("hours")
            for key in ("phone", "website", "rating", "reviews",
                        "category", "address"):
                if info.get(key) is not None:
                    setattr(biz, key, info.get(key))
            session.pause()
    return businesses
