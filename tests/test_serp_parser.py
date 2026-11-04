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


def test_empty_page_parses_to_nothing():
    parsed = google.parse_serp("<html><body></body></html>", "q")
    assert all(not v for v in parsed.values())
