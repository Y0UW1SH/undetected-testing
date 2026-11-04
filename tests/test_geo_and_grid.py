import base64
import os

import pytest

from seo import geo, models
from seo.local import grid


def test_uule_known_value():
    # Reference value seen in real Google URLs for "California".
    assert geo.uule("California") == "w+CAIQICIKQ2FsaWZvcm5pYQ"


def test_uule_length_prefix_and_encoding():
    name = "Austin,Texas,United States"           # 26 bytes -> 'a'
    value = geo.uule(name)
    assert value.startswith("w+CAIQICIa")
    expected_tail = base64.b64encode(name.encode()).decode().rstrip("=")
    assert value.endswith(expected_tail)
    assert "=" not in value
    with pytest.raises(ValueError):
        geo.uule("")
    with pytest.raises(ValueError):
        geo.uule("x" * 130)


def test_canonical_name():
    assert geo.canonical_name("Austin", "Texas") == (
        "Austin,Texas,United States")
    assert geo.canonical_name("Paris", country="France") == "Paris,France"


def test_grid_geometry():
    cells = geo.grid(30.0, -97.0, size=3, spacing_km=1.0)
    assert len(cells) == 9
    rows = [c[0] for c in cells]
    assert rows == [0, 0, 0, 1, 1, 1, 2, 2, 2]
    centre = cells[4]
    assert (centre[2], centre[3]) == (30.0, -97.0)
    north = cells[1]
    assert north[2] > 30.0 and abs(north[3] + 97.0) < 1e-6
    # ~1 km north is ~0.009 degrees of latitude
    assert abs((north[2] - 30.0) - 0.009043) < 1e-4
    with pytest.raises(ValueError):
        geo.grid(0, 0, size=4)


def test_geocode_with_injected_opener():
    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        assert "nominatim" in req.full_url
        assert req.get_header("User-agent")
        return Resp(b'[{"lat": "30.27", "lon": "-97.74", '
                    b'"display_name": "Austin, Texas"}]')

    assert geo.geocode("Austin, TX", opener=opener) == (
        30.27, -97.74, "Austin, Texas")


def test_grid_summary_and_heatmap(tmp_path):
    cells = []
    ranks = [1, 2, 5, None, 12, 3, None, 1, 20]
    for i, rank in enumerate(ranks):
        cells.append(models.GridCell(
            keyword="plumber", target="Joe's", center="Austin",
            row=i // 3, col=i % 3, lat=30.0, lng=-97.0, rank=rank))
    summary = grid.summarize(cells)
    assert summary == {"cells": 9, "found": 7, "top3": 4,
                       "avg_rank": 6.29, "coverage_pct": 77.8}
    path = grid.heatmap_html(cells, os.path.join(tmp_path, "g.html"))
    html = open(path, encoding="utf-8").read()
    assert "<title>Rank grid</title>" in html
    assert html.count("<tr>") == 3
    assert "#9ca3af" in html and "#15803d" in html


def test_normalize_name():
    assert grid.normalize_name("Joe's Plumbing, LLC!") == "joes plumbing llc"
