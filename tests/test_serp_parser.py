from seo.serp import google


def test_build_url_includes_uule_and_pagination():
    url = google.build_url("plumber", "Austin,Texas,United States",
                           page=2)
    assert url.startswith("https://www.google.com/search?")
    assert "q=plumber" in url and "start=10" in url and "pws=0" in url
    assert "&uule=w+CAIQICI" in url


def test_organic_results_skip_ads_paa_local_and_google_links(fixture):
    parsed = google.parse_serp(fixture("google_serp_desktop.html"),
                               "emergency plumber austin", "Austin, TX")
    organic = parsed["organic"]
    domains = [r.domain for r in organic]
    assert domains == ["joesplumbing.example", "yelp.com",
                       "austindrainpros.example"]
    assert [r.position for r in organic] == [1, 2, 3]
    assert organic[0].snippet.startswith("Fast 24-hour")
    # /url?q= wrapper is unwrapped and decoded
    assert organic[1].url.startswith("https://www.yelp.com/search?")
    assert organic[2].snippet.startswith("Same-day")
    assert all(r.result_type == "organic" for r in organic)
    assert organic[0].query == "emergency plumber austin"
    assert organic[0].location == "Austin, TX"


def test_ads(fixture):
    parsed = google.parse_serp(fixture("google_serp_desktop.html"), "q")
    ads = parsed["ads"]
    assert [a.domain for a in ads] == ["adplumbers.example",
                                       "bottomad.example"]
    assert ads[0].title == "Ad Plumbers | 24/7 Service"
    assert ads[0].result_type == "ad"


def test_people_also_ask(fixture):
    parsed = google.parse_serp(fixture("google_serp_desktop.html"), "q")
    assert [p.title for p in parsed["paa"]] == [
        "How much does an emergency plumber cost in Austin?",
        "Is a leaking pipe an emergency?",
    ]


def test_featured_snippet(fixture):
    parsed = google.parse_serp(fixture("google_serp_desktop.html"), "q")
    fs = parsed["featured"]
    assert len(fs) == 1
    assert fs[0].domain == "homeadvisor.example"
    assert "shut off" in fs[0].snippet


def test_local_pack(fixture):
    parsed = google.parse_serp(fixture("google_serp_desktop.html"), "q")
    pack = parsed["local_pack"]
    assert [e.name for e in pack] == ["Joe's Plumbing LLC",
                                      "Austin Drain Pros",
                                      "Sponsored Pipe Co"]
    assert (pack[0].rating, pack[0].reviews) == (4.8, 312)
    assert pack[0].category == "Plumber"
    assert pack[0].address == "123 Main St"
    assert pack[1].is_ad is False
    assert pack[2].is_ad is True


def test_flatten_counts(fixture):
    parsed = google.parse_serp(fixture("google_serp_desktop.html"), "q")
    assert len(google.flatten(parsed)) == 3 + 2 + 2 + 1 + 3


def test_result_without_snippet_does_not_borrow_a_neighbours(fixture):
    html = fixture("google_serp_desktop.html").replace(
        '<div class="g">\n    <a href="https://www.joesplumbing.example/'
        'emergency"><h3>Duplicate link to Joe\'s</h3></a>\n  </div>',
        '<div class="g"><a href="https://bare.example/"><h3>Bare result'
        '</h3></a></div>')
    parsed = google.parse_serp(html, "q")
    bare = [r for r in parsed["organic"] if r.domain == "bare.example"]
    assert len(bare) == 1
    assert bare[0].snippet in (None, "")
    first = parsed["organic"][0]
    assert first.snippet.startswith("Fast 24-hour")


def test_local_pack_phone_is_not_a_review_count():
    html = """<div id="rso"><div data-cid="1">
      <div role="heading">Joe's</div>
      <div>Plumber · 1 Main St</div>
      <div>(512) 555-0100</div></div></div>"""
    pack = google.parse_serp(html, "q")["local_pack"]
    assert pack[0].reviews is None
    assert pack[0].category == "Plumber"


def test_live_serp_fixture(fixture):
    """Captured from a real Google results page via GitHub Actions. Links
    are opaque /goto?url= redirects; the domain comes from <cite>."""
    parsed = google.parse_serp(fixture("live_google_serp.html"),
                               "emergency plumber austin", "Austin, TX")
    organic = parsed["organic"]
    assert len(organic) >= 8
    assert [r.position for r in organic] == list(range(1, len(organic) + 1))
    first = organic[0]
    assert first.title.startswith("Austin Plumber")
    assert first.domain == "rotorooter.com"
    assert first.url == "https://www.rotorooter.com"
    assert first.source == "Roto-Rooter"
    assert "emergency plumbing" in first.snippet
    assert organic[1].domain == "abacusplumbing.com"
    reddit = [r for r in organic if "r/Austin" in (r.title or "")][0]
    assert reddit.domain is None                 # cite had no URL
    assert reddit.source == "Reddit"
    assert reddit.url.startswith("https://www.google.com/goto?url=")
    assert all((r.domain or "").find("google") == -1 for r in organic)
    assert len({r.title for r in organic}) == len(organic)   # no dupes
    paa = parsed["paa"]
    assert len(paa) == 4
    assert paa[0].title == "How much should I pay for an emergency plumber?"
    pack = parsed["local_pack"]
    assert [e.name for e in pack][:2] == ["Austin's Greatest Plumbing",
                                          "Reliant Plumbing - Austin"]
    assert len(pack) == 3
    assert (pack[0].rating, pack[0].reviews) == (5.0, 346)
    assert pack[0].category == "Plumber"
    assert pack[0].phone == "(512) 377-9919"
    assert pack[1].reviews == 1700                # "1.7K user reviews"
    assert pack[2].reviews == 2900
    assert all(e.is_ad is False for e in pack)
    assert parsed["ads"] == [] and parsed["featured"] == []


def test_parse_count():
    from seo.textutil import parse_count
    assert parse_count("346") == 346
    assert parse_count("1,234") == 1234
    assert parse_count("1.7K") == 1700
    assert parse_count("2M") == 2000000
    assert parse_count("abc") is None


def test_result_link_variants():
    assert google._result_link("/goto?url=abc") == (
        "https://www.google.com/goto?url=abc", True)
    assert google._result_link("/url?q=https://x.example/p&sa=U") == (
        "https://x.example/p", False)
    assert google._result_link("https://x.example/") == (
        "https://x.example/", False)
    assert google._result_link("https://www.google.com/maps") == (
        None, False)
    assert google._result_link("#") == (None, False)


def test_empty_page_parses_to_nothing():
    parsed = google.parse_serp("<html><body></body></html>", "q")
    assert all(not v for v in parsed.values())
