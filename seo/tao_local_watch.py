"""Monitor finalized Bittensor events for watched coldkeys on this PC."""
import argparse
from collections import defaultdict
from contextlib import closing, suppress
from datetime import datetime, timezone
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit
import warnings


HOST = "api-bittensor-mainnet.n.dwellir.com"
INTERVAL = 300
RPC_PER_SECOND = 10
KEEP = 500
SOURCE = "local-finney-events-v1"
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ADDRESS = re.compile(r"5[1-9A-HJ-NP-Za-km-z]{45,47}")


class WatchError(Exception):
    """A local, safe-to-print configuration or state error."""


class RequestBudget(Exception):
    pass


def _endpoint(prompt):
    url = os.environ.get("DWELLIR_WS_URL")
    if url:
        try:
            parts = urlsplit(url)
            if (parts.scheme == "wss" and parts.hostname == HOST
                    and parts.port in (None, 443) and not parts.username
                    and not parts.password and len(parts.path) > 1
                    and not parts.query and not parts.fragment):
                return url
        except ValueError:
            pass
        raise WatchError("DWELLIR_WS_URL must be Dwellir's Bittensor mainnet "
                         "WSS endpoint with the key in its path.")
    key = os.environ.get("DWELLIR_API_KEY")
    if not key and prompt and sys.stdin.isatty() and sys.stderr.isatty():
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                key = getpass.getpass("Dwellir API key (hidden): ")
            except (getpass.GetPassWarning, EOFError):
                raise WatchError("A masked interactive console is required.")
    if not key:
        raise WatchError("Set DWELLIR_API_KEY locally or run with --prompt "
                         "in an interactive console.")
    return "wss://%s/%s" % (HOST, quote(key.strip(), safe=""))


def _connect(url, budget):
    try:
        from substrateinterface import SubstrateInterface
    except ImportError:
        raise WatchError("Install the local dependency first with "
                         "watch_wallets.cmd --setup or pip install "
                         "substrate-interface==1.8.1.")

    class MeteredClient(SubstrateInterface):
        requests = 0
        last_request = 0.

        def rpc_request(self, method, params, result_handler=None):
            if self.requests >= budget:
                raise RequestBudget
            wait = self.last_request + 1 / RPC_PER_SECOND - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            self.requests += 1
            return super().rpc_request(method, params, result_handler)

        def get_events(self, block_hash):
            obj = self.query("System", "Events", block_hash=block_hash)
            if obj is None or (getattr(obj, "meta_info", None)
                               or {}).get("result_found") is False:
                raise ValueError("finalized events unavailable")
            return obj.elements

    return MeteredClient(url=url, ss58_format=42, auto_reconnect=False,
                         auto_discover=False, use_remote_preset=False,
                         ws_options={"timeout": 25})


def _scalar(v):
    if isinstance(v, dict) and "value" in v:
        return _scalar(v["value"])
    if isinstance(v, (list, tuple)) and len(v) == 1:
        return _scalar(v[0])
    return v


