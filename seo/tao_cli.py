"""Bittensor subnet stats and prices from taostats.io. Installed as `tao`.

    tao subnets                       # fetch and print the subnet table
    tao subnet 8                      # latest row + price history
    tao watch --threshold 10          # fetch, compare with the last run,
                                      # warn on big price / market-cap moves
    tao report                        # data/tao_report.html
    tao init --netuids 1,8,19         # seo/config/tao.json
    tao export --format csv
"""
import argparse
import html
import json
import os
import sys

import seo
from seo import cli as seo_cli
from seo import storage
from seo.models import ChallengeError, MissingDependency, utcnow
from seo.sources import taostats

DEFAULT_DB = os.path.join("data", "tao.sqlite")
DEFAULT_CONFIG = os.path.join("seo", "config", "tao.json")
EXAMPLE_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "config", "tao.example.json")
TABLE = "subnet_stats"
WATCH_FIELDS = (("price_tao", "price"), ("market_cap_tao", "market cap"))


# ---- alerts (pure) ----------------------------------------------------------
def _pct(old, new):
    if old in (None, 0) or new is None:
        return None
    return 100.0 * (new - old) / old


def price_alerts(latest_rows, previous_rows, threshold=10.0, netuids=None):
    """Compare two runs (lists of dict rows). Returns alert dicts with
    kind in price_up / price_down / mcap_up / mcap_down / new / gone."""
    allowed = set(int(n) for n in netuids) if netuids else None
    latest = {r["netuid"]: r for r in latest_rows}
    previous = {r["netuid"]: r for r in previous_rows}
    alerts = []
    for netuid in sorted(set(latest) | set(previous)):
        if allowed is not None and netuid not in allowed:
            continue
        new, old = latest.get(netuid), previous.get(netuid)
        name = (new or old).get("name")
        if new is None:
            alerts.append(dict(netuid=netuid, name=name, kind="gone",
                               field=None, old=old.get("price_tao"),
                               new=None, change_pct=None))
            continue
        if old is None:
            if previous:
                alerts.append(dict(netuid=netuid, name=name, kind="new",
                                   field=None, old=None,
                                   new=new.get("price_tao"),
                                   change_pct=None))
            continue
        for field, label in WATCH_FIELDS:
            change = _pct(old.get(field), new.get(field))
            if change is None or abs(change) < threshold:
                continue
            prefix = "price" if field == "price_tao" else "mcap"
            alerts.append(dict(
                netuid=netuid, name=name,
                kind="%s_%s" % (prefix, "up" if change > 0 else "down"),
                field=label, old=old.get(field), new=new.get(field),
                change_pct=round(change, 2)))
    return alerts


def format_alert(a):
    if a["kind"] == "gone":
        return "WARNING SN%s %s disappeared from the list" % (
            a["netuid"], a["name"] or "")
    if a["kind"] == "new":
        return "WARNING SN%s %s is new (price τ%s)" % (
            a["netuid"], a["name"] or "", _fmt(a["new"]))
    return "WARNING SN%s %s %s %s %.1f%% (τ%s -> τ%s)" % (
        a["netuid"], a["name"] or "", a["field"],
        "up" if a["change_pct"] > 0 else "down", abs(a["change_pct"]),
        _fmt(a["old"]), _fmt(a["new"]))


def _fmt(value, digits=4):
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return "{:,.0f}".format(value)
    return ("{:.%df}" % digits).format(value).rstrip("0").rstrip(".")


# ---- commands ---------------------------------------------------------------
def _fetch_and_save(args, store):
    with seo_cli._session(args) as s:
        rows = taostats.fetch_subnets(s)
    if not rows:
        raise RuntimeError(
            "No subnet table found on %s. Capture the page with "
            "`seo capture taostats --out page.html` and refresh the parser."
            % taostats.SUBNETS_URL)
    store.save(rows, stamp=utcnow())
    return rows


def _print_table(rows):
    print("%6s  %-32s %12s %8s %14s %9s" % (
        "netuid", "name", "price τ", "24h %", "market cap τ", "emis %"))
    for r in rows:
        get = r.get if isinstance(r, dict) else lambda k: getattr(r, k)
        print("%6s  %-32s %12s %8s %14s %9s" % (
            get("netuid"), (get("name") or "")[:32], _fmt(get("price_tao")),
            _fmt(get("change_24h_pct"), 1), _fmt(get("market_cap_tao")),
            _fmt(get("emission_pct"), 2)))


