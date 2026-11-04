"""Command-line interface.

    python -m seo serp "emergency plumber" --location "Austin, TX"
    python -m seo local "plumber" --location "Austin, TX" --details
    python -m seo grid "plumber" --target "Joe's Plumbing" \\
        --location "Austin, TX" --size 5 --spacing 1.5
    python -m seo snapshot "plumber" --location "Austin, TX"
    python -m seo nap "Joe's Plumbing" --phone 512-555-0100 \\
        --address "123 Main St, Austin, TX 78701"
    python -m seo batch --config seo/config/keywords.json
    python -m seo canary --queries 20
    python -m seo export --format csv
    python -m seo capture serp "query" --out tests/fixtures/new.html
"""
import argparse
import json
import sys

from seo import canary, geo, storage
from seo.browser import Session
from seo.local import grid as grid_mod
from seo.local import maps, nap
from seo.models import ChallengeError, utcnow
from seo.serp import google


def _resolve_location(store, text, lat=None, lng=None):
    """Return (lat, lng, canonical_name, label). Uses explicit
    coordinates if given, else the geocode cache, else Nominatim."""
    if lat is not None and lng is not None:
        return lat, lng, None, text or "%s,%s" % (lat, lng)
    if not text:
        return None, None, None, ""
    cached = store.cached_geocode(text)
    if cached is None:
        g_lat, g_lng, display = geo.geocode(text)
        store.cache_geocode(text, g_lat, g_lng, display)
    else:
        g_lat, g_lng, display = cached
    parts = [p.strip() for p in text.split(",")]
    canonical = geo.canonical_name(parts[0], parts[1] if len(parts) > 1
                                   else None)
    return g_lat, g_lng, canonical, text


def _session(args, geoloc=None):
    return Session(geoloc=geoloc, mobile=getattr(args, "mobile", False),
                   proxy=getattr(args, "proxy", None),
                   headless=getattr(args, "headless", False))


def _print_json(obj):
    print(json.dumps(obj, indent=2, default=str))


# ---- commands -------------------------------------------------------------
def cmd_serp(args, store):
    lat, lng, canonical, label = _resolve_location(
        store, args.location, args.lat, args.lng)
    device = "mobile" if args.mobile else "desktop"
    if not args.force and store.fetched_today(
            "serp_results", query=args.query, location=label,
            device=device):
        print("Already fetched today; use --force to refetch.")
        return 0
    geoloc = (lat, lng) if lat is not None else None
    with _session(args, geoloc) as s:
        all_records = []
        for page in range(1, args.pages + 1):
            parsed = google.fetch_serp(s, args.query, canonical, device,
                                       page, label)
            all_records.extend(google.flatten(parsed))
            _report_serp(parsed, page)
            if page < args.pages:
                s.pause()
    print("Saved %d records." % store.save(all_records))
    return 0


def _report_serp(parsed, page):
    print("--- page %d ---" % page)
    for r in parsed["ads"]:
        print("  [ad %d] %s  %s" % (r.position, r.domain, r.title))
    for r in parsed["featured"]:
        print("  [featured] %s  %s" % (r.domain, (r.snippet or "")[:80]))
    for e in parsed["local_pack"]:
        print("  [local %d] %s  %s (%s)" % (e.position, e.name, e.rating,
                                            e.reviews))
    for r in parsed["organic"]:
        print("  %2d. %s  %s" % (r.position, r.domain, r.title))
    for r in parsed["paa"]:
        print("  [paa] %s" % r.title)


def cmd_local(args, store):
    lat, lng, _, label = _resolve_location(store, args.location, args.lat,
                                           args.lng)
    geoloc = (lat, lng) if lat is not None else None
    with _session(args, geoloc) as s:
        businesses = maps.fetch_listings(
            s, args.query, lat, lng, label, max_results=args.max,
            details=args.details)
    for b in businesses:
        print("%2d. %s  %s (%s)  %s" % (b.position, b.name, b.rating,
                                        b.reviews, b.phone or ""))
    print("Saved %d businesses." % store.save(businesses))
    return 0


def cmd_grid(args, store):
    lat, lng, _, label = _resolve_location(store, args.location, args.lat,
                                           args.lng)
    if lat is None:
        print("grid needs --location or --lat/--lng", file=sys.stderr)
        return 2
    with _session(args, (lat, lng)) as s:
        cells = grid_mod.run_grid(
            s, args.query, args.target, lat, lng, size=args.size,
            spacing_km=args.spacing, center_label=label,
            on_cell=lambda c: print("  cell (%d,%d) rank=%s" % (
                c.row, c.col, c.rank)))
    store.save(cells, stamp=utcnow())
    _print_json(grid_mod.summarize(cells))
    out = args.out or "data/grid_%s.html" % grid_mod.normalize_name(
        args.target).replace(" ", "_")
    print("Heatmap written to %s" % grid_mod.heatmap_html(cells, out))
    return 0


