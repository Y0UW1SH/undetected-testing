"""NAP (name / address / phone) consistency across directory sites."""
import re

from bs4 import BeautifulSoup

from seo import models
from seo.serp.google import fetch_serp

DIRECTORIES = {
    "yelp.com": "Yelp",
    "bbb.org": "BBB",
    "yellowpages.com": "Yellow Pages",
    "facebook.com": "Facebook",
    "mapquest.com": "MapQuest",
    "foursquare.com": "Foursquare",
    "manta.com": "Manta",
    "angi.com": "Angi",
    "nextdoor.com": "Nextdoor",
    "apple.com": "Apple Maps",
    "bingplaces.com": "Bing Places",
}

PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]?)?\(?\b(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})\b"
)
_ABBREV = {
    "street": "st", "avenue": "ave", "boulevard": "blvd", "road": "rd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl",
    "suite": "ste", "north": "n", "south": "s", "east": "e", "west": "w",
    "highway": "hwy", "parkway": "pkwy",
}
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def normalize_phone(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def extract_phones(text):
    found = []
    for m in PHONE_RE.finditer(text or ""):
        digits = "".join(m.groups())
        if digits not in found:
            found.append(digits)
    return found


def normalize_address(value):
    words = _NON_ALNUM.sub(" ", (value or "").lower()).split()
    words = [_ABBREV.get(w, w) for w in words]
    return [w for w in words if w not in ("ste", "unit", "apt", "#")]


def address_matches(reference, text):
    """True if the street number plus street name (and zip, if given)
    from `reference` all appear in `text`."""
    ref = normalize_address(reference)
    if not ref:
        return None
    hay = " ".join(normalize_address(text))
    number = ref[0] if ref[0].isdigit() else None
    street = [w for w in ref[1:3] if not w.isdigit()]
    zips = [w for w in ref if len(w) == 5 and w.isdigit()]
    checks = []
    if number:
        checks.append(" %s " % number in " %s " % hay)
    checks.extend(w in hay for w in street)
    checks.extend(" %s " % z in " %s " % hay for z in zips)
    return bool(checks) and all(checks)


def directory_for(url):
    host = re.sub(r"^https?://(www\.)?", "", url or "").split("/")[0]
    for domain, label in DIRECTORIES.items():
        if host == domain or host.endswith("." + domain):
            return label
    return None


def filter_directory_results(results):
    """Keep SERP results that point at known directory sites, de-duped
    per directory (first hit wins)."""
    picked, seen = [], set()
    for r in results:
        label = directory_for(r.url)
        if label and label not in seen:
            seen.add(label)
            picked.append((label, r))
    return picked


def page_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def audit_page(html, business, source, url, phone, address):
    text = page_text(html)
    phones = extract_phones(text)
    want = normalize_phone(phone)
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None
    return models.NapRecord(
        business=business, source=source, url=url, name_found=title,
        phone_found=", ".join(phones[:3]) or None,
        address_found=None,
        phone_match=(want in phones) if want and phones else None,
        address_match=address_matches(address, text) if address else None)


def audit(session, business, phone, address, city=""):
    """Search Google for the business and check each directory hit."""
    query = '"%s" %s' % (business, phone or city)
    parsed = fetch_serp(session, query, location_label=city)
    hits = filter_directory_results(parsed["organic"])
    records = []
    for label, result in hits:
        session.goto(result.url, check_google=False)
        records.append(audit_page(session.html(), business, label,
                                  result.url, phone, address))
        session.pause()
    return records
