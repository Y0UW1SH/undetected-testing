"""Product research: Amazon and eBay search results with price alerts.

Installed as `research` (or `python -m seo.research_cli`). Separate from
the `seo` command on purpose: different sites, config, database, schedule
and output.

First run:
    research init --amazon "wireless mouse" --ebay "wireless mouse"
    research track            # fetch everything in the config, show alerts
    research report           # data/research_report.html

Single commands:
    research amazon "wireless mouse" --pages 2
    research ebay "wireless mouse"
    research alerts --threshold 5
    research export --format csv
"""
import argparse
import html
import json
import os
import shutil
import sys

import seo
from seo import cli as seo_cli
from seo import storage
from seo.models import ChallengeError, MissingDependency, utcnow
from seo.sources import amazon, ebay

DEFAULT_DB = os.path.join("data", "research.sqlite")
DEFAULT_CONFIG = os.path.join("seo", "config", "research.json")
EXAMPLE_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "config", "research.example.json")
ALERTS_PATH = os.path.join("data", "research_alerts.json")
SOURCES = ("amazon", "ebay")


# ---- fetching --------------------------------------------------------------
def fetch_source(session, source, query, pages=1, domain="amazon.com",
                 site="www.ebay.com"):
    if source == "amazon":
        return amazon.fetch(session, query, pages=pages, domain=domain)
    if source == "ebay":
        return ebay.fetch(session, query, pages=pages, site=site)
    raise ValueError("unknown source %r" % source)


def _print_items(items):
    for it in items:
        flag = " [sponsored]" if it.is_sponsored else ""
        price = it.price_text or "-"
        rating = "%.1f" % it.rating if it.rating is not None else "-"
        reviews = str(it.reviews) if it.reviews is not None else "-"
        print("%3d. %-14s %-5s %-8s %s%s" % (
            it.position, price[:14], rating, reviews, it.title[:70], flag))


def _run_query(args, store, source, query, pages, label=True):
    if not args.force and store.fetched_today("listings", source=source,
                                              query=query):
        print("%s %r already fetched today; use --force to refetch."
              % (source, query))
        return None
    with seo_cli._session(args) as s:
        items = fetch_source(s, source, query, pages,
                             domain=getattr(args, "domain", "amazon.com"),
                             site=getattr(args, "site", "www.ebay.com"))
    store.save(items, stamp=utcnow())
    if label:
        print('*** %s results for "%s" (%d items):' % (
            source.capitalize(), query, len(items)))
    _print_items(items)
    return items


def cmd_amazon(args, store):
    _run_query(args, store, "amazon", args.query, args.pages)
    return 0


def cmd_ebay(args, store):
    _run_query(args, store, "ebay", args.query, args.pages)
    return 0


# ---- alerts ---------------------------------------------------------------
def price_alerts(latest, previous, threshold=10.0):
    """Compare two runs of the same (source, query).

    `latest` and `previous` are lists of listing rows (dicts or objects
    with item_id, title, price, position, source, query). Returns alert
    dicts: kind is price_up / price_down (abs change > threshold %),
    new (in latest only) or gone (in previous only)."""
    def get(row, key):
        return row[key] if isinstance(row, dict) else getattr(row, key)

    def key_of(row):
        return get(row, "item_id") or get(row, "title")

    alerts = []
    if not latest and not previous:
        return alerts
    old = {key_of(r): r for r in previous}
    new = {key_of(r): r for r in latest}
    sample = latest[0] if latest else previous[0]
    base = {"source": get(sample, "source"), "query": get(sample, "query")}
    for key, row in new.items():
        if key not in old:
            if previous:
                alerts.append(dict(
                    base, kind="new", item_id=get(row, "item_id"),
                    title=get(row, "title"), position=get(row, "position"),
                    new_price=get(row, "price"), old_price=None,
                    change_pct=None))
            continue
        old_price = get(old[key], "price")
        new_price = get(row, "price")
        if old_price in (None, 0) or new_price is None:
            continue
        change = 100.0 * (new_price - old_price) / old_price
        if abs(change) > threshold:
            alerts.append(dict(
                base, kind="price_up" if change > 0 else "price_down",
                item_id=get(row, "item_id"), title=get(row, "title"),
                position=get(row, "position"), old_price=old_price,
                new_price=new_price, change_pct=round(change, 1)))
    for key, row in old.items():
        if key not in new and latest:
            alerts.append(dict(
                base, kind="gone", item_id=get(row, "item_id"),
                title=get(row, "title"), position=get(row, "position"),
                old_price=get(row, "price"), new_price=None,
                change_pct=None))
    order = {"price_down": 0, "price_up": 1, "gone": 2, "new": 3}
    alerts.sort(key=lambda a: (order[a["kind"]],
                               -(abs(a["change_pct"] or 0))))
    return alerts