def cmd_snapshot(args, store):
    """Top-N local competitors now, plus a diff against the previous run."""
    lat, lng, _, label = _resolve_location(store, args.location, args.lat,
                                           args.lng)
    geoloc = (lat, lng) if lat is not None else None
    with _session(args, geoloc) as s:
        businesses = maps.fetch_listings(
            s, args.query, lat, lng, label, max_results=args.max,
            details=True)
    store.save(businesses, stamp=utcnow())
    latest, previous = store.latest_runs(
        "businesses", ("name",), query=args.query, location=label)
    prev_by_name = {r["name"]: r for r in previous}
    for r in latest:
        old = prev_by_name.get(r["name"])
        delta = ""
        if old and old["reviews"] is not None and r["reviews"] is not None:
            diff = r["reviews"] - old["reviews"]
            delta = " (%+d reviews)" % diff if diff else ""
        moved = ""
        if old and old["position"] != r["position"]:
            moved = " was #%s" % old["position"]
        print("%2d. %s  %s (%s)%s%s" % (r["position"], r["name"],
                                        r["rating"], r["reviews"], delta,
                                        moved))
    gone = set(prev_by_name) - {r["name"] for r in latest}
    for name in sorted(gone):
        print("  dropped out: %s" % name)
    return 0


def cmd_nap(args, store):
    with _session(args) as s:
        records = nap.audit(s, args.business, args.phone, args.address,
                            args.city or "")
    for r in records:
        print("%-12s phone=%s address=%s  %s" % (
            r.source, r.phone_match, r.address_match, r.url))
    print("Saved %d records." % store.save(records))
    return 0


