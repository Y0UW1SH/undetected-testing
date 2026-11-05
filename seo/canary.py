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
    """Every query ends up with exactly one record: ok, challenge, error,
    or skipped (not attempted because its session was abandoned after a
    challenge or failed to start), so summarize() can report the
    challenge rate over the queries that were really attempted."""
    queries = list(queries or DEFAULT_QUERIES)
    records = []
    for start in range(0, len(queries), per_session):
        batch = queries[start:start + per_session]
        attempted = 0
        try:
            with session_factory(mobile=mobile, proxy=proxy) as s:
                for q in batch:
                    attempted += 1
                    rec = _one(s, q)
                    records.append(rec)
                    if rec.outcome == "challenge":
                        break   # this IP/session is burnt
        except Exception as e:  # browser failed to start or to close
            if attempted < len(batch):
                records.append(models.CanaryRun(
                    query=batch[attempted], outcome="error",
                    detail=str(e)[:200]))
                attempted += 1
        for q in batch[attempted:]:
            records.append(models.CanaryRun(
                query=q, outcome="skipped",
                detail="not attempted: session abandoned"))
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
    counts = {}
    for r in records:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    attempted = sum(v for k, v in counts.items() if k != "skipped")
    rate = 100.0 * counts.get("challenge", 0) / attempted if attempted else 0
    return {"total": len(records), "attempted": attempted,
            "challenge_rate_pct": round(rate, 1), **counts}
