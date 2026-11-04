"""Measure how often Google challenges this runner's IP.

Runs N harmless queries in fresh sessions and records ok / challenge /
error for each. The challenge rate tells you whether an environment
(GitHub Actions, a VPS, a proxy) is usable for SERP scraping."""
from seo import models
from seo.browser import Session
from seo.models import ChallengeError
from seo.serp.google import build_url

DEFAULT_QUERIES = [
    "weather tomorrow", "python list comprehension", "best pizza near me",
    "how to tie a tie", "convert 5 km to miles", "local time in tokyo",
    "chess openings for beginners", "sourdough starter recipe",
    "what is a 401k", "how many ounces in a cup",
]


def run(queries=None, per_session=1, mobile=False, proxy=None,
        session_factory=Session):
    queries = list(queries or DEFAULT_QUERIES)
    records = []
    i = 0
    while i < len(queries):
        batch = queries[i:i + per_session]
        i += per_session
        try:
            with session_factory(mobile=mobile, proxy=proxy) as s:
                for q in batch:
                    records.append(_one(s, q))
                    if records[-1].outcome == "challenge":
                        break   # this IP is burnt; new session next
        except Exception as e:  # browser failed to start, etc.
            records.append(models.CanaryRun(
                query=batch[0], outcome="error", detail=str(e)[:200]))
    return records


def _one(session, query):
    try:
        session.goto(build_url(query))
        return models.CanaryRun(query=query, outcome="ok",
                                detail=session.title()[:120])
    except ChallengeError as e:
        return models.CanaryRun(query=query, outcome="challenge",
                                detail=str(e)[:200])
    except Exception as e:
        return models.CanaryRun(query=query, outcome="error",
                                detail=str(e)[:200])


def summarize(records):
    total = len(records)
    counts = {}
    for r in records:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    rate = 100.0 * counts.get("challenge", 0) / total if total else 0.0
    return {"total": total, "challenge_rate_pct": round(rate, 1), **counts}
