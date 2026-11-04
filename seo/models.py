"""Typed records produced by the scrapers and stored by seo.storage."""
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Optional


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class _Record:
    def to_dict(self):
        return asdict(self)

    @classmethod
    def columns(cls):
        return [f.name for f in fields(cls)]


@dataclass
class SerpResult(_Record):
    """One entry on a Google web-search results page."""
    query: str
    location: str
    device: str
    position: int          # 1-based rank within its result type
    result_type: str       # organic, ad, paa, featured_snippet, video
    url: Optional[str]
    domain: Optional[str]
    title: Optional[str]
    snippet: Optional[str] = None
    page: int = 1
    fetched_at: str = field(default_factory=utcnow)


@dataclass
class LocalPackEntry(_Record):
    """One business in the SERP local 3-pack."""
    query: str
    location: str
    device: str
    position: int
    name: str
    rating: Optional[float] = None
    reviews: Optional[int] = None
    category: Optional[str] = None
    address: Optional[str] = None
    is_ad: bool = False
    fetched_at: str = field(default_factory=utcnow)


@dataclass
class Business(_Record):
    """A Google Maps listing, from the results feed or a place page."""
    query: str
    location: str
    position: int
    name: str
    rating: Optional[float] = None
    reviews: Optional[int] = None
    category: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    hours: Optional[str] = None
    maps_url: Optional[str] = None
    cid: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    fetched_at: str = field(default_factory=utcnow)


@dataclass
class GridCell(_Record):
    """Rank of a target business at one grid point."""
    keyword: str
    target: str
    center: str
    row: int
    col: int
    lat: float
    lng: float
    rank: Optional[int]      # None means not found in the feed
    top_name: Optional[str] = None
    fetched_at: str = field(default_factory=utcnow)


@dataclass
class NapRecord(_Record):
    """Name / address / phone as seen on one directory page."""
    business: str
    source: str
    url: str
    name_found: Optional[str]
    phone_found: Optional[str]
    address_found: Optional[str]
    phone_match: Optional[bool]
    address_match: Optional[bool]
    fetched_at: str = field(default_factory=utcnow)


@dataclass
class CanaryRun(_Record):
    """One Google query attempt, used to measure the challenge rate."""
    query: str
    outcome: str             # ok, challenge, consent, error
    detail: Optional[str] = None
    fetched_at: str = field(default_factory=utcnow)


class ChallengeError(RuntimeError):
    """Google served a CAPTCHA / 'unusual traffic' page."""