def cmd_canary(args, store):
    queries = canary.DEFAULT_QUERIES[:args.queries]
    if args.queries > len(canary.DEFAULT_QUERIES):
        queries = (canary.DEFAULT_QUERIES * (
            args.queries // len(canary.DEFAULT_QUERIES) + 1))[:args.queries]
    records = canary.run(queries, per_session=args.per_session,
                         mobile=args.mobile, proxy=args.proxy)
    store.save(records)
    summary = canary.summarize(records)
    _print_json(summary)
    if args.fail_above is not None and \
            summary["challenge_rate_pct"] > args.fail_above:
        return 1
    return 0


def cmd_batch(args, store):
    """Run every job in a JSON config, rotating the browser session every
    --per-session jobs so one flagged IP/session does not poison the rest.

    Config format (see seo/config/keywords.example.json):
      {"location": "Austin, TX", "device": "desktop",
       "serp": ["emergency plumber", "water heater repair"],
       "local": ["plumber"],
       "grid": [{"query": "plumber", "target": "Joe's Plumbing"}]}
    A list of such objects is also accepted.
    """
    with open(args.config, encoding="utf-8") as fh:
        config = json.load(fh)
    blocks = config if isinstance(config, list) else [config]
    jobs = []
    for block in blocks:
        loc = block.get("location", "")
        device = block.get("device", "desktop")
        for q in block.get("serp", []):
            jobs.append(("serp", q, loc, device, None))
        for q in block.get("local", []):
            jobs.append(("local", q, loc, device, None))
        for g in block.get("grid", []):
            jobs.append(("grid", g["query"], loc, device, g))
    done = challenged = skipped = 0
    i = 0
    while i < len(jobs):
        batch = jobs[i:i + args.per_session]
        i += args.per_session
        kind, q, loc, device, extra = batch[0]
        lat, lng, canonical, label = _resolve_location(store, loc)
        geoloc = (lat, lng) if lat is not None else None
        mobile = device == "mobile"
        try:
            with Session(geoloc=geoloc, mobile=mobile, proxy=args.proxy,
                         headless=args.headless) as s:
                for kind, q, loc, device, extra in batch:
                    table = {"serp": "serp_results", "local": "businesses",
                             "grid": "grid_cells"}[kind]
                    key = {"query": q} if kind != "grid" else {"keyword": q}
                    if not args.force and store.fetched_today(table, **key):
                        skipped += 1
                        continue
                    print(">>> %s %r @ %s" % (kind, q, label or "anywhere"))
                    if kind == "serp":
                        parsed = google.fetch_serp(s, q, canonical, device,
                                                   1, label)
                        store.save(google.flatten(parsed))
                        _report_serp(parsed, 1)
                    elif kind == "local":
                        biz = maps.fetch_listings(s, q, lat, lng, label,
                                                  max_results=args.max)
                        store.save(biz)
                        print("    %d listings" % len(biz))
                    else:
                        cells = grid_mod.run_grid(
                            s, q, extra["target"], lat, lng,
                            size=extra.get("size", 5),
                            spacing_km=extra.get("spacing_km", 1.0),
                            center_label=label)
                        store.save(cells, stamp=utcnow())
                        _print_json(grid_mod.summarize(cells))
                        out = "data/grid_%s.html" % grid_mod.normalize_name(
                            extra["target"]).replace(" ", "_")
                        grid_mod.heatmap_html(cells, out)
                    done += 1
                    s.pause()
        except ChallengeError as e:
            challenged += 1
            print("CHALLENGED, abandoning this session: %s" % e,
                  file=sys.stderr)
            if args.on_challenge == "stop":
                break
    _print_json({"done": done, "skipped": skipped,
                 "challenged_sessions": challenged})
    return 3 if challenged and not done else 0


def cmd_export(args, store):
    for table in storage.TABLES:
        print(store.export(table, args.dir, args.format))
    return 0


def cmd_capture(args, store):
    """Save the live HTML of a SERP or Maps page as a parser fixture."""
    with _session(args) as s:
        if args.kind == "serp":
            s.goto(google.build_url(args.query))
        else:
            s.goto(maps.build_maps_url(args.query))
            maps.scroll_feed(s, max_results=10, max_rounds=3)
        html = s.html()
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("Wrote %d bytes to %s" % (len(html), args.out))
    return 0


# ---- parser -----------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="seo", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=storage.DEFAULT_DB)
    sub = p.add_subparsers(dest="command", required=True)

    def loc_args(sp):
        sp.add_argument("--location", default="",
                        help='e.g. "Austin, TX"')
        sp.add_argument("--lat", type=float)
        sp.add_argument("--lng", type=float)
        sp.add_argument("--mobile", action="store_true")
        sp.add_argument("--proxy", help="user:pass@host:port")
        sp.add_argument("--headless", action="store_true",
                        help="no display at all (Xvfb is the default)")

    sp = sub.add_parser("serp", help="Google web search results")
    sp.add_argument("query")
    loc_args(sp)
    sp.add_argument("--pages", type=int, default=1)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_serp)

    sp = sub.add_parser("local", help="Google Maps listings")
    sp.add_argument("query")
    loc_args(sp)
    sp.add_argument("--max", type=int, default=20)
    sp.add_argument("--details", action="store_true",
                    help="visit each place page for phone/website/hours")
    sp.set_defaults(func=cmd_local)

    sp = sub.add_parser("grid", help="local rank grid for one business")
    sp.add_argument("query")
    sp.add_argument("--target", required=True, help="business name or CID")
    loc_args(sp)
    sp.add_argument("--size", type=int, default=5, help="odd number")
    sp.add_argument("--spacing", type=float, default=1.0, help="km")
    sp.add_argument("--out", help="heatmap HTML path")
    sp.set_defaults(func=cmd_grid)

    sp = sub.add_parser("snapshot", help="competitor snapshot + diff")
    sp.add_argument("query")
    loc_args(sp)
    sp.add_argument("--max", type=int, default=10)
    sp.set_defaults(func=cmd_snapshot)

    sp = sub.add_parser("nap", help="NAP consistency across directories")
    sp.add_argument("business")
    sp.add_argument("--phone", required=True)
    sp.add_argument("--address", required=True)
    sp.add_argument("--city", default="")
    sp.add_argument("--mobile", action="store_true")
    sp.add_argument("--proxy")
    sp.set_defaults(func=cmd_nap)

    sp = sub.add_parser("canary", help="measure Google challenge rate")
    sp.add_argument("--queries", type=int, default=10)
    sp.add_argument("--per-session", type=int, default=1)
    sp.add_argument("--mobile", action="store_true")
    sp.add_argument("--proxy")
    sp.add_argument("--fail-above", type=float,
                    help="exit 1 if challenge %% exceeds this")
    sp.set_defaults(func=cmd_canary)

    sp = sub.add_parser("batch", help="run all jobs from a JSON config")
    sp.add_argument("--config", default="seo/config/keywords.json")
    sp.add_argument("--per-session", type=int, default=5,
                    help="new browser every N jobs")
    sp.add_argument("--max", type=int, default=20)
    sp.add_argument("--proxy")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--on-challenge", choices=("stop", "skip"),
                    default="stop")
    sp.add_argument("--headless", action="store_true")
    sp.set_defaults(func=cmd_batch)

    sp = sub.add_parser("export", help="dump all tables to CSV/JSON")
    sp.add_argument("--dir", default="data")
    sp.add_argument("--format", choices=("csv", "json"), default="csv")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("capture", help="save live HTML as a fixture")
    sp.add_argument("kind", choices=("serp", "maps"))
    sp.add_argument("query")
    sp.add_argument("--out", required=True)
    sp.add_argument("--mobile", action="store_true")
    sp.add_argument("--proxy")
    sp.set_defaults(func=cmd_capture)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    store = storage.Storage(args.db)
    try:
        return args.func(args, store)
    except ChallengeError as e:
        print("CHALLENGED: %s" % e, file=sys.stderr)
        print("Stop this job; the runner IP is flagged. Retry later or "
              "from another IP.", file=sys.stderr)
        return 3
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