def _addr(v):
    v = _scalar(v)
    if isinstance(v, dict):
        v = v.get("Id") or v.get("ss58")
    if isinstance(v, bytes):
        v = "0x" + v.hex()
    if isinstance(v, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", v):
        body = bytes([42]) + bytes.fromhex(v[2:])
        chk = hashlib.blake2b(b"SS58PRE" + body).digest()[:2]
        num = int.from_bytes(body + chk, "big")
        encoded = ""
        while num:
            num, rem = divmod(num, 58)
            encoded = ALPHABET[rem] + encoded
        return encoded
    if isinstance(v, str) and ADDRESS.fullmatch(v):
        return v
    raise ValueError("unrecognized AccountId")


def _args(attrs, names):
    if isinstance(attrs, dict):
        return [_scalar(attrs[k]) for k in names]
    if isinstance(attrs, (tuple, list)) and len(attrs) >= len(names):
        return [_scalar(v) for v in attrs[:len(names)]]
    raise ValueError("unrecognized runtime event layout")


def _units(v):
    if isinstance(v, bool) or not isinstance(v, (int, str)) or (
            not str(v).isdigit()):
        raise ValueError("invalid base units")
    return int(v)


def _event(height, i, ext, mod, name, attrs):
    src = dst = hot = dest_hot = n = dest_n = alpha = tao = None
    if mod == "Balances" and name == "Transfer":
        src, dst, tao = _args(attrs, ("from", "to", "amount"))
        kind = "tao_transfer"
    elif mod == "SubtensorModule" and name in ("StakeAdded", "StakeRemoved"):
        owner, hot, tao, alpha, n = _args(
            attrs, ("coldkey", "hotkey", "tao", "alpha", "netuid"))
        if name == "StakeAdded":
            kind, dst = "stake_increase", owner
        else:
            kind, src = "stake_decrease", owner
    elif mod == "SubtensorModule" and name in (
            "StakeTransferred", "StakeAndHotkeyTransferred"):
        if name == "StakeTransferred":
            src, dst, hot, n, dest_n, tao = _args(attrs, (
                "origin_coldkey", "destination_coldkey", "hotkey",
                "origin_netuid", "destination_netuid", "amount"))
            dest_hot = hot
        else:
            src, dst, hot, dest_hot, n, dest_n, tao = _args(attrs, (
                "origin_coldkey", "destination_coldkey", "origin_hotkey",
                "destination_hotkey", "origin_netuid", "destination_netuid",
                "amount"))
        kind = "stake_transfer"
    elif mod == "SubtensorModule" and name in ("StakeMoved", "StakeSwapped"):
        if name == "StakeMoved":
            src, hot, n, dest_hot, dest_n, tao = _args(attrs, (
                "coldkey", "origin_hotkey", "origin_netuid",
                "destination_hotkey", "destination_netuid", "amount"))
        else:
            src, hot, n, dest_n, tao = _args(attrs, (
                "coldkey", "hotkey", "origin_netuid",
                "destination_netuid", "amount"))
            dest_hot = hot
        kind, dst = "stake_transfer", src
    else:
        return None
    alpha = _units(alpha) if alpha is not None else None
    tao = _units(tao) if tao is not None else None
    return dict(id=f"finney:{height}:{i}", block_number=height, event_index=i,
                extrinsic=f"{height}-{ext:04d}" if ext is not None else None,
                event_name=f"{mod}.{name}", kind=kind,
                src=_addr(src) if src else None, dst=_addr(dst) if dst
                else None, hotkey=_addr(hot) if hot else None,
                dst_hotkey=_addr(dest_hot) if dest_hot else None,
                netuid=_units(n) if n is not None else None,
                dst_netuid=_units(dest_n) if dest_n is not None else None,
                tao=tao / 1e9 if tao is not None else None,
                tao_rao=str(tao) if tao is not None else None,
                alpha=alpha / 1e9 if alpha is not None else None,
                alpha_units=str(alpha) if alpha is not None else None)


def block_events(height, records, watched):
    failed = set()
    rows = []
    for i, obj in enumerate(records):
        rec = getattr(obj, "value", obj)
        mod, name = rec["module_id"], rec["event_id"]
        ext = rec.get("extrinsic_idx")
        if mod == "System" and name == "ExtrinsicFailed":
            failed.add(ext)
        row = _event(height, i, ext, mod, name,
                     rec.get("attributes", rec.get("params")))
        if row:
            rows.append((ext, row))
    groups = defaultdict(list)
    for ext, r in rows:
        if ext not in failed:
            groups[ext].append(r)
    found = []
    for ext, records in groups.items():
        used = set()
        for move in records:
            if move["kind"] != "stake_transfer" or ext is None:
                continue
            # The summary amount is TAO equivalent. Only a unique pair of
            # corroborating stake legs establishes quantities on both subnets.
            outgoing = [r for r in records if r["kind"] == "stake_decrease"
                        and r["src"] == move["src"]
                        and r["hotkey"] == move["hotkey"]
                        and r["netuid"] == move["netuid"]]
            incoming = [r for r in records if r["kind"] == "stake_increase"
                        and r["dst"] == move["dst"]
                        and r["hotkey"] == move["dst_hotkey"]
                        and r["netuid"] == move["dst_netuid"]]
            if len(outgoing) == len(incoming) == 1 and (
                    outgoing[0]["event_index"] < incoming[0]["event_index"]
                    < move["event_index"]) and not (
                        {outgoing[0]["id"], incoming[0]["id"]} & used):
                move["alpha"] = outgoing[0]["alpha"]
                move["alpha_units"] = outgoing[0]["alpha_units"]
                move["dst_alpha"] = incoming[0]["alpha"]
                move["dst_alpha_units"] = incoming[0]["alpha_units"]
                used.update([outgoing[0]["id"], incoming[0]["id"]])
        found.extend(r for r in records if r["id"] not in used
                     and (r["src"] in watched or r["dst"] in watched))
    return found


def _now():
    return datetime.now(timezone.utc).isoformat()


def poll(chain, old, addresses, max_blocks=40, new_events=None):
    addresses = sorted(set(addresses))
    feed = dict(old, source=SOURCE, addresses=addresses, checked_at=_now(),
                poll_interval_seconds=INTERVAL, stale_after_seconds=660,
                status="current")
    previous = old.get("last_scanned_block")
    events = [e for e in old.get("events", [])
              if e["src"] in addresses or e["dst"] in addresses]
    feed["events"] = events
    try:
        genesis = chain.get_block_hash(0)
        finalized = chain.get_chain_finalised_head()
        head = chain.get_block_number(finalized)
        if not genesis or not finalized or not isinstance(head, int):
            raise ValueError("finalized head unavailable")
        if old.get("genesis_hash", genesis) != genesis:
            raise WatchError("The saved feed belongs to a different chain.")
        if previous is not None and head < previous:
            raise ValueError("endpoint lags saved checkpoint")
        start = (old.get("coverage_start", head) if previous is None
                 else previous + 1)
        feed.update(head=head, genesis_hash=genesis,
                    coverage_start=old.get("coverage_start", start))
        starts = old.get("address_coverage_start", {})
        feed["address_coverage_start"] = {
            a: starts.get(a, start) for a in addresses}
        for height in range(start, min(head + 1, start + max_blocks)):
            block_hash = finalized if height == head else (
                chain.get_block_hash(height))
            if not block_hash:
                raise ValueError("finalized block unavailable")
            matches = block_events(height, chain.get_events(block_hash),
                                   addresses)
            if matches:
                ms = chain.query("Timestamp", "Now",
                                 block_hash=block_hash).value
                when = datetime.fromtimestamp(int(ms) / 1000,
                                              timezone.utc).isoformat()
                for row in matches:
                    row["time"] = when
                events.extend(matches)
                if new_events is not None:
                    new_events.extend(matches)
            previous = height
        if previous != head:
            feed["status"] = "catching_up"
    except RequestBudget:
        feed["status"] = "rpc_budget"
    except WatchError:
        raise
    except Exception:
        # RPC exceptions may contain the URL including its private key.
        feed["status"] = "read_error"
    events.sort(key=lambda e: (e["block_number"], e["event_index"]),
                reverse=True)
    feed["discarded_events"] = old.get("discarded_events", 0) + max(
        0, len(events) - KEEP)
    feed["events"] = events[:KEEP]
    feed["retained_events"] = len(feed["events"])
    feed["last_scanned_block"] = previous
    feed["lag_blocks"] = (feed["head"] - previous if previous is not None
                          and feed.get("head") is not None else None)
    feed["rpc_calls"] = chain.requests
    return feed


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf8", delete=False,
                                     dir=path.parent) as f:
        tmp = Path(f.name)
        try:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
            f.close()
            tmp.replace(path)
        finally:
            with suppress(FileNotFoundError):
                tmp.unlink()


