"""A private watchlist and research browser served only on this PC."""
import argparse
from contextlib import closing
from functools import lru_cache
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
from urllib.parse import parse_qs, urlsplit
import webbrowser

from seo.tao_directory_view import _cached, _research

PREFS = Path("try_tao_out/local_watchlist.sqlite")
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MAX_GROUP_MEMBERS = 50
MAX_GROUP_SUBSET = 24


def valid_address(s):
    if not isinstance(s, str) or not re.fullmatch(
            r"5[1-9A-HJ-NP-Za-km-z]{47}", s):
        return False
    n = 0
    for c in s:
        n = n * 58 + ALPHABET.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return (len(raw) == 35 and raw[0] == 42 and raw[-2:] == hashlib.blake2b(
        b"SS58PRE" + raw[:-2]).digest()[:2])


def _read_db(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def preferences(path):
    if not path.is_file():
        return {}
    with closing(_read_db(path)) as db:
        db.row_factory = sqlite3.Row
        return {r["address"]: dict(r) for r in db.execute(
            "SELECT address,choice,label,note FROM local_choices")}


def save(path, item):
    fields = {"address", "choice", "label", "note"}
    if not isinstance(item, dict) or set(item) - fields:
        raise ValueError("Invalid watchlist entry.")
    a, choice = item.get("address"), item.get("choice")
    label, note = item.get("label", ""), item.get("note", "")
    if not valid_address(a):
        raise ValueError("Enter a Finney coldkey with a valid checksum.")
    if choice not in ("auto", "watch", "mute"):
        raise ValueError("Choose watch, mute, or automatic.")
    if (not isinstance(label, str) or not isinstance(note, str)
            or len(label) > 100 or len(note) > 2000):
        raise ValueError("Limit the label to 100 and the note to 2,000 "
                         "characters.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS local_choices (address TEXT "
                   "PRIMARY KEY,choice TEXT,label TEXT,note TEXT)")
        if choice == "auto" and not (label.strip() or note.strip()):
            db.execute("DELETE FROM local_choices WHERE address=?", (a,))
        else:
            db.execute("INSERT OR REPLACE INTO local_choices VALUES(?,?,?,?)",
                       (a, choice, label.strip(), note.strip()))


def apply(path, reasons):
    result = {a: list(v) for a, v in reasons.items()}
    for a, item in preferences(path).items():
        if item["choice"] == "mute":
            result.pop(a, None)
        elif item["choice"] == "watch" and valid_address(a):
            result.setdefault(a, []).append("personal")
    return result


def save_group(path, members):
    """Watch one server-resolved group in a transaction, retaining mutes."""
    if (not isinstance(members, list)
            or not 1 <= len(members) <= MAX_GROUP_MEMBERS
            or not all(valid_address(a) for a in members)
            or len(set(members)) != len(members)):
        raise ValueError("This group cannot be saved in one action. "
                         "Review its wallets individually (maximum 50).")
    path.parent.mkdir(parents=True, exist_ok=True)
    result = dict(watched=[], muted=[])
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS local_choices (address TEXT "
                   "PRIMARY KEY,choice TEXT,label TEXT,note TEXT)")
        for address in members:
            row = db.execute("SELECT choice FROM local_choices WHERE "
                             "address=?", (address,)).fetchone()
            if row and row[0] == "mute":
                result["muted"].append(address)
                continue
            if row:
                db.execute("UPDATE local_choices SET choice='watch' "
                           "WHERE address=?", (address,))
            else:
                db.execute("INSERT INTO local_choices VALUES(?,?,?,?)",
                           (address, "watch", "", ""))
            result["watched"].append(address)
    return result


@lru_cache(maxsize=2)
def _group_files(cache, files):
    from seo.tao_wallet_groups import build_groups
    reports = []
    for path, _, _ in files:
        try:
            with path.open(encoding="utf8") as source:
                report = json.load(source)
            if isinstance(report, dict):
                relative = path.relative_to(cache.resolve())
                report["_source"] = relative.as_posix()
                reports.append(report)
        except (OSError, ValueError):
            continue
    return build_groups(reports)


@lru_cache(maxsize=2)
def _research_files(cache, files):
    from seo.tao_local_watch import _cached_discovery, _selection
    from seo.tao_view import load_snapshot, newest_cached
    wallets, stamps = _research([], {p for p, _, _ in files})
    path = newest_cached(cache)
    reasons = {}
    if path:
        reasons, _ = _selection(load_snapshot(path), [],
                                discovery=_cached_discovery(cache))
    return wallets, stamps, reasons


class Model:
    def __init__(self, root):
        self.root = root
        self.cache = root / "github_out"
        self.db = self.cache / "taostats-directory.sqlite"
        self.prefs = root / PREFS

    def _files(self):
        paths = _cached(self.cache)
        return tuple(sorted((p, p.stat().st_mtime_ns, p.stat().st_size)
                            for p in paths))

    def _base(self):
        return (*_research_files(self.cache, self._files()),
                preferences(self.prefs))

    def groups(self):
        return _group_files(self.cache, self._files())

    def watch_group(self, item):
        if (not isinstance(item, dict)
                or set(item) not in ({"group_id"}, {"group_id", "members"})
                or not isinstance(item["group_id"], str)
                or not re.fullmatch(r"group-[0-9a-f]{16}", item["group_id"])):
            raise ValueError("Select a group from the local research.")
        group = next((g for g in self.groups()["groups"]
                      if g["id"] == item["group_id"]), None)
        if group is None:
            raise ValueError("This group is no longer available. "
                             "Refresh and review the current groups.")
        broad = (group.get("watchable") is False
                 or group.get("oversized") is True)
        if "members" in item:
            members = item["members"]
            limit = MAX_GROUP_SUBSET if broad else MAX_GROUP_MEMBERS
            if (not isinstance(members, list)
                    or not 1 <= len(members) <= limit
                    or not all(isinstance(a, str) for a in members)
                    or len(set(members)) != len(members)
                    or not set(members).issubset(group["members"])):
                raise ValueError("Choose up to %d distinct wallets from "
                                 "this group's member list." % limit)
            return save_group(self.prefs, members)
        if broad:
            raise ValueError("This group is too broad to watch in one action. "
                             "Select up to 24 members to watch together.")
        return save_group(self.prefs, group["members"])

    @staticmethod
    def _wallet(a, wallets, reasons, prefs):
        w = wallets.get(a, {})
        ranks = w.get("ranks", {})
        pref = prefs.get(a, {})
        return dict(address=a, choice=pref.get("choice", "auto"),
                    label=pref.get("label", ""), note=pref.get("note", ""),
                    automatic=bool(reasons.get(a)), reasons=reasons.get(a, []),
                    inspected=bool(w.get("stamp")), fills=w.get("fills", 0),
                    buys=w.get("buys", 0), sells=w.get("sells", 0),
                    known_realized=w.get("known_realized"),
                    known_open=w.get("known_open"),
                    unresolved_sale_proceeds=w.get("unresolved_proceeds"),
                    basis_fraction=w.get("known_value_fraction"),
                    checked_at=w.get("stamp"), screens=w.get("screens", []),
                    gaps=w.get("gaps", []),
                    screen_status=w.get("screen_status"),
                    subnets=w.get("subnets", []),
                    positions=w.get("held", []),
                    current_inventory=w.get("current_inventory", False),
                    accounting_source=w.get("accounting_source"),
                    balance_tao=w.get("balance_tao"),
                    balance_fetched_at=w.get("balance_fetched_at"),
                    timing=w.get("timing", {}),
                    examples=w.get("examples", []),
                    buy_rank=ranks.get("buy"), sell_rank=ranks.get("sell"),
                    open_rank=ranks.get("open"),
                    profit_rank=ranks.get("profit"))

    def data(self):
        wallets, stamps, reasons, prefs = self._base()
        keys = sorted(wallets.keys() | reasons.keys() | prefs.keys())
        count = None
        if self.db.is_file():
            with closing(_read_db(self.db)) as db:
                count, = db.execute("SELECT COUNT(*) FROM census_accounts"
                                    ).fetchone()
        rows = [self._wallet(a, wallets, reasons, prefs) for a in keys]
        return dict(wallets=rows, census_rows=count, scan_count=len(stamps))

    def search(self, prefix):
        if not re.fullmatch(r"5[1-9A-HJ-NP-Za-km-z]{2,47}", prefix):
            return []
        wallets, _, reasons, prefs = self._base()
        addresses = {a for a in wallets.keys() | prefs.keys() | reasons.keys()
                     if a.startswith(prefix)}
        if self.db.is_file():
            with closing(_read_db(self.db)) as db:
                addresses.update(a for a, in db.execute(
                    "SELECT DISTINCT address FROM census_accounts WHERE "
                    "address>=? AND address<? ORDER BY address LIMIT 50",
                    (prefix, prefix + "~")))
        if valid_address(prefix):
            addresses.add(prefix)
        return [self._wallet(a, wallets, reasons, prefs)
                for a in sorted(addresses)[:50]]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, body, kind="application/json"):
        if isinstance(body, dict):
            body = json.dumps(body, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _local(self):
        return self.headers.get("Host") == self.server.authority

    def do_GET(self):
        if not self._local():
            return self.reply(403, {"error": "Local access only."})
        url = urlsplit(self.path)
        model = self.server.model
        try:
            if url.path == "/":
                template = Path(__file__).with_name("watchlist_editor.html")
                page = template.read_text(encoding="utf8").replace(
                    "__WATCHLIST_TOKEN__", self.server.token)
                return self.reply(200, page.encode(), "text/html")
            if url.path == "/api/instance":
                return self.reply(200, _instance(model.root))
            if url.path == "/api/watchlist":
                return self.reply(200, model.data())
            if url.path == "/api/groups":
                return self.reply(200, model.groups())
            if url.path == "/api/search":
                q = parse_qs(url.query).get("q", [""])[0].strip()
                return self.reply(200, dict(wallets=model.search(q)))
            if (url.path in ("/accounts.html", "/wallets.html",
                             "/native-holders.html", "/wallets-native.html",
                             "/network.html") or re.fullmatch(
                    r"/[0-9]{1,20}/(?:wallets|accounts)\.html", url.path)):
                # Archived reports use paths relative to their run folder.
                page = model.cache / url.path[1:]
                if page.name == "accounts.html":
                    page = model.cache / "accounts.html"
                if url.path == "/network.html" and not page.is_file():
                    fallback = (model.root / "explorer/network.html",
                                model.cache / "network_from_reports.html")
                    page = next((p for p in fallback if p.is_file()), page)
                if page.is_file():
                    return self.reply(200, page.read_bytes(), "text/html")
            return self.reply(404, {"error": "No saved view at this path."})
        except (OSError, ValueError, sqlite3.Error, KeyError):
            return self.reply(500, {"error": "Cannot read local research."})

    def do_POST(self):
        origin = "http://" + self.server.authority
        if not self._local() or self.headers.get("Origin") != origin:
            return self.reply(403, {"error": "Save from the local editor."})
        if self.path not in ("/api/watchlist", "/api/groups/watch"):
            return self.reply(404, {"error": "Unknown operation."})
        token = self.headers.get("X-Watchlist-Token", "")
        if not secrets.compare_digest(token.encode(),
                                      self.server.token.encode()):
            return self.reply(403, {"error": "Reload the local editor."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            kind = self.headers.get_content_type()
            if not 0 < size <= 16384 or kind != "application/json":
                raise ValueError("Send a watchlist entry as JSON.")
            item = json.loads(self.rfile.read(size).decode("utf8"))
            if self.path == "/api/groups/watch":
                result = self.server.model.watch_group(item)
                return self.reply(200, dict(ok=True, **result))
            save(self.server.model.prefs, item)
            return self.reply(200, {"ok": True})
        except ValueError as exc:
            return self.reply(400, {"error": str(exc)})
        except (OSError, sqlite3.Error):
            return self.reply(500, {"error": "Could not save the watchlist."})


def server(root, port=8749):
    http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    http.authority = "127.0.0.1:%d" % http.server_port
    http.model = Model(root)
    http.token = secrets.token_urlsafe(32)
    return http


def _instance(root):
    path = os.path.normcase(str(root.resolve())).encode("utf8")
    return dict(app="tao-watchlist", root_id=hashlib.sha256(path).hexdigest())


def same_instance(root, port=8749):
    try:
        with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as c:
            c.request("GET", "/api/instance")
            r = c.getresponse()
            return r.status == 200 and json.loads(r.read(1024)) == _instance(
                Path(root))
    except (OSError, ValueError):
        return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--open", action="store_true")
    args = p.parse_args(argv)
    root = Path.cwd()
    url = "http://127.0.0.1:8749/"
    try:
        http = server(root)
    except OSError:
        if same_instance(root):
            if args.open:
                webbrowser.open(url)
            return 0
        print("Port 8749 is already in use. Close the other editor first.",
              file=sys.stderr)
        return 1
    with http:
        if args.open:
            webbrowser.open(url)
        http.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
