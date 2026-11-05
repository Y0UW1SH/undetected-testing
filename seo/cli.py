"""Command-line interface. Installed as `seo` (or `python -m seo`).

First run:
    seo doctor --browser          # is Chrome / SeleniumBase / a display OK?
    seo init --location "Austin, TX" --serp "plumber" --local "plumber"
    seo batch                     # run everything in the config
    seo report                    # data/report.html

Single commands:
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
import os
import shutil
import sys

import seo
from seo import canary, doctor, geo, report, storage
from seo.browser import Session
from seo.local import grid as grid_mod
from seo.local import maps, nap
from seo.models import ChallengeError, MissingDependency, utcnow
from seo.serp import google
from seo.sources import reddit

DEFAULT_CONFIG = os.path.join("seo", "config", "keywords.json")
EXAMPLE_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "config", "keywords.example.json")


def _resolve_location(store, text, lat=None, lng=None):
    """Return (lat, lng, canonical_name, label). Uses explicit
    coordinates if given, else the geocode cache, else Nominatim. The
    canonical name used for Google's uule parameter comes from the
    geocoder's address details ("Austin,Texas,United States"), never
    from the raw text, which may hold abbreviations or no country."""
    if lat is not None and lng is not None:
        return lat, lng, None, text or "%s,%s" % (lat, lng)
    if not text:
        return None, None, None, ""
    cached = store.cached_geocode(text)
    if cached is None:
        try:
            g_lat, g_lng, display, canonical = geo.geocode(text)
        except Exception as e:
            raise LookupError(
                "Could not geocode %r (%s). Check the spelling, or pass "
                "--lat and --lng instead." % (text, str(e)[:120])) from e
        store.cache_geocode(text, g_lat, g_lng, display, canonical)
    else:
        g_lat, g_lng, display, canonical = cached
    return g_lat, g_lng, canonical, text


def _chrome_kwargs(args):
    binary = getattr(args, "chrome_binary", None)
    return {"browser_executable_path": binary} if binary else {}


def _session(args, geoloc=None):
    return Session(geoloc=geoloc, mobile=getattr(args, "mobile", False),
                   proxy=getattr(args, "proxy", None),
                   headless=getattr(args, "headless", False),
                   **_chrome_kwargs(args))


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
        print("  %2d. %s  %s" % (r.position, r.domain or r.source, r.title))
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


def cmd_reddit(args, store):
    """Reddit search: brand mentions, questions people ask, competitors."""
    key = dict(query=args.query, sort=args.sort)
    if not args.force and store.fetched_today("reddit_posts", **key):
        print("Already fetched today; use --force to refetch.")
        return 0
    with _session(args) as s:
        posts = reddit.fetch(s, args.query, args.subreddit, args.sort,
                             args.time, args.limit)
    for p in posts:
        print("%2d. r/%-18s %5s pts %4s cmts  %s" % (
            p.position, (p.subreddit or "")[:18], p.score, p.num_comments,
            p.title[:70]))
    print("Saved %d posts." % store.save(posts, stamp=utcnow()))
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


def _batch_groups(config):
    """Jobs from a config, grouped by (location, device) so that a
    browser session is only ever shared by jobs that need the same
    emulated location and device."""
    blocks = config if isinstance(config, list) else [config]
    groups = {}
    for block in blocks:
        loc = block.get("location", "")
        device = block.get("device", "desktop")
        jobs = groups.setdefault((loc, device), [])
        for q in block.get("serp", []):
            jobs.append({"kind": "serp", "query": q})
        for q in block.get("local", []):
            jobs.append({"kind": "local", "query": q})
        for g in block.get("grid", []):
            jobs.append({"kind": "grid", "query": g["query"], "extra": g})
        for r in block.get("reddit", []):
            if isinstance(r, str):
                r = {"query": r}
            jobs.append({"kind": "reddit", "query": r["query"], "extra": r})
    return groups


def _job_done_today(store, job, label, device):
    kind, q = job["kind"], job["query"]
    if kind == "serp":
        return store.fetched_today("serp_results", query=q, location=label,
                                   device=device)
    if kind == "local":
        return store.fetched_today("businesses", query=q, location=label)
    if kind == "reddit":
        return store.fetched_today(
            "reddit_posts", query=q,
            sort=job["extra"].get("sort", "relevance"))
    return store.fetched_today("grid_cells", keyword=q,
                               target=job["extra"]["target"], center=label)


def _run_job(s, store, args, job, device, lat, lng, canonical, label):
    kind, q = job["kind"], job["query"]
    if kind == "serp":
        parsed = google.fetch_serp(s, q, canonical, device, 1, label)
        store.save(google.flatten(parsed))
        _report_serp(parsed, 1)
    elif kind == "local":
        biz = maps.fetch_listings(s, q, lat, lng, label,
                                  max_results=args.max)
        store.save(biz)
        print("    %d listings" % len(biz))
    elif kind == "reddit":
        extra = job["extra"]
        posts = reddit.fetch(s, q, extra.get("subreddit"),
                             extra.get("sort", "relevance"),
                             extra.get("time", "all"), extra.get("limit", 25))
        store.save(posts, stamp=utcnow())
        print("    %d posts" % len(posts))
    else:
        if lat is None or lng is None:
            raise ValueError("grid job %r needs a block 'location'" % q)
        extra = job["extra"]
        cells = grid_mod.run_grid(
            s, q, extra["target"], lat, lng, size=extra.get("size", 5),
            spacing_km=extra.get("spacing_km", 1.0), center_label=label)
        store.save(cells, stamp=utcnow())
        _print_json(grid_mod.summarize(cells))
        out = "data/grid_%s.html" % grid_mod.normalize_name(
            extra["target"]).replace(" ", "_")
        grid_mod.heatmap_html(cells, out)


def cmd_batch(args, store):
    """Run every job in a JSON config, rotating the browser session every
    --per-session jobs so one flagged IP/session does not poison the rest.

    Config format (see seo/config/keywords.example.json):
      {"location": "Austin, TX", "device": "desktop",
       "serp": ["emergency plumber", "water heater repair"],
       "local": ["plumber"],
       "grid": [{"query": "plumber", "target": "Joe's Plumbing"}]}
    A list of such objects is also accepted. Jobs that already ran today
    for the same location/device are skipped unless --force is given.
    A job that fails for a reason other than a Google challenge is
    counted as an error and the run continues.
    """
    with open(args.config, encoding="utf-8") as fh:
        config = json.load(fh)
    counts = {"done": 0, "skipped": 0, "errors": 0, "challenged_jobs": 0,
              "challenged_sessions": 0}
    stop = False
    for (loc, device), jobs in _batch_groups(config).items():
        if stop:
            break
        try:
            lat, lng, canonical, label = _resolve_location(store, loc)
        except Exception as e:
            print("Cannot resolve location %r: %s" % (loc, e),
                  file=sys.stderr)
            counts["errors"] += len(jobs)
            continue
        pending = [j for j in jobs
                   if args.force or not _job_done_today(store, j, label,
                                                        device)]
        counts["skipped"] += len(jobs) - len(pending)
        geoloc = (lat, lng) if lat is not None else None
        queue = list(pending)
        while queue and not stop:
            batch, queue = queue[:args.per_session], queue[args.per_session:]
            try:
                with Session(geoloc=geoloc, mobile=device == "mobile",
                             proxy=args.proxy, headless=args.headless,
                             **_chrome_kwargs(args)) as s:
                    for idx, job in enumerate(batch):
                        print(">>> %s %r @ %s [%s]" % (
                            job["kind"], job["query"], label or "anywhere",
                            device))
                        try:
                            _run_job(s, store, args, job, device, lat, lng,
                                     canonical, label)
                            counts["done"] += 1
                        except ChallengeError:
                            # Skip this job; give the rest of the batch a
                            # fresh session (unless --on-challenge stop).
                            counts["challenged_jobs"] += 1
                            queue = batch[idx + 1:] + queue
                            raise
                        except Exception as e:
                            counts["errors"] += 1
                            print("ERROR in %s %r: %s" % (
                                job["kind"], job["query"], e),
                                file=sys.stderr)
                        s.pause()
            except ChallengeError as e:
                counts["challenged_sessions"] += 1
                print("CHALLENGED, abandoning this session: %s" % e,
                      file=sys.stderr)
                if args.on_challenge == "stop":
                    stop = True
    _print_json(counts)
    if counts["done"]:
        print("Next: `seo report` writes data/report.html")
        return 0
    if counts["challenged_sessions"]:
        return 3
    return 1 if counts["errors"] else 0


def cmd_export(args, store):
    for table in storage.TABLES:
        print(store.export(table, args.dir, args.format))
    return 0


CAPTURE_KINDS = ("serp", "maps", "amazon", "ebay", "reddit", "taostats")


def capture_url(kind, query):
    """URL the capture command loads for a kind; lazy imports keep the
    SEO CLI independent of the other tools' modules."""
    if kind == "serp":
        return google.build_url(query)
    if kind == "maps":
        return maps.build_maps_url(query)
    if kind == "amazon":
        from seo.sources import amazon
        return amazon.build_url(query)
    if kind == "ebay":
        from seo.sources import ebay
        return ebay.build_url(query)
    if kind == "reddit":
        from seo.sources import reddit
        return reddit.build_url(query, as_json=False)
    if kind == "taostats":
        return "https://taostats.io/subnets" if not query.strip() else (
            "https://taostats.io/subnets/%s" % query.strip())
    raise ValueError(kind)