def _report_path(cache):
    for folder in sorted((p for p in cache.glob("*") if p.is_dir()
                          and p.name.isdecimal()), key=lambda p: int(p.name),
                         reverse=True):
        path = next(folder.rglob("tao_collect.json"), None)
        if path:
            return path
    raise WatchError("No cached wallet report found. Run "
                     "view_wallets_github.cmd or pass --report.")


def _addresses(report, explicit):
    blocked = {w["address"] for w in report.get("performance", {}).get(
        "wallets", []) if w.get("screens")}
    try:
        chosen = [_addr(a) for a in report.get("following", [])
                  if a not in blocked] + [_addr(a) for a in explicit]
    except (TypeError, ValueError):
        raise WatchError("Use full Finney coldkey addresses.")
    if not chosen:
        raise WatchError("This report has no eligible followed wallets. "
                         "Supply --address with the wallet to monitor.")
    return sorted(set(chosen))


def _persist(args, feed, report):
    _write(args.out, json.dumps(feed, indent=2, allow_nan=False))
    if report:
        from seo.tao_view import render
        _write(args.view, render(report, feed))


def _notice(event):
    peers = [a[:6] + "..." + a[-4:] for a in (event["src"], event["dst"]) if a]
    if event["kind"] == "tao_transfer":
        value = f"{event['tao']:.9g} TAO"
    else:
        qty = event["alpha"]
        value = (f"SN{event['netuid']} {qty:.9g} alpha" if qty is not None
                 else f"SN{event['netuid']} (alpha unavailable)")
        if event["dst_netuid"] not in (None, event["netuid"]):
            value += f" to SN{event['dst_netuid']}"
    return (f"Block {event['block_number']}  {event['kind']}  "
            f"{' -> '.join(peers)}  {value}")


