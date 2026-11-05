"""Self-contained HTML report of everything in the database."""
import html
import os
from collections import OrderedDict

CSS = """
body{font-family:system-ui,sans-serif;margin:24px;color:#111;max-width:1100px}
h1{margin-bottom:0}h2{margin-top:36px;border-bottom:1px solid #ddd}
h3{margin:18px 0 6px;color:#333}table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid #eee;
font-size:14px;vertical-align:top}th{background:#f6f6f6}
.muted{color:#666;font-size:13px}.ad{color:#b45309}.pill{display:inline-block;
padding:1px 6px;border-radius:4px;background:#eef;font-size:12px}
"""


def _esc(v):
    return html.escape("" if v is None else str(v))


def _latest_batches(rows, key_fields):
    """Group rows by key, keep only each key's most recent fetched_at."""
    groups = OrderedDict()
    for r in rows:
        key = tuple(r[k] for k in key_fields)
        groups.setdefault(key, []).append(r)
    out = OrderedDict()
    for key, items in groups.items():
        stamp = max(i["fetched_at"] for i in items)
        out[key] = [i for i in items if i["fetched_at"] == stamp]
    return out


def _table(headers, rows):
    head = "".join("<th>%s</th>" % _esc(h) for h in headers)
    body = "".join(
        "<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in row)
        for row in rows)
    return "<table><tr>%s</tr>%s</table>" % (head, body)


def _serp_section(store):
    rows = store.rows("serp_results")
    pack = store.rows("local_pack")
    if not rows and not pack:
        return ""
    parts = ["<h2>Google web search</h2>"]
    for (q, loc, dev), items in _latest_batches(
            rows, ("query", "location", "device")).items():
        parts.append("<h3>%s <span class='pill'>%s</span> "
                     "<span class='pill'>%s</span></h3>" % (
                         _esc(q), _esc(loc or "no location"), _esc(dev)))
        parts.append("<div class='muted'>fetched %s</div>" % _esc(
            items[0]["fetched_at"]))
        order = {"ad": 0, "featured_snippet": 1, "organic": 2, "paa": 3}
        items.sort(key=lambda r: (order.get(r["result_type"], 9),
                                  r["position"]))
        body = []
        for r in items:
            kind = r["result_type"]
            label = {"ad": "Ad", "featured_snippet": "Featured",
                     "organic": str(r["position"]), "paa": "PAA"}.get(kind,
                                                                      kind)
            link = ("<a href='%s'>%s</a>" % (_esc(r["url"]), _esc(r["domain"]))
                    if r["url"] else "")
            body.append([
                "<span class='%s'>%s</span>" % ("ad" if kind == "ad" else "",
                                                label),
                link, _esc(r["title"]),
                "<span class='muted'>%s</span>" % _esc(
                    (r["snippet"] or "")[:160])])
        parts.append(_table(["#", "Domain", "Title", "Snippet"], body))
        pack_items = [p for p in pack if (p["query"], p["location"],
                                          p["device"]) == (q, loc, dev)]
        if pack_items:
            stamp = max(p["fetched_at"] for p in pack_items)
            pack_items = [p for p in pack_items if p["fetched_at"] == stamp]
            parts.append("<h3 class='muted'>Local pack</h3>")
            parts.append(_table(
                ["#", "Business", "Rating", "Reviews", "Category",
                 "Address"],
                [[_esc(p["position"]) + (" (ad)" if p["is_ad"] else ""),
                  _esc(p["name"]), _esc(p["rating"]), _esc(p["reviews"]),
                  _esc(p["category"]), _esc(p["address"])]
                 for p in sorted(pack_items, key=lambda p: p["position"])]))
    return "\n".join(parts)