def cmd_subnets(args, store):
    if not args.force and store.fetched_today(
            TABLE, source_url=taostats.SUBNETS_URL):
        print("Already fetched today; use --force to refetch. Latest:")
        latest, _ = store.latest_runs(TABLE, ("netuid",),
                                      source_url=taostats.SUBNETS_URL)
        _print_table(sorted(latest, key=lambda r: r["netuid"]))
        return 0
    rows = _fetch_and_save(args, store)
    _print_table(rows)
    print("Saved %d subnets." % len(rows))
    return 0


def cmd_subnet(args, store):
    rows = store.rows(TABLE, netuid=args.netuid)
    if not rows:
        print("No data for subnet %d yet; run `tao subnets`." % args.netuid)
        return 1
    latest = rows[-1]
    print("SN%d %s" % (latest["netuid"], latest["name"] or ""))
    for key in ("price_tao", "price_usd", "change_24h_pct",
                "change_7d_pct", "market_cap_tao", "emission_pct",
                "volume_24h_tao", "tao_in_pool"):
        print("  %-16s %s" % (key, _fmt(latest[key])))
    print("  history (price τ):")
    for r in rows[-args.history:]:
        print("    %s  %s" % (r["fetched_at"], _fmt(r["price_tao"])))
    return 0


def _netuids(text):
    if not text:
        return None
    return [int(x) for x in str(text).replace(";", ",").split(",")
            if x.strip()]


def cmd_watch(args, store):
    netuids = _netuids(args.netuids)
    if netuids is None and os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
        netuids = cfg.get("netuids") or None
        if args.threshold is None:
            args.threshold = cfg.get("alert_threshold_pct")
    threshold = 10.0 if args.threshold is None else float(args.threshold)
    _fetch_and_save(args, store)
    latest, previous = store.latest_runs(TABLE, ("netuid",),
                                         source_url=taostats.SUBNETS_URL)
    alerts = price_alerts(latest, previous, threshold, netuids)
    shown = [r for r in sorted(latest, key=lambda r: r["netuid"])
             if not netuids or r["netuid"] in set(netuids)]
    _print_table(shown)
    if not previous:
        print("First run recorded; alerts start with the next run.")
    for a in alerts:
        line = format_alert(a)
        print(line)
        if os.environ.get("GITHUB_ACTIONS"):
            print("::warning::%s" % line[len("WARNING "):])
    if not alerts and previous:
        print("No moves beyond %.1f%% since %s." % (
            threshold, previous[0]["fetched_at"]))
    out = os.path.join(os.path.dirname(store.path) or ".", "tao_alerts.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"threshold_pct": threshold, "checked_at": utcnow(),
                   "previous_run": previous[0]["fetched_at"]
                   if previous else None, "alerts": alerts}, fh, indent=2)
    print("Alerts written to %s" % out)
    return 2 if alerts and args.fail_on_alert else 0


CSS = """
body{font-family:system-ui,sans-serif;margin:24px;color:#111;max-width:1100px}
h1{margin-bottom:0}h2{margin-top:32px;border-bottom:1px solid #ddd}
table{border-collapse:collapse;width:100%}th,td{text-align:right;padding:4px 8px;
border-bottom:1px solid #eee;font-size:14px}th{background:#f6f6f6}
td:nth-child(2),th:nth-child(2){text-align:left}.muted{color:#666;font-size:13px}
.up{color:#15803d}.down{color:#b91c1c}.warn{background:#fff7ed;padding:6px 10px;
border-left:4px solid #ea580c;margin:6px 0}
"""


def _esc(v):
    return html.escape("" if v is None else str(v))


def _delta(old_row, new_row, field):
    if old_row is None:
        return ""
    change = _pct(old_row.get(field), new_row.get(field))
    if change is None:
        return ""
    cls = "up" if change > 0 else "down"
    return "<span class='%s'>%+.1f%%</span>" % (cls, change)


