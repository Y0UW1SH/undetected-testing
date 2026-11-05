"""Small text helpers shared by the SERP and Maps parsers."""
import re

REVIEWS_TOKEN_RE = re.compile(r"^\(([\d,]+)\)$")
_SUFFIX = {"k": 1_000, "m": 1_000_000}


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