def cmd_capture(args, store):
    """Save the live HTML of a page as a parser fixture."""
    with _session(args) as s:
        s.goto(capture_url(args.kind, args.query), check_google=False)
        if args.kind in ("serp", "amazon", "ebay", "reddit", "taostats"):
            s.scroll(times=4, amount=15)
        if args.kind == "maps":
            maps.scroll_feed(s, max_results=10, max_rounds=3)
        html = s.html()
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("Wrote %d bytes to %s" % (len(html), args.out))
    return 0


def cmd_doctor(args, store):
    results = doctor.checks(args.config, os.path.dirname(store.path) or ".")
    if args.network:
        results.append(doctor.geocoder_check())
    if args.browser:
        results.append(doctor.browser_check(
            Session, headless=args.headless, min_pause=0.2, max_pause=0.4,
            **_chrome_kwargs(args)))
    print(doctor.render(results))
    failed = [r for r in results if r["status"] == doctor.FAIL]
    if failed:
        print("\n%d check(s) failed." % len(failed))
        return 1
    print("\nAll required checks passed.")
    return 0


def cmd_init(args, store):
    """Write a config file. With no keyword arguments, copy the example."""
    path = args.config
    if os.path.exists(path) and not args.force:
        print("%s already exists (use --force to overwrite)." % path)
        return 1
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not (args.serp or args.local or args.target or args.reddit):
        shutil.copyfile(EXAMPLE_CONFIG, path)
        print("Copied the example config to %s. Edit the location and "
              "keywords, then run `seo batch`." % path)
        return 0
    block = {"location": args.location, "device": args.device}
    if args.serp:
        block["serp"] = args.serp
    if args.local:
        block["local"] = args.local
    if args.reddit:
        block["reddit"] = args.reddit
    if args.target:
        grid_query = args.local[0] if args.local else (
            args.serp[0] if args.serp else args.target)
        block["grid"] = [{"query": grid_query, "target": args.target,
                          "size": args.grid_size,
                          "spacing_km": args.grid_spacing}]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([block], fh, indent=2)
        fh.write("\n")
    print("Wrote %s:" % path)
    print(json.dumps([block], indent=2))
    print("\nNext: `seo batch` (then `seo report`).")
    return 0


