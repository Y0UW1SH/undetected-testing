"""Reddit search source and the `seo reddit` command."""
import os

from seo import storage
from seo.sources import reddit
from tests.fakes import RecordingSession, run, write_config


def test_build_url():
    url = reddit.build_url("plumber austin", sort="new", time="week")
    assert url.startswith("https://www.reddit.com/search.json?")
    assert "q=plumber+austin" in url and "sort=new" in url and "t=week" in url
    assert "raw_json=1" in url
    sub = reddit.build_url("leak", subreddit="r/Austin/", as_json=False)
    assert sub.startswith("https://www.reddit.com/r/Austin/search/?")
    assert "restrict_sr=1" in sub and "raw_json" not in sub


def test_is_blocked():
    assert reddit.is_blocked("u", "Too Many Requests", "") is not None
    assert reddit.is_blocked("u", "", '{"message": "Too Many Requests", '
                             '"error": 429}') is not None
    assert reddit.is_blocked("u", "reddit", "<html>ok</html>") is None


def test_parse_json_listing(fixture):
    posts = reddit.parse(fixture("reddit_search.json"), "plumber", "new")
    assert len(posts) == 2                      # untitled entry skipped
    first = posts[0]
    assert first.post_id == "abc123" and first.subreddit == "Austin"
    assert first.title == "Best emergency plumber in Austin?"
    assert (first.score, first.num_comments) == (57, 23)
    assert first.permalink == ("https://www.reddit.com/r/Austin/comments/"
                               "abc123/best_emergency_plumber_in_austin/")
    assert first.created_utc == 1761955200 and first.sort == "new"
    assert posts[1].url == "https://imgur.com/x"


def test_parse_json_inside_pre(fixture):
    html = "<html><body><pre>%s</pre></body></html>" % fixture(
        "reddit_search.json")
    assert len(reddit.parse(html, "q")) == 2


def test_parse_html_fallback(fixture):
    posts = reddit.parse(fixture("reddit_search.html"), "plumber")
    assert [p.title for p in posts] == [
        "Best emergency plumber in Austin?",
        "Clarke Kent Plumbing saved my weekend"]
    assert posts[0].subreddit == "Austin" and posts[0].score == 57
    assert posts[0].permalink.startswith("https://www.reddit.com/r/Austin/")
    assert posts[1].score == 1204                # "1,204"
    assert posts[1].permalink.startswith("https://www.reddit.com/r/austin")


def test_reddit_command_and_report(fake_env, capsys):
    assert run(fake_env, "reddit", "emergency plumber austin",
               "--sort", "new") == 0
    s = RecordingSession.created[0]
    assert len(s.fake.visited) == 1 and ".json" in s.fake.visited[0]
    out = capsys.readouterr().out
    assert "r/Austin" in out and "Saved 2 posts." in out
    assert run(fake_env, "reddit", "emergency plumber austin",
               "--sort", "new") == 0
    assert len(RecordingSession.created) == 1      # fetched today
    store = storage.Storage(fake_env["db"])
    rows = store.rows("reddit_posts")
    assert len(rows) == 2 and rows[0]["sort"] == "new"
    store.close()
    assert run(fake_env, "report", "--out", "data/report.html") == 0
    html = open("data/report.html", encoding="utf-8").read()
    assert "<h2>Reddit</h2>" in html and "r/Austin" in html


def test_batch_runs_reddit_jobs(fake_env):
    write_config({"reddit": ["plumber", {"query": "leak",
                                         "subreddit": "Austin",
                                         "sort": "top"}]})
    assert run(fake_env, "batch", "--config", "cfg.json") == 0
    urls = RecordingSession.created[0].fake.visited
    assert len(urls) == 2
    assert "/r/Austin/search.json" in urls[1] and "sort=top" in urls[1]
    store = storage.Storage(fake_env["db"])
    assert len(store.rows("reddit_posts")) == 4
    store.close()


def test_init_accepts_reddit(fake_env):
    assert run(fake_env, "init", "--config", "cfg.json",
               "--location", "Austin, TX", "--reddit", "plumber") == 0
    assert os.path.exists("cfg.json")
    assert '"reddit"' in open("cfg.json").read()