def tracked_pairs(store):
    rows = store.conn.execute(
        "SELECT DISTINCT source, query FROM listings ORDER BY source, query"
    ).fetchall()
    return [(r["source"], r["query"]) for r in rows]


def all_alerts(store, threshold=10.0, pairs=None):
    alerts = []
    for source, query in pairs or tracked_pairs(store):
        latest, previous = store.latest_runs(
            "listings", ("item_id",), source=source, query=query)
        alerts.extend(price_alerts(latest, previous, threshold))
    return alerts


def format_alert(a):
    if a["kind"] in ("price_up", "price_down"):
        arrow = "up" if a["kind"] == "price_up" else "down"
        return "%s %r: %s %s %.2f -> %.2f (%+.1f%%) %s" % (
            a["source"], a["query"], arrow, a["item_id"] or "",
            a["old_price"], a["new_price"], a["change_pct"],
            (a["title"] or "")[:60])
    if a["kind"] == "new":
        return "%s %r: new #%s %s %s" % (
            a["source"], a["query"], a["position"], a["item_id"] or "",
            (a["title"] or "")[:60])
    return "%s %r: gone %s %s" % (
        a["source"], a["query"], a["item_id"] or "", (a["title"] or "")[:60])


def write_alerts(alerts, path=ALERTS_PATH):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": utcnow(), "alerts": alerts}, fh,
                  indent=2)
    return path


def print_alerts(alerts):
    if not alerts:
        print("No price alerts (nothing changed beyond the threshold, or "
              "no previous run to compare with).")
        return
    print("*** %d alert(s):" % len(alerts))
    for a in alerts:
        line = format_alert(a)
        print("  " + line)
        if os.environ.get("GITHUB_ACTIONS"):
            print("::warning::" + line)


def cmd_alerts(args, store):
    alerts = all_alerts(store, args.threshold)
    print_alerts(alerts)
    print("Written to %s" % write_alerts(alerts, args.out))
    return 0


# ---- track ----------------------------------------------------------------
def load_config(path):
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)
    jobs = []
    for source in SOURCES:
        for q in config.get(source, []):
            jobs.append((source, q))
    return config, jobs


def cmd_track(args, store):
    config, jobs = load_config(args.config)
    pages = int(config.get("pages", 1))
    threshold = args.threshold if args.threshold is not None else float(
        config.get("alert_threshold_pct", 10))
    counts = {"done": 0, "skipped": 0, "errors": 0, "challenged_jobs": 0,
              "challenged_sessions": 0}
    touched = []
    stop = False
    for source in SOURCES:
        if stop:
            break
        pending = [q for s, q in jobs if s == source and (
            args.force or not store.fetched_today("listings", source=source,
                                                  query=q))]
        counts["skipped"] += len([q for s, q in jobs if s == source]) - len(
            pending)
        queue = list(pending)
        while queue and not stop:
            batch, queue = queue[:args.per_session], queue[args.per_session:]
            try:
                with seo_cli._session(args) as s:
                    for idx, query in enumerate(batch):
                        print(">>> %s %r" % (source, query))
                        try:
                            items = fetch_source(s, source, query, pages)
                            store.save(items, stamp=utcnow())
                            _print_items(items)
                            touched.append((source, query))
                            counts["done"] += 1
                        except ChallengeError:
                            counts["challenged_jobs"] += 1
                            queue = batch[idx + 1:] + queue
                            raise
                        except Exception as e:
                            counts["errors"] += 1
                            print("ERROR in %s %r: %s" % (source, query, e),
                                  file=sys.stderr)
                        s.pause()
            except ChallengeError as e:
                counts["challenged_sessions"] += 1
                print("CHALLENGED, abandoning this session: %s" % e,
                      file=sys.stderr)
                if args.on_challenge == "stop":
                    stop = True
    alerts = all_alerts(store, threshold, pairs=sorted(set(touched)))
    print_alerts(alerts)
    write_alerts(alerts)
    seo_cli._print_json(counts)
    if counts["done"]:
        print("Next: `research report` writes data/research_report.html")
        return 0
    if counts["challenged_sessions"]:
        return 3
    return 1 if counts["errors"] else 0


