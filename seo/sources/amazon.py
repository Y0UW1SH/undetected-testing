"""Amazon search results.

Hooks: each result is div[data-component-type="s-search-result"] with a
data-asin attribute; title in the h2; price in span.a-price
span.a-offscreen; rating in the "4.5 out of 5 stars" icon text; review
count in the link whose aria-label ends with "ratings"."""
import re
import urllib.parse

from bs4 import BeautifulSoup

from seo import models
from seo.textutil import parse_count, parse_price

RATING_RE = re.compile(r"([\d.]+)\s+out of\s+5")
COUNT_RE = re.compile(r"^\s*([\d.,]+[KkMm]?)")
BLOCK_TITLES = ("Robot Check", "Sorry! Something went wrong",
                "Amazon.com: Robot Check", "Bot Check")


def build_url(query, page=1, domain="amazon.com"):
    url = "https://www.%s/s?k=%s" % (domain, urllib.parse.quote_plus(query))
    if page > 1:
        url += "&page=%d" % page
    return url


def is_blocked(url, title, html):
    title = title or ""
    if any(title.startswith(t) for t in BLOCK_TITLES):
        return "Amazon bot check (%s)" % title
    sample = (html or "")[:60000]
    if "validateCaptcha" in sample or "Enter the characters you see" in sample:
        return "Amazon captcha page"
    return None


def _text(node):
    return " ".join(node.get_text(" ", strip=True).split()) if node else None


def parse(html, query, page=1, domain="amazon.com"):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for div in soup.select(
            'div[data-component-type="s-search-result"][data-asin]'):
        asin = (div.get("data-asin") or "").strip()
        h2 = div.select_one("h2")
        title = _text(h2.select_one("span") or h2) if h2 else None
        if not asin or not title:
            continue
        link = div.select_one("h2 a[href]") or div.select_one(
            'a[href*="/dp/"]')
        href = link.get("href", "") if link else ""
        price_node = div.select_one("span.a-price span.a-offscreen")
        price_text = _text(price_node)
        price, currency = parse_price(price_text)
        rating = None
        rating_node = div.select_one(
            'i[class*="a-icon-star"] span.a-icon-alt, '
            '[aria-label*="out of 5 stars"]')
        if rating_node is not None:
            m = RATING_RE.search(rating_node.get("aria-label")
                                 or rating_node.get_text())
            if m:
                rating = float(m.group(1))
        reviews = None
        count_node = div.select_one(
            'a[aria-label*="ratings"], a[aria-label*="rating"], '
            "span.s-underline-text")
        if count_node is not None:
            m = COUNT_RE.match(count_node.get("aria-label")
                               or count_node.get_text())
            if m:
                reviews = parse_count(m.group(1))
        text = div.get_text(" ", strip=True)
        sponsored = ("/sspa/" in href or "Sponsored" in text
                     or div.select_one(
                         '[data-component-type="sp-sponsored-result"]')
                     is not None)
        results.append(models.Listing(
            source="amazon", query=query, position=len(results) + 1,
            title=title, url="https://www.%s/dp/%s" % (domain, asin),
            item_id=asin, price=price, currency=currency,
            price_text=price_text, rating=rating, reviews=reviews,
            is_sponsored=sponsored,
            prime=div.select_one("i.a-icon-prime") is not None, page=page))
    return results


def fetch(session, query, pages=1, domain="amazon.com"):
    results = []
    for page in range(1, pages + 1):
        session.goto(build_url(query, page, domain), check_google=False,
                     blocked=is_blocked)
        session.scroll(times=3, amount=15)
        batch = parse(session.html(), query, page, domain)
        for item in batch:
            item.position += len(results)
        results.extend(batch)
        if page < pages:
            session.pause()
    return results