def _run(args):
    if args.cached_report:
        args.report = _report_path(Path("github_out"))
    paths = [args.out, args.view] + ([args.report] if args.report else [])
    if len({p.resolve() for p in paths}) != len(paths):
        raise WatchError("Use separate paths for the report, feed and view.")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(args.out) + ".lock", timeout=0)) as lock:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            raise WatchError("Another watcher already uses this feed.")
        feed = {}
        if args.out.exists():
            feed = json.loads(args.out.read_text(encoding="utf8"))
            if feed.get("source") != SOURCE:
                raise WatchError("The output already exists and is not a "
                                 "local watcher feed. Choose a new path.")
        report = (json.loads(args.report.read_text(encoding="utf8"))
                  if args.report else {})
        addresses = _addresses(report, args.address)
        url = _endpoint(args.prompt)
        # Remote errors/log lines can contain the key-bearing endpoint.
        level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        chain = None
        try:
            while True:
                start = time.monotonic()
                notices = []
                if args.report:
                    if args.cached_report:
                        args.report = _report_path(Path("github_out"))
                    report = json.loads(args.report.read_text(encoding="utf8"))
                    addresses = _addresses(report, args.address)
                try:
                    if chain is None:
                        chain = _connect(url, args.rpc_limit)
                    else:
                        chain.requests = 0
                    feed = poll(chain, feed, addresses, args.max_blocks,
                                notices)
                except WatchError:
                    raise
                except Exception:
                    feed = dict(feed, source=SOURCE, status="read_error",
                                checked_at=_now(), addresses=addresses,
                                events=feed.get("events", []),
                                poll_interval_seconds=INTERVAL,
                                stale_after_seconds=660)
                feed["rpc_limit"] = args.rpc_limit
                feed["rpc_rate_limit"] = RPC_PER_SECOND
                _persist(args, feed, report)
                for row in notices:
                    print(_notice(row), flush=True)
                print("Local watcher %s. Finalized checkpoint %s, lag %s, "
                      "RPC requests %s/%d." % (
                          feed["status"], feed.get("last_scanned_block"),
                          feed.get("lag_blocks"), feed.get("rpc_calls"),
                          args.rpc_limit), flush=True)
                error = feed["status"] == "read_error"
                if args.once:
                    return int(error)
                if error and chain is not None:
                    with suppress(Exception):
                        chain.close()
                    chain = None
                time.sleep(max(0., INTERVAL - (time.monotonic() - start)))
        except KeyboardInterrupt:
            if feed:
                feed.update(status="stopped", checked_at=_now())
                _persist(args, feed, report)
            return 130
        finally:
            if chain is not None:
                with suppress(Exception):
                    chain.close()
            logging.disable(level)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report", type=Path, help="GitHub collector JSON")
    p.add_argument("--cached-report", action="store_true",
                   help="use the newest cached report unless --report is set")
    p.add_argument("--address", action="append", default=[], metavar="WALLET")
    p.add_argument("--out", type=Path,
                   default=Path("try_tao_out/local_wallet_activity.json"))
    p.add_argument("--view", type=Path,
                   default=Path("github_out/wallets.html"))
    p.add_argument("--max-blocks", type=int, default=40)
    p.add_argument("--rpc-limit", type=int, default=180,
                   help="maximum RPC requests per five-minute poll")
    p.add_argument("--once", action="store_true", help="run one poll and exit")
    p.add_argument("--prompt", action="store_true",
                   help="ask for a masked key if it is missing locally")
    args = p.parse_args(argv)
    if args.report:
        args.cached_report = False
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        print("The Dwellir watcher runs only on a local PC.", file=sys.stderr)
        return 2
    if not 1 <= args.max_blocks <= 200 or not 10 <= args.rpc_limit <= 300:
        p.error("use 1-200 blocks and 10-300 requests per poll")
    try:
        return _run(args)
    except WatchError as e:
        print(e, file=sys.stderr)
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
        print("Could not read or write the local report/feed.",
              file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
