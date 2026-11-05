"""Audit one bounded window of finalized Finney asset events on a local PC."""

import argparse
from collections import Counter
from contextlib import closing, suppress
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys

from seo import tao_local_watch as watch


SOURCE = "local-finney-window-v1"


def _decoded(height, records):
    records = [getattr(rec, "value", rec) for rec in records]
    failed = {r.get("extrinsic_idx") for r in records
              if (r["module_id"], r["event_id"])
              == ("System", "ExtrinsicFailed")}
    raw = []
    for i, r in enumerate(records):
        ext = r.get("extrinsic_idx")
        if ext in failed:
            continue
        row = watch._event(height, i, ext, r["module_id"], r["event_id"],
                           r.get("attributes", r.get("params")))
        if row:
            raw.append(row)
    return raw, watch.block_events(height, records, None)


def _summary(result):
    rows = result["movements"]
    edges = [r for r in rows if r["kind"] in {"stake_transfer", "tao_transfer"}
             and r["src"] and r["dst"] and r["src"] != r["dst"]]
    result["transfer_edges"] = edges
    result["counts"] = dict(
        finalized_blocks=len(result["scanned_blocks"]),
        raw_native_events=sum(b["runtime_events"]
                              for b in result["scanned_blocks"]),
        raw_asset_events=len(result["raw_asset_events"]), movements=len(rows),
        by_kind=dict(Counter(r["kind"] for r in rows)),
        cross_wallet_edges=len(edges),
        distinct_transfer_addresses=len({a for r in edges
                                         for a in (r["src"], r["dst"])}),
        same_wallet_stake_moves=sum(r["kind"] == "stake_transfer"
                                    and r["src"] == r["dst"] for r in rows))


def scan_window(chain, *, blocks=24, end_block=None, old=None,
                checkpoint=None):
    """Use block-level RPC calls only. Commit each completely decoded block."""
    if not 1 <= blocks <= 60 or end_block is not None and end_block < 0:
        raise watch.WatchError("Use 1–60 blocks and a nonnegative end block.")
    if old and old.get("source") != SOURCE:
        raise watch.WatchError("The output belongs to another data source.")
    result = old or dict(
        source=SOURCE, requested_blocks=blocks, first_block=None,
        last_block=None, last_scanned_block=None, scanned_blocks=[],
        movements=[], raw_asset_events=[], rpc_total=0,
        interpretation="Edges identify native transfers between addresses. "
        "Stake changes alone do not establish a trade, owner or profit.")
    if (result["requested_blocks"] != blocks or end_block is not None
            and result["last_block"] not in (None, end_block)):
        raise watch.WatchError("The output already tracks another window. "
                               "Choose a new file to start a different one.")
    previous_requests = result.get("rpc_total", 0)

    def save(status):
        result.update(status=status, checked_at=watch._now(),
                      rpc_calls=chain.requests,
                      rpc_total=previous_requests + chain.requests)
        _summary(result)
        if checkpoint:
            checkpoint(result)

    phase = "reading finalized head"
    try:
        genesis = chain.get_block_hash(0)
        final_hash = chain.get_chain_finalised_head()
        head = chain.get_block_number(final_hash)
        if not genesis or not final_hash or type(head) is not int or head < 0:
            raise ValueError
        if result.get("genesis_hash", genesis) != genesis:
            raise watch.WatchError("Checkpoint belongs to another chain.")
        if result["first_block"] is None:
            end = head if end_block is None else end_block
            if end > head:
                raise watch.WatchError("End block exceeds the finalized head.")
            result.update(first_block=max(0, end - blocks + 1), last_block=end,
                          genesis_hash=genesis, head_at_start=head)
        if head < result["last_block"]:
            raise watch.WatchError("Finalized head is behind this window.")
        result["finalized_head"] = head
        result.pop("incomplete_step", None)
        save("scanning")
        previous = result["last_scanned_block"]
        start = result["first_block"] if previous is None else previous + 1
        for height in range(start, result["last_block"] + 1):
            phase = f"reading hash for block {height}"
            block_hash = final_hash if head == height else (
                chain.get_block_hash(height))
            if not block_hash:
                raise ValueError
            phase = f"reading System.Events for block {height}"
            records = list(chain.get_events(block_hash))
            phase = f"decoding runtime events for block {height}"
            raw, movements = _decoded(height, records)
            result["raw_asset_events"].extend(raw)
            result["movements"].extend(movements)
            result["scanned_blocks"].append(dict(
                height=height, block_hash=block_hash,
                runtime_events=len(records), asset_events=len(raw)))
            result["last_scanned_block"] = height
            save("scanning")
    except watch.RequestBudget:
        result["incomplete_step"] = phase
        save("rpc_budget")
    except watch.WatchError:
        raise
    except Exception:
        # Transport exceptions can contain the endpoint and credential.
        result["incomplete_step"] = phase
        save("read_error")
    else:
        save("complete")
    return result


def _run(args):
    path = args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path) + ".lock", timeout=0)) as lock:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.Error:
            raise watch.WatchError("Another local audit is writing this file.")
        old = json.loads(path.read_text("utf-8")) if path.exists() else None
        if old is not None and (not isinstance(old, dict)
                                or old.get("source") != SOURCE):
            raise watch.WatchError("Choose a new file for the chain window.")
        if old and old.get("status") == "complete":
            if (old.get("requested_blocks") != args.blocks
                    or args.end_block not in (None, old.get("last_block"))):
                raise watch.WatchError("Choose a new file for another window.")
            print("Window already complete. New RPC requests: 0.")
            return 0
        level = logging.root.manager.disable
        chain = None
        logging.disable(logging.CRITICAL)
        try:
            chain = watch._connect(watch._endpoint(args.prompt),
                                   args.rpc_limit)
            result = scan_window(
                chain, blocks=args.blocks, end_block=args.end_block, old=old,
                checkpoint=lambda r: watch._write(path, json.dumps(r)))
        except watch.WatchError:
            raise
        except Exception:
            raise watch.WatchError("Could not open the Finney audit. Check "
                                   "access and the output path.") from None
        finally:
            if chain is not None:
                with suppress(Exception):
                    chain.close()
            logging.disable(level)
    print(f"{result['status']}: {result['counts']['finalized_blocks']} "
          f"blocks, {result['counts']['movements']} movements, "
          f"{result['counts']['cross_wallet_edges']} cross-address transfers, "
          f"{result['rpc_calls']}/{args.rpc_limit} RPC requests. {path}")
    return int(result["status"] != "complete")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--blocks", type=int, default=24, help="1–60 (default 24)")
    p.add_argument("--end-block", type=int, help="inclusive finalized height; "
                   "defaults to the current finalized head")
    p.add_argument("--rpc-limit", type=int, default=180,
                   help="hard limit per invocation, up to 200")
    p.add_argument("--out", type=Path,
                   default=Path("try_tao_out/native_window.json"),
                   help="new window or previously interrupted checkpoint")
    p.add_argument("--prompt", action="store_true",
                   help="prompt locally for a masked key if needed")
    args = p.parse_args(argv)
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        print("This Finney audit runs only on a local PC.", file=sys.stderr)
        return 2
    if (not 1 <= args.blocks <= 60 or not 1 <= args.rpc_limit <= 200
            or args.end_block is not None and args.end_block < 0):
        p.error("use 1–60 blocks, 1–200 RPC requests and a finalized height")
    try:
        return _run(args)
    except watch.WatchError as exc:
        print(exc, file=sys.stderr)
    except (ValueError, OSError, sqlite3.Error):
        print("Could not read/write the local audit file.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
