"""Local rank grid: rank of one business across an N x N grid of points."""
import html as html_mod
import os
import re
import time

from seo import geo, models
from seo.local.maps import build_maps_url, parse_feed, scroll_feed

_PUNCT = re.compile(r"[^a-z0-9 ]+")


def normalize_name(name):
    name = (name or "").lower().replace("'", "").replace("\u2019", "")
    return " ".join(_PUNCT.sub(" ", name).split())


def find_rank(businesses, target):
    """1-based position of `target` in a list of Business records, or None.
    Matches on normalized name containment so 'Joe's Plumbing LLC'
    matches 'Joes Plumbing'. If `target` looks like a CID it is matched
    exactly against Business.cid."""
    if target.isdigit():
        for biz in businesses:
            if biz.cid == target:
                return biz.position
        return None
    t = normalize_name(target)
    for biz in businesses:
        n = normalize_name(biz.name)
        if t and (t in n or n in t):
            return biz.position
    return None


def run_grid(session, keyword, target, center_lat, center_lng, size=5,
             spacing_km=1.0, zoom=14, center_label="", max_results=20,
             on_cell=None):
    """Visit every grid point in one browser session, re-pointing the
    emulated geolocation and the Maps viewport each time."""
    cells = []
    for row, col, lat, lng in geo.grid(center_lat, center_lng, size,
                                       spacing_km):
        session.set_geolocation(lat, lng)
        session.goto(build_maps_url(keyword, lat, lng, zoom))
        scroll_feed(session, max_results=max_results, max_rounds=6)
        listings = parse_feed(session.html(), keyword, center_label)
        rank = find_rank(listings, target)
        cell = models.GridCell(
            keyword=keyword, target=target, center=center_label,
            row=row, col=col, lat=lat, lng=lng, rank=rank,
            top_name=listings[0].name if listings else None)
        cells.append(cell)
        if on_cell:
            on_cell(cell)
        time.sleep(1.0)
    return cells


def summarize(cells):
    ranks = [c.rank for c in cells if c.rank is not None]
    total = len(cells)
    return {
        "cells": total,
        "found": len(ranks),
        "top3": sum(1 for r in ranks if r <= 3),
        "avg_rank": round(sum(ranks) / len(ranks), 2) if ranks else None,
        "coverage_pct": round(100.0 * len(ranks) / total, 1) if total else 0,
    }


def _color(rank):
    if rank is None:
        return "#9ca3af"            # grey: not found
    if rank <= 3:
        return "#15803d"            # green
    if rank <= 10:
        return "#ca8a04"            # amber
    return "#b91c1c"                # red


def heatmap_html(cells, path):
    """Write a self-contained HTML heatmap of the grid and return path."""
    if not cells:
        raise ValueError("no cells to render")
    size = max(c.row for c in cells) + 1
    by_pos = {(c.row, c.col): c for c in cells}
    stats = summarize(cells)
    first = cells[0]
    rows_html = []
    for r in range(size):
        tds = []
        for c in range(size):
            cell = by_pos.get((r, c))
            label = "-" if cell is None or cell.rank is None else cell.rank
            title = "" if cell is None else "%s, %s" % (cell.lat, cell.lng)
            tds.append(
                '<td style="background:%s" title="%s">%s</td>'
                % (_color(None if cell is None else cell.rank),
                   html_mod.escape(title), label)
            )
        rows_html.append("<tr>%s</tr>" % "".join(tds))
    doc = """<!doctype html><html><head><meta charset="utf-8">
<title>Rank grid</title>
<style>
body{font-family:system-ui,sans-serif;margin:24px;color:#111}
table{border-collapse:separate;border-spacing:4px}
td{width:56px;height:56px;text-align:center;color:#fff;font-weight:600;
border-radius:6px;font-size:18px}
.meta{color:#444;margin-bottom:12px}
</style></head><body>
<h1>%(kw)s</h1>
<div class="meta">Target: <b>%(target)s</b> &middot; Centre: %(center)s
&middot; %(found)s/%(cells)s cells found &middot; top-3 in %(top3)s
&middot; avg rank %(avg)s &middot; %(when)s</div>
<table>%(rows)s</table>
<p class="meta">Green = top 3, amber = 4-10, red = 11+, grey = not in
the results feed.</p>
</body></html>""" % dict(
        kw=html_mod.escape(first.keyword),
        target=html_mod.escape(first.target),
        center=html_mod.escape(first.center or ""), found=stats["found"],
        cells=stats["cells"], top3=stats["top3"], avg=stats["avg_rank"],
        when=html_mod.escape(first.fetched_at), rows="".join(rows_html))
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path
