"""Small text helpers shared by the SERP and Maps parsers."""
import re

REVIEWS_TOKEN_RE = re.compile(r"^\(([\d,]+)\)$")


def paren_count(node):
    """Review count from a text node that is exactly "(1,234)".

    Only whole text nodes are considered, so the area code of a phone
    number like "(512) 555-0100" is never mistaken for a review count."""
    for text in node.find_all(string=True):
        m = REVIEWS_TOKEN_RE.match(text.strip())
        if m:
            return int(m.group(1).replace(",", ""))
    return None