# ---- report ---------------------------------------------------------------
CSS = """
body{font-family:system-ui,sans-serif;margin:24px;color:#111;max-width:1100px}
h1{margin-bottom:0}h2{margin-top:36px;border-bottom:1px solid #ddd}
h3{margin:18px 0 6px;color:#333}table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid #eee;
font-size:14px;vertical-align:top}th{background:#f6f6f6}
.muted{color:#666;font-size:13px}.sp{color:#b45309}.up{color:#b91c1c}
.down{color:#15803d}.pill{display:inline-block;padding:1px 6px;
border-radius:4px;background:#eef;font-size:12px}
"""


def _esc(v):
    return html.escape("" if v is None else str(v))


def _table(headers, rows):
    head = "".join("<th>%s</th>" % _esc(h) for h in headers)
    body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in row)
                   for row in rows)
    return "<table><tr>%s</tr>%s</table>" % (head, body)


def render_report(store, threshold=10.0):
    parts = []
    alerts = all_alerts(store, threshold)
    parts.append("<h2>Price alerts (threshold %s%%)</h2>" % _esc(threshold))
    if alerts:
        rows = []
        for a in alerts:
            cls = {"price_up": "up", "price_down": "down"}.get(a["kind"], "")
            change = ("%+.1f%%" % a["change_pct"]
                      if a["change_pct"] is not None else "")
            rows.append([
                "<span class='%s'>%s</span>" % (cls, _esc(a["kind"])),
                _esc(a["source"]), _esc(a["query"]), _esc(a["item_id"]),
                _esc(a["title"]), _esc(a["old_price"]), _esc(a["new_price"]),
                "<span class='%s'>%s</span>" % (cls, _esc(change))])
        parts.append(_table(["Kind", "Source", "Query", "Item", "Title",
                             "Old", "New", "Change"], rows))
    else:
        parts.append("<p class='muted'>No alerts.</p>")
    for source, query in tracked_pairs(store):
        latest, previous = store.latest_runs(
            "listings", ("item_id",), source=source, query=query)
        if not latest:
            continue
        latest.sort(key=lambda r: r["position"])
        parts.append("<h2>%s: %s</h2>" % (_esc(source.capitalize()),
                                            _esc(query)))
        parts.append("<div class='muted'>fetched %s, %d items%s</div>" % (
            _esc(latest[0]["fetched_at"]), len(latest),
            ", previous run %s" % _esc(previous[0]["fetched_at"])
            if previous else ""))
        old_prices = {(r["item_id"] or r["title"]): r["price"]
                      for r in previous}
        rows = []
        for r in latest:
            prev = old_prices.get(r["item_id"] or r["title"])
            delta = ""
            if prev not in (None, 0) and r["price"] is not None:
                pct = 100.0 * (r["price"] - prev) / prev
                if abs(pct) >= 0.05:
                    delta = "<span class='%s'>%+.1f%%</span>" % (
                        "up" if pct > 0 else "down", pct)
            rows.append([
                _esc(r["position"]) + (
                    " <span class='sp'>(sponsored)</span>"
                    if r["is_sponsored"] else ""),
                "<a href='%s'>%s</a>" % (_esc(r["url"]), _esc(r["title"]))
                if r["url"] else _esc(r["title"]),
                _esc(r["price_text"] or ""), delta,
                _esc(r["rating"] if r["rating"] is not None else ""),
                _esc(r["reviews"] if r["reviews"] is not None else ""),
                _esc(r["condition"] or r["item_id"] or "")])
        parts.append(_table(["#", "Title", "Price", "vs previous", "Rating",
                             "Reviews", "Condition / id"], rows))
    body = "\n".join(parts)
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Product research</title><style>%s</style></head><body>"
            "<h1>Product research</h1><div class='muted'>%s</div>%s"
            "</body></html>" % (CSS, _esc(os.path.abspath(store.path)),
                                body))


def cmd_report(args, store):
    directory = os.path.dirname(args.out)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(render_report(store, args.threshold))
    print("Report written to %s" % os.path.abspath(args.out))
    return 0


