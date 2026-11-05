"""Small text helpers shared by the SERP and Maps parsers."""
import re

REVIEWS_TOKEN_RE = re.compile(r"^\(([\d,]+)\)$")
_SUFFIX = {"k": 1_000, "m": 1_000_000}
_CURRENCY = {"$": "USD", "US $": "USD", "£": "GBP", "€": "EUR", "C $": "CAD",
             "AU $": "AUD", "¥": "JPY", "₹": "INR"}
_PRICE_RE = re.compile(
    r"(US \$|C \$|AU \$|[$£€¥₹]|USD|GBP|EUR|CAD|AUD)\s*"
    r"(\d{1,3}(?:[,.]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
)


def parse_price(text):
    """First price in a string: "$24.99" -> (24.99, "USD");
    "£1,299.00 to £1,399.00" -> (1299.0, "GBP"); "EUR 12,99" -> (12.99,
    "EUR"). Returns (None, None) when nothing price-like is present."""
    if not text:
        return None, None
    m = _PRICE_RE.search(text.replace("\xa0", " "))
    if not m:
        return None, None
    symbol, number = m.group(1), m.group(2)
    if "," in number and "." in number:
        number = number.replace(",", "") if number.rfind(".") > \
            number.rfind(",") else number.replace(".", "").replace(",", ".")
    elif "," in number:
        head, _, tail = number.rpartition(",")
        number = head + "." + tail if len(tail) <= 2 and head else \
            number.replace(",", "")
    try:
        value = float(number)
    except ValueError:
        return None, None
    currency = _CURRENCY.get(symbol, symbol if symbol.isalpha() else None)
    return value, currency


def parse_count(text):
    """'346' -> 346, '1,234' -> 1234, '1.7K' -> 1700, '2M' -> 2000000."""
    if text is None:
        return None
    m = re.match(r"^\s*([\d][\d,]*(?:\.\d+)?)\s*([KkMm])?\s*$", text)
    if not m:
        return None
    number = float(m.group(1).replace(",", ""))
    return int(round(number * _SUFFIX.get((m.group(2) or "").lower(), 1)))


def paren_count(node):
    """Review count from a text node that is exactly "(1,234)".

    Only whole text nodes are considered, so the area code of a phone
    number like "(512) 555-0100" is never mistaken for a review count."""
    for text in node.find_all(string=True):
        m = REVIEWS_TOKEN_RE.match(text.strip())
        if m:
            return int(m.group(1).replace(",", ""))
    return None