def render_report(store, threshold=10.0):
    latest, previous = store.latest_runs(TABLE, ("netuid",),
                                         source_url=taostats.SUBNETS_URL)
    prev_by = {r["netuid"]: r for r in previous}
    alerts = price_alerts(latest, previous, threshold)
    parts = ["<!doctype html><html><head><meta charset='utf-8'>"
             "<title>Bittensor subnets</title><style>%s</style></head>"
             "<body><h1>Bittensor subnets</h1>" % CSS]
    if not latest:
        parts.append("<p class='muted'>No data yet. Run <code>tao subnets"
                     "</code> or <code>tao watch</code>.</p>")
    else:
        parts.append("<div class='muted'>Source %s, fetched %s%s</div>" % (
            _esc(taostats.SUBNETS_URL), _esc(latest[0]["fetched_at"]),
            ", compared with %s" % _esc(previous[0]["fetched_at"])
            if previous else ""))
        parts.append("<h2>Alerts (moves beyond %.1f%%)</h2>" % threshold)
        if alerts:
            for a in alerts:
                parts.append("<div class='warn'>%s</div>" % _esc(
                    format_alert(a)[len("WARNING "):]))
        else:
            parts.append("<p class='muted'>None.</p>")
        parts.append("<h2>Latest</h2><table><tr>%s</tr>" % "".join(
            "<th>%s</th>" % h for h in (
                "netuid", "name", "price τ", "Δ price", "24h %", "7d %",
                "market cap τ", "Δ mcap", "emission %", "volume 24h τ",
                "TAO in pool")))
        for r in sorted(latest, key=lambda r: r["netuid"]):
            old = prev_by.get(r["netuid"])
            parts.append("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in (
                _esc(r["netuid"]), _esc(r["name"]), _fmt(r["price_tao"]),
                _delta(old, r, "price_tao"), _fmt(r["change_24h_pct"], 1),
                _fmt(r["change_7d_pct"], 1), _fmt(r["market_cap_tao"]),
                _delta(old, r, "market_cap_tao"),
                _fmt(r["emission_pct"], 2), _fmt(r["volume_24h_tao"]),
                _fmt(r["tao_in_pool"]))))
        parts.append("</table>")
    parts.append("</body></html>")
    return "\n".join(parts)


def cmd_report(args, store):
    threshold = 10.0 if args.threshold is None else float(args.threshold)
    directory = os.path.dirname(args.out) or "."
    os.makedirs(directory, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(render_report(store, threshold))
    print("Report written to %s" % os.path.abspath(args.out))
    return 0


def cmd_init(args, store):
    path = args.config
    if os.path.exists(path) and not args.force:
        print("%s already exists (use --force to overwrite)." % path)
        return 1
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(EXAMPLE_CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if args.netuids:
        cfg["netuids"] = _netuids(args.netuids)
    if args.threshold is not None:
        cfg["alert_threshold_pct"] = float(args.threshold)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    print("Wrote %s:\n%s\nNext: `tao watch`" % (path, json.dumps(cfg,
                                                                indent=2)))
    return 0


def cmd_export(args, store):
    print(store.export(TABLE, args.dir, args.format))
    return 0


# ---- parser -----------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="tao", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=os.environ.get("TAO_DB", DEFAULT_DB),
                   help="SQLite file (default data/tao.sqlite or $TAO_DB)")
    p.add_argument("--chrome-binary", default=os.environ.get("SEO_CHROME"))
    p.add_argument("--headless", action="store_true")
    p.add_argument("--proxy")
    p.add_argument("--version", action="version",
                   version="tao %s" % seo.__version__)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("subnets", help="fetch the subnet table")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_subnets)

    sp = sub.add_parser("subnet", help="latest stats + history for one")
    sp.add_argument("netuid", type=int)
    sp.add_argument("--history", type=int, default=10)
    sp.set_defaults(func=cmd_subnet)

    sp = sub.add_parser("watch", help="fetch and warn about big moves")
    sp.add_argument("--threshold", type=float,
                    help="percent (default 10 or the config value)")
    sp.add_argument("--netuids", help="e.g. 1,8,19 (default: config/all)")
    sp.add_argument("--config", default=DEFAULT_CONFIG)
    sp.add_argument("--force", action="store_true",
                    help="(watch always fetches; kept for symmetry)")
    sp.add_argument("--fail-on-alert", action="store_true",
                    help="exit 2 when any alert fires")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("report", help="write an HTML report")
    sp.add_argument("--out", default=os.path.join("data", "tao_report.html"))
    sp.add_argument("--threshold", type=float)
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("init", help="write seo/config/tao.json")
    sp.add_argument("--config", default=DEFAULT_CONFIG)
    sp.add_argument("--netuids", help="e.g. 1,8,19")
    sp.add_argument("--threshold", type=float)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("export", help="dump subnet_stats to CSV/JSON")
    sp.add_argument("--dir", default="data")
    sp.add_argument("--format", choices=("csv", "json"), default="csv")
    sp.set_defaults(func=cmd_export)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    store = storage.Storage(args.db)
    try:
        return args.func(args, store)
    except ChallengeError as e:
        print("BLOCKED: %s" % e, file=sys.stderr)
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
