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
    assert maps.build_maps_url("x") == (
        "https://www.google.com/maps/search/x?hl=en")


def test_find_rank_by_name_and_cid(fixture):
    biz = maps.parse_feed(fixture("google_maps_feed.html"), "plumber")
    assert grid.find_rank(biz, "joes plumbing") == 1
    assert grid.find_rank(biz, "Austin Drain Pros, Inc.") == 2
    assert grid.find_rank(biz, "Nobody Here") is None
    assert grid.find_rank(biz, str(int("0xabcdef0123456789", 16))) == 2