def cmd_report(args, store):
    path = report.write(store, args.out)
    print("Report written to %s" % os.path.abspath(path))
    return 0


# ---- parser -----------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="seo", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=os.environ.get("SEO_DB",
                                                  storage.DEFAULT_DB),
                   help="SQLite file (default data/seo.sqlite or $SEO_DB)")
    p.add_argument("--chrome-binary", default=os.environ.get("SEO_CHROME"),
                   help="path to a Chrome/Chromium binary ($SEO_CHROME)")
    p.add_argument("--version", action="version",
                   version="seo %s" % seo.__version__)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("doctor", help="check Python, Chrome, display, "
                                       "config; --browser opens Chrome")
    sp.add_argument("--config", default=DEFAULT_CONFIG)
    sp.add_argument("--browser", action="store_true",
                    help="also launch the stealth browser once")
    sp.add_argument("--network", action="store_true",
                    help="also test the geocoder")
    sp.add_argument("--headless", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("init", help="write a keywords config file")
    sp.add_argument("--config", default=DEFAULT_CONFIG)
    sp.add_argument("--location", default="", help='e.g. "Austin, TX"')
    sp.add_argument("--device", choices=("desktop", "mobile"),
                    default="desktop")
    sp.add_argument("--serp", action="append", default=[],
                    metavar="KEYWORD", help="web-search keyword (repeat)")
    sp.add_argument("--local", action="append", default=[],
                    metavar="KEYWORD", help="Maps keyword (repeat)")
    sp.add_argument("--reddit", action="append", default=[],
                    metavar="KEYWORD", help="Reddit search (repeat)")
    sp.add_argument("--target", help="your business name for a rank grid")
    sp.add_argument("--grid-size", type=int, default=5)
    sp.add_argument("--grid-spacing", type=float, default=1.0)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("report", help="write an HTML report from the db")
    sp.add_argument("--out", default=os.path.join("data", "report.html"))
    sp.set_defaults(func=cmd_report)

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

    sp = sub.add_parser("reddit", help="Reddit search results")
    sp.add_argument("query")
    sp.add_argument("--subreddit", help="limit to one subreddit")
    sp.add_argument("--sort", choices=reddit.SORTS, default="relevance")
    sp.add_argument("--time", choices=reddit.TIMES, default="all")
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--mobile", action="store_true")
    sp.add_argument("--proxy")
    sp.add_argument("--headless", action="store_true")
    sp.set_defaults(func=cmd_reddit)

    sp = sub.add_parser("nap", help="NAP consistency across directories")
    sp.add_argument("business")
    sp.add_argument("--phone", required=True)
    sp.add_argument("--address", required=True)
    sp.add_argument("--city", default="")
    sp.add_argument("--mobile", action="store_true")
    sp.add_argument("--proxy")
    sp.add_argument("--headless", action="store_true")
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
    sp.add_argument("--config", default=DEFAULT_CONFIG)
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
    sp.add_argument("kind", choices=CAPTURE_KINDS)
    sp.add_argument("query", nargs="?", default="")
    sp.add_argument("--out", required=True)
    sp.add_argument("--mobile", action="store_true")
    sp.add_argument("--proxy")
    sp.add_argument("--headless", action="store_true")
    sp.set_defaults(func=cmd_capture)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "batch" and not os.path.exists(args.config):
        print("No config at %s. Create one with:\n  seo init --location "
              "\"Austin, TX\" --serp \"plumber\" --local \"plumber\""
              % args.config, file=sys.stderr)
        return 2
    store = storage.Storage(args.db)
    try:
        return args.func(args, store)
    except ChallengeError as e:
        print("CHALLENGED: %s" % e, file=sys.stderr)
        print("Stop this job; the runner IP is flagged. Retry later or "
              "from another IP.", file=sys.stderr)
        return 3
    except MissingDependency as e:
        print("SETUP PROBLEM: %s" % e, file=sys.stderr)
        return 4
    except LookupError as e:
        print("LOCATION PROBLEM: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