def _local_section(store):
    rows = store.rows("businesses")
    if not rows:
        return ""
    parts = ["<h2>Google Maps listings</h2>"]
    for (q, loc), items in _latest_batches(rows, ("query",
                                                  "location")).items():
        parts.append("<h3>%s <span class='pill'>%s</span></h3>" % (
            _esc(q), _esc(loc or "no location")))
        parts.append("<div class='muted'>fetched %s</div>" % _esc(
            items[0]["fetched_at"]))
        items.sort(key=lambda r: r["position"])
        parts.append(_table(
            ["#", "Business", "Rating", "Reviews", "Category", "Phone",
             "Website", "Address"],
            [[_esc(r["position"]) + (" (ad)" if r["is_ad"] else ""),
              "<a href='%s'>%s</a>" % (_esc(r["maps_url"]), _esc(r["name"]))
              if r["maps_url"] else _esc(r["name"]),
              _esc(r["rating"]), _esc(r["reviews"]), _esc(r["category"]),
              _esc(r["phone"]),
              "<a href='%s'>site</a>" % _esc(r["website"])
              if r["website"] else "",
              _esc(r["address"])] for r in items]))
    return "\n".join(parts)


def _grid_section(store, report_dir):
    rows = store.rows("grid_cells")
    if not rows:
        return ""
    from seo.local.grid import normalize_name
    parts = ["<h2>Local rank grids</h2>"]
    for (kw, target, center), cells in _latest_batches(
            rows, ("keyword", "target", "center")).items():
        ranks = [c["rank"] for c in cells if c["rank"] is not None]
        size = max(c["row"] for c in cells) + 1
        heat = "grid_%s.html" % normalize_name(target).replace(" ", "_")
        link = ("<a href='%s'>heatmap</a>" % _esc(heat)
                if os.path.exists(os.path.join(report_dir, heat)) else "")
        parts.append("<h3>%s for <b>%s</b> <span class='pill'>%s</span> "
                     "%s</h3>" % (_esc(kw), _esc(target), _esc(center), link))
        parts.append(
            "<div class='muted'>%dx%d grid, found in %d/%d cells, top-3 in "
            "%d, average rank %s, fetched %s</div>" % (
                size, size, len(ranks), len(cells),
                sum(1 for r in ranks if r <= 3),
                ("%.1f" % (sum(ranks) / len(ranks))) if ranks else "-",
                _esc(cells[0]["fetched_at"])))
    return "\n".join(parts)


def _nap_section(store):
    rows = store.rows("nap_records")
    if not rows:
        return ""
    parts = ["<h2>NAP consistency</h2>"]
    for (biz,), items in _latest_batches(rows, ("business",)).items():
        parts.append("<h3>%s</h3>" % _esc(biz))
        parts.append(_table(
            ["Directory", "Phone matches", "Address matches", "Page"],
            [[_esc(r["source"]), _esc(_yesno(r["phone_match"])),
              _esc(_yesno(r["address_match"])),
              "<a href='%s'>%s</a>" % (_esc(r["url"]), _esc(r["url"][:60]))]
             for r in items]))
    return "\n".join(parts)


def _yesno(v):
    return {None: "unknown", 1: "yes", 0: "no", True: "yes",
            False: "no"}.get(v, v)


def _canary_section(store):
    rows = store.rows("canary_runs")
    if not rows:
        return ""
    rows = rows[-20:]
    attempted = [r for r in rows if r["outcome"] != "skipped"]
    challenged = sum(1 for r in attempted if r["outcome"] == "challenge")
    rate = (100.0 * challenged / len(attempted)) if attempted else 0
    return ("<h2>Canary</h2><div class='muted'>last %d queries: %d "
            "challenged (%.0f%%), last run %s</div>" % (
                len(rows), challenged, rate, _esc(rows[-1]["fetched_at"])))


def render(store, report_dir="data"):
    sections = [_serp_section(store), _local_section(store),
                _grid_section(store, report_dir), _nap_section(store),
                _canary_section(store)]
    body = "\n".join(s for s in sections if s) or (
        "<p class='muted'>No data yet. Run <code>seo serp</code>, "
        "<code>seo local</code> or <code>seo batch</code> first.</p>")
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<title>SEO report</title><style>%s</style></head><body>"
            "<h1>SEO report</h1><div class='muted'>%s</div>%s"
            "</body></html>" % (CSS, _esc(os.path.abspath(store.path)),
                                body))


def write(store, path):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render(store, directory))
    return path