# ---- init / export ---------------------------------------------------------
def cmd_init(args, store):
    path = args.config
    if os.path.exists(path) and not args.force:
        print("%s already exists (use --force to overwrite)." % path)
        return 1
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not (args.amazon or args.ebay):
        shutil.copyfile(EXAMPLE_CONFIG, path)
        print("Copied the example config to %s. Edit the queries, then run "
              "`research track`." % path)
        return 0
    config = {"amazon": args.amazon, "ebay": args.ebay, "pages": args.pages,
              "alert_threshold_pct": args.threshold}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")
    print("Wrote %s:" % path)
    print(json.dumps(config, indent=2))
    print("\nNext: `research track` (then `research report`).")
    return 0


def cmd_export(args, store):
    os.makedirs(args.dir, exist_ok=True)
    path = store.export("listings", args.dir, args.format)
    target = os.path.join(args.dir, "research_listings.%s" % args.format)
    os.replace(path, target)
    print(target)
    return 0


# ---- parser -----------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="research", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=os.environ.get("RESEARCH_DB", DEFAULT_DB),
                   help="SQLite file (default data/research.sqlite or "
                        "$RESEARCH_DB)")
    p.add_argument("--chrome-binary", default=os.environ.get("SEO_CHROME"))
    p.add_argument("--version", action="version",
                   version="research %s" % seo.__version__)
    sub = p.add_subparsers(dest="command", required=True)

    def browser_args(sp):
        sp.add_argument("--proxy", help="user:pass@host:port")
        sp.add_argument("--headless", action="store_true")

    sp = sub.add_parser("amazon", help="Amazon search results")
    sp.add_argument("query")
    sp.add_argument("--pages", type=int, default=1)
    sp.add_argument("--domain", default="amazon.com")
    sp.add_argument("--force", action="store_true")
    browser_args(sp)
    sp.set_defaults(func=cmd_amazon)

    sp = sub.add_parser("ebay", help="eBay search results")
    sp.add_argument("query")
    sp.add_argument("--pages", type=int, default=1)
    sp.add_argument("--site", default="www.ebay.com")
    sp.add_argument("--force", action="store_true")
    browser_args(sp)
    sp.set_defaults(func=cmd_ebay)

    sp = sub.add_parser("track", help="run every query in the config and "
                                      "report price alerts")
    sp.add_argument("--config", default=DEFAULT_CONFIG)
    sp.add_argument("--per-session", type=int, default=5)
    sp.add_argument("--on-challenge", choices=("stop", "skip"),
                    default="stop")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--threshold", type=float,
                    help="alert when price moves more than this percent")
    browser_args(sp)
    sp.set_defaults(func=cmd_track)

    sp = sub.add_parser("alerts", help="price changes since the previous run")
    sp.add_argument("--threshold", type=float, default=10.0)
    sp.add_argument("--out", default=ALERTS_PATH)
    sp.set_defaults(func=cmd_alerts)

    sp = sub.add_parser("report", help="write an HTML report")
    sp.add_argument("--out",
                    default=os.path.join("data", "research_report.html"))
    sp.add_argument("--threshold", type=float, default=10.0)
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("init", help="write the research config")
    sp.add_argument("--config", default=DEFAULT_CONFIG)
    sp.add_argument("--amazon", action="append", default=[],
                    metavar="QUERY")
    sp.add_argument("--ebay", action="append", default=[], metavar="QUERY")
    sp.add_argument("--pages", type=int, default=1)
    sp.add_argument("--threshold", type=float, default=10.0)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("export", help="export listings to CSV/JSON")
    sp.add_argument("--dir", default="data")
    sp.add_argument("--format", choices=("csv", "json"), default="csv")
    sp.set_defaults(func=cmd_export)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "track" and not os.path.exists(args.config):
        print("No config at %s. Create one with:\n  research init --amazon "
              "\"wireless mouse\" --ebay \"wireless mouse\"" % args.config,
              file=sys.stderr)
        return 2
    store = storage.Storage(args.db)
    try:
        return args.func(args, store)
    except ChallengeError as e:
        print("CHALLENGED: %s" % e, file=sys.stderr)
        return 3
    except MissingDependency as e:
        print("SETUP PROBLEM: %s" % e, file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
