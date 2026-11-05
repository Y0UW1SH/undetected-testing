"""Reddit search.

Primary path: load the JSON endpoint (search.json) in the stealth
browser and parse the listing. Fallback: the HTML search page, whose
<shreddit-post> elements expose title, subreddit, score, comment count
and permalink as attributes."""
import json
import urllib.parse

from bs4 import BeautifulSoup

from seo import models

SORTS = ("relevance", "hot", "top", "new", "comments")
TIMES = ("hour", "day", "week", "month", "year", "all")


def build_url(query, subreddit=None, sort="relevance", time="all",
              limit=25, as_json=True):
    base = "https://www.reddit.com/"
    if subreddit:
        base += "r/%s/" % subreddit.strip("/").replace("r/", "")
    base += "search" + (".json" if as_json else "/")
    params = {"q": query, "sort": sort, "t": time, "limit": limit}
    if subreddit:
        params["restrict_sr"] = 1
    if as_json:
        params["raw_json"] = 1
    return base + "?" + urllib.parse.urlencode(params)


def is_blocked(url, title, html):
    title = title or ""
    sample = (html or "")[:20000]
    if "blocked by network security" in sample or "Too Many Requests" in title:
        return "Reddit rate limit / network block"
    if '"error": 429' in sample or '"error":429' in sample:
        return "Reddit 429"
    return None


def _json_text(html):
    stripped = (html or "").lstrip()
    if stripped.startswith("{"):
        return stripped
    soup = BeautifulSoup(html, "html.parser")
    pre = soup.select_one("pre")
    if pre is not None and pre.get_text().lstrip().startswith("{"):
        return pre.get_text()
    return None


def parse(html, query, sort="relevance"):
    text = _json_text(html)
    if text:
        try:
            return _from_json(json.loads(text), query, sort)
        except (ValueError, KeyError, TypeError):
            pass
    return _from_html(html, query, sort)


def _from_json(data, query, sort):
    posts = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if not d.get("title"):
            continue
        permalink = d.get("permalink")
        posts.append(models.RedditPost(
            query=query, position=len(posts) + 1, post_id=d.get("id"),
            subreddit=d.get("subreddit"), title=d["title"],
            author=d.get("author"), score=d.get("score"),
            num_comments=d.get("num_comments"),
            permalink="https://www.reddit.com" + permalink
            if permalink else None, url=d.get("url"),
            created_utc=int(d["created_utc"]) if d.get("created_utc")
            else None, sort=sort))
    return posts


def _int(value):
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _from_html(html, query, sort):
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for node in soup.select("shreddit-post"):
        title = node.get("post-title") or ""
        if not title:
            heading = node.select_one('[slot="title"], a[id^="post-title"]')
            title = heading.get_text(strip=True) if heading else ""
        if not title:
            continue
        permalink = node.get("permalink")
        sub = (node.get("subreddit-prefixed-name") or "").replace("r/", "")
        posts.append(models.RedditPost(
            query=query, position=len(posts) + 1, post_id=node.get("id"),
            subreddit=sub or None, title=title, author=node.get("author"),
            score=_int(node.get("score")),
            num_comments=_int(node.get("comment-count")),
            permalink="https://www.reddit.com" + permalink
            if permalink and permalink.startswith("/") else permalink,
            url=node.get("content-href"), created_utc=None, sort=sort))
    return posts


def fetch(session, query, subreddit=None, sort="relevance", time="all",
          limit=25):
    session.goto(build_url(query, subreddit, sort, time, limit),
                 check_google=False, blocked=is_blocked)
    posts = parse(session.html(), query, sort)
    if not posts:
        session.goto(build_url(query, subreddit, sort, time, limit,
                               as_json=False),
                     check_google=False, blocked=is_blocked)
        session.scroll(times=3, amount=15)
        posts = parse(session.html(), query, sort)
    return posts
