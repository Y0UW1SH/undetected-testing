"""eBay search results.

Hooks: items are li elements under ul.srp-results with a title in
.s-item__title (older layout) or .s-card__title (2025 layout); the item
number is in the /itm/<id> link; price, condition and shipping have
stable class prefixes. The first "Shop on eBay" placeholder is skipped.
A bot wall shows the title "Pardon Our Interruption"."""
import re
import urllib.parse

from bs4 import BeautifulSoup

from seo import models
from seo.textutil import parse_count, parse_price

ITEM_RE = re.compile(r"/itm/(\d{9,15})")
RATING_RE = re.compile(r"([\d.]+)\s+out of\s+5")
COUNT_RE = re.compile(r"([\d.,]+[KkMm]?)")
BLOCK_TITLES = ("Pardon Our Interruption", "Security Measure",
                "Access Denied", "Error Page")
BLOCK_TEXT = ("Something went wrong on our end",
              "Please verify yourself to continue")


def build_url(query, page=1, site="www.ebay.com"):
    url = "https://%s/sch/i.html?_nkw=%s" % (
        site, urllib.parse.quote_plus(query))
    if page > 1:
        url += "&_pgn=%d" % page
    return url


def is_blocked(url, title, html):
    title = title or ""
    if any(t in title for t in BLOCK_TITLES):
        return "eBay bot check (%s)" % title
    if "/splashui/challenge" in (url or ""):
        return "eBay challenge page"
    sample = (html or "")[:200000]
    if any(t in sample for t in BLOCK_TEXT) and "srp-results" not in sample:
        return "eBay error page (soft block)"
    return None


def _text(node):
    return " ".join(node.get_text(" ", strip=True).split()) if node else None


def parse(html, query, page=1):
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("ul.srp-results") or soup
    results, seen = [], set()
    for li in root.select("li"):
        title_node = li.select_one(".s-item__title, .s-card__title")
        if title_node is None:
            continue
        badges = title_node.select(".LIGHT_HIGHLIGHT, .s-item__title--tag")
        for label in badges:
            label.decompose()          # "New Listing" badges
        title = _text(title_node)
        if not title or title.startswith("Shop on eBay"):
            continue
        link = li.select_one(
            'a.s-item__link[href], a.s-card__link[href], a[href*="/itm/"]')
        href = link.get("href", "") if link else ""
        m = ITEM_RE.search(href)
        item_id = m.group(1) if m else None
        key = item_id or title
        if key in seen:
            continue
        seen.add(key)
        price_text = _text(li.select_one(".s-item__price, .s-card__price"))
        price, currency = parse_price(price_text)
        rating = reviews = None
        rating_node = li.select_one(
            ".x-star-rating .clipped, [aria-label*='out of 5 stars']")
        if rating_node is not None:
            m = RATING_RE.search(rating_node.get("aria-label")
                                 or rating_node.get_text())
            if m:
                rating = float(m.group(1))
        count_node = li.select_one(
            ".s-item__reviews-count, .s-card__reviews-count")
        if count_node is not None:
            m = COUNT_RE.search(count_node.get_text())
            if m:
                reviews = parse_count(m.group(1))
        text = li.get_text(" ", strip=True)
        sponsored = (li.select_one('[aria-label*="Sponsored" i]') is not None
                     or "Sponsored" in text)
        results.append(models.Listing(
            source="ebay", query=query, position=len(results) + 1,
            title=title,
            url="https://www.ebay.com/itm/%s" % item_id if item_id else
            (href.split("?")[0] or None),
            item_id=item_id, price=price, currency=currency,
            price_text=price_text, rating=rating, reviews=reviews,
            is_sponsored=sponsored,
            condition=_text(li.select_one(
                ".SECONDARY_INFO, .s-card__subtitle")),
            shipping=_text(li.select_one(
                ".s-item__shipping, .s-item__logisticsCost, "
                "[class*='shipping']")),
            page=page))
    return results


def fetch(session, query, pages=1, site="www.ebay.com"):
    results = []
    for page in range(1, pages + 1):
        session.goto(build_url(query, page, site), check_google=False,
                     blocked=is_blocked)
        session.scroll(times=3, amount=15)
        batch = parse(session.html(), query, page)
        for item in batch:
            item.position += len(results)
        results.extend(batch)
        if page < pages:
            session.pause()
    return results
