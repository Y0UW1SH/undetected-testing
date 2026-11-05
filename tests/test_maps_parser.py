from seo.local import grid, maps


def test_feed_parse_dedupes_and_extracts_fields(fixture):
    biz = maps.parse_feed(fixture("google_maps_feed.html"), "plumber",
                          "Austin, TX")
    assert [b.name for b in biz] == ["Joe's Plumbing LLC",
                                     "Austin Drain Pros", "No Rating Pipes"]
    joe = biz[0]
    assert (joe.rating, joe.reviews) == (4.8, 312)
    assert joe.category == "Plumber"
    assert joe.address == "123 Main St"
    assert joe.status == "Open 24 hours" and joe.phone is None
    assert (joe.lat, joe.lng) == (30.2672, -97.7431)
    assert joe.cid == str(int("0x1f2e3d4c5b6a7980", 16))
    assert biz[2].rating is None and biz[2].reviews is None
    assert [b.position for b in biz] == [1, 2, 3]


def test_place_parse(fixture):
    info = maps.parse_place(fixture("google_maps_place.html"))
    assert info["name"] == "Joe's Plumbing LLC"
    assert info["rating"] == 4.8
    assert info["reviews"] == 312
    assert info["category"] == "Plumber"
    assert info["address"] == "123 Main St, Austin, TX 78701"
    assert info["phone"] == "+15125550100"
    assert info["website"] == "https://www.joesplumbing.example/"
    assert info["hours"] == "Monday: 8 AM–6 PM; Tuesday: 8 AM–6 PM"


def test_build_maps_url():
    assert maps.build_maps_url("plumber austin", 30.1, -97.2) == (
        "https://www.google.com/maps/search/plumber%20austin/"
        "@30.1,-97.2,14z?hl=en")
    # a slash in the query must not split the path
    assert maps.build_maps_url("24/7 plumber", 30.1, -97.2) == (
        "https://www.google.com/maps/search/24%2F7%20plumber/"
        "@30.1,-97.2,14z?hl=en")


def test_phone_area_code_is_not_a_review_count():
    html = """<div role="feed"><div><div>
      <a href="https://www.google.com/maps/place/X/data=!1s0x1:0x2a"
         aria-label="No Rating Pipes"></a>
      <div><div>No Rating Pipes</div><div>No reviews</div>
      <div>Plumber · 1 Side St</div><div>Open 24 hours</div>
      <div>(512) 555-0100</div></div></div></div></div>"""
    biz = maps.parse_feed(html, "q")
    assert len(biz) == 1
    assert biz[0].reviews is None and biz[0].rating is None
    assert biz[0].phone == "(512) 555-0100"
    assert biz[0].category == "Plumber"
    assert biz[0].address == "1 Side St"
    assert maps.build_maps_url("x") == (
        "https://www.google.com/maps/search/x?hl=en")


def test_live_feed_fixture(fixture):
    """Captured from a real Maps results page (scripts stripped)."""
    biz = maps.parse_feed(fixture("live_maps_feed.html"),
                          "plumber austin tx", "Austin, TX")
    assert len(biz) == 10
    first = biz[0]
    assert first.name == "LoneSprings Plumbing"
    assert first.is_ad is True
    assert (first.rating, first.reviews) == (4.9, 49)
    assert first.category == "Plumber"
    assert first.address == "1 Pine Point Drive"
    assert first.status == "Open 24 hours"
    assert first.phone == "(512) 798-3755"
    assert first.cid == "306376917645060018"
    assert (first.lat, first.lng) == (30.105061, -97.288002)
    second = biz[1]
    assert second.name == "Austin Plumbery" and second.is_ad is False
    assert second.website and "austinplumbery" in second.website
    # service-area business: category but no address; status fragments
    # are joined; the phone still comes through
    assert second.category == "Plumber" and second.address is None
    assert second.status == "Closes soon · 6 PM · Opens 7 AM Thu"
    assert second.phone == "+1 512-766-1519"
    by_name = {b.name: b for b in biz}
    abacus = by_name["Abacus Plumbing, Air Conditioning & Electrical"]
    assert abacus.address == "708 S Lamar Blvd G"   # icon glyph skipped
    assert abacus.status == "Open 24 hours"
    we_plumb = by_name["We Plumb LLC"]
    assert (we_plumb.category, we_plumb.address) == ("Plumber", None)
    assert we_plumb.status == "Open 24 hours"
    assert we_plumb.phone == "+1 737-297-5863"
    austin_pl = by_name["Austin Plumbing"]
    assert austin_pl.address == "5115 N Lamar Blvd Ste 100"
    assert austin_pl.status == "Open · Closes 11 PM"
    assert all(b.category == "Plumber" for b in biz)
    assert all(b.phone for b in biz)
    assert all(b.cid for b in biz)
    assert sum(1 for b in biz if b.is_ad) == 1
    # ads are excluded from the organic rank
    assert grid.find_rank(biz, "Austin Plumbery") == 1
    assert grid.find_rank(biz, "Austin Plumbery", include_ads=True) == 2
    assert grid.find_rank(biz, "LoneSprings Plumbing") is None
    assert grid.find_rank(biz, "LoneSprings Plumbing",
                          include_ads=True) == 1


def test_live_place_fixture(fixture):
    info = maps.parse_place(fixture("live_maps_place.html"))
    assert info["name"] == "Austin Plumbery"
    assert info["rating"] == 4.8 and info["reviews"] == 228
    assert info["category"] == "Plumber"
    assert info["phone"] == "+15127661519"
    assert info["website"] == "http://www.austinplumbery.com/"
    assert info["address"] is None          # service-area business
    hours = info["hours"]
    assert hours.count(";") == 6            # exactly seven weekdays
    assert "Wednesday: 7 AM–6 PM" in hours
    assert "stars" not in hours             # rating-histogram rows skipped


def test_find_rank_by_name_and_cid(fixture):
    biz = maps.parse_feed(fixture("google_maps_feed.html"), "plumber")
    assert grid.find_rank(biz, "joes plumbing") == 1
    assert grid.find_rank(biz, "Austin Drain Pros, Inc.") == 2
    assert grid.find_rank(biz, "Nobody Here") is None
    assert grid.find_rank(biz, str(int("0xabcdef0123456789", 16))) == 2
