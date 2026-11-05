"""Accounting for observed trades, transfers and subnet position snapshots."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
import math


def _chain_order(event):
    numbers = str(event.get("extrinsic", "")).split("-")
    sequence = tuple(int(n) for n in numbers if n.isdigit())
    return (event["time"], sequence,
            int(event.get("event_index") or 0), event["id"])


def net_transfers(events):
    """Count matching in/out observations once per actual movement.

    Stable row IDs deduplicate repeat scans. Distinct equal movements in a
    single extrinsic keep the multiplicity observed on either account.
    """
    groups = defaultdict(lambda: defaultdict(list))
    unique = {e["id"]: e for e in events}
    result = []
    for e in unique.values():
        if e["kind"] not in ("stake_transfer", "tao_transfer"):
            result.append(e)
            continue
        amount = e["tao"] if e["alpha"] is None else e["alpha"]
        key = (e.get("extrinsic", e["id"]), e["kind"], e["src"], e["dst"],
               e["netuid"], amount)
        groups[key][e.get("owner")].append(e)
    for sides in groups.values():
        # Prefer the sender's account if both sides have equal coverage.
        longest = max(sides.items(), key=lambda item: (
            len(item[1]), item[0] == item[1][0]["src"], item[0] or ""))
        result.extend(longest[1])
    return sorted(result, key=_chain_order)


@dataclass
class _Change:
    old: dict
    current: dict
    delta: float


def _unexplained(activity, address, netuid, start, end):
    times = activity[address, netuid]
    return bisect_right(times, start) == bisect_right(times, end)


def _link(src, dst, gain, netuid, certain):
    qty = min(-src.delta, gain)
    when = min(src.current["time"], dst["time"])
    return dict(id="snapshot/%s/%s/%s/%s" % (
        src.current["batch"], src.current["address"], dst["address"], netuid),
        time=when, src=src.current["address"], dst=dst["address"],
        kind="stake_transfer", alpha=qty, netuid=netuid, tao=None,
        evidence="coldkey_swap" if certain else "snapshot_match",
        confidence=1.0 if certain else round(
            0.9 * qty / max(-src.delta, gain), 4))


def _match_changes(changes, netuid, activity, first):
    # A coldkey relationship is recorded by the site. This also permits
    # matching a receiver with no earlier snapshot: only the quantity
    # observed leaving the sender and held by the receiver can be assigned.
    losses = [d for d in changes if d.delta < 0]
    gains = {d.current["address"]: d for d in changes if d.delta > 0}
    proposals = []
    for src in losses:
        old = src.old
        receiver = src.current.get("swap")
        if receiver:
            gain = gains.get(receiver)
            dest = gain.current if gain else first.get(receiver)
            if not dest or dest["batch"] != src.current["batch"]:
                continue
            qty = gain.delta if gain else dest["units"].get(netuid, 0.)
            if qty <= 0 or old["time"] >= dest["time"]:
                continue
            if gain and gain.old["time"] >= src.current["time"]:
                continue
            if _unexplained(activity, receiver, netuid,
                            old["time"], dest["time"]):
                proposals.append(_link(src, dest, qty, netuid, True))
            continue
        for gain in gains.values():
            if (max(old["time"], gain.old["time"])
                    < min(src.current["time"], gain.current["time"])
                    and math.isclose(-src.delta, gain.delta, rel_tol=0.001)):
                proposals.append(_link(src, gain.current, gain.delta,
                                       netuid, False))
    # Only one proposal is permitted at either end. Give a documented swap
    # priority over a coincidental delta in another wallet.
    certain = {p["dst"] for p in proposals if p["evidence"] == "coldkey_swap"}
    proposals = [p for p in proposals if p["dst"] not in certain
                 or p["evidence"] == "coldkey_swap"]
    senders, recipients = defaultdict(int), defaultdict(int)
    for p in proposals:
        senders[p["src"]] += 1
        recipients[p["dst"]] += 1
    return [p for p in proposals
            if senders[p["src"]] == 1 and recipients[p["dst"]] == 1]


def inferred_links(checkpoints, events):
    """Compare deltas within the same observed interval per subnet.

    An observed trade or transfer on either side suppresses inference.
    Unconfirmed matches require both prior positions, intersecting time
    intervals and a unique, closely matched quantity in each direction.
    """
    activity = defaultdict(list)
    for e in events:
        for a in (e["src"], e["dst"]):
            if a and e["netuid"] is not None:
                activity[a, e["netuid"]].append(e["time"])
    for times in activity.values():
        times.sort()
    changes = defaultdict(list)
    previous, first = {}, {}
    for p in sorted(checkpoints, key=lambda x: (x["time"], x["address"])):
        addr = p["address"]
        first.setdefault(addr, p)
        old = previous.get(addr)
        previous[addr] = p
        if not old or old["time"] >= p["time"]:
            continue
        for n in set(old["units"]) | set(p["units"]):
            delta = p["units"].get(n, 0.) - old["units"].get(n, 0.)
            if abs(delta) >= 1e-6 and _unexplained(
                    activity, addr, n, old["time"], p["time"]):
                changes[old["batch"], p["batch"], n].append(
                    _Change(old, p, delta))
    links = []
    for (_, _, netuid), rows in changes.items():
        links.extend(_match_changes(rows, netuid, activity, first))
    return sorted(links, key=_chain_order)


@dataclass
class _Holding:
    alpha: float = 0.
    known: float = 0.
    cost: float = 0.
    probable: float = 0.
    estimated_cost: float = 0.

    def remove(self, qty):
        # Unknown units and both kinds of cost move in the same proportion
        # as the wallet's weighted-average inventory. A sale or transfer
        # larger than the observed balance has unknown cost on the excess.
        share = min(qty / self.alpha, 1.) if self.alpha else 0.
        portion = _Holding(qty, self.known * share, self.cost * share,
                           self.probable * share, self.estimated_cost * share)
        self.alpha = max(0., self.alpha - qty)
        self.known *= 1. - share
        self.cost *= 1. - share
        self.probable *= 1. - share
        self.estimated_cost *= 1. - share
        return portion

    def add(self, part):
        self.alpha += part.alpha
        self.known += part.known
        self.cost += part.cost
        self.probable += part.probable
        self.estimated_cost += part.estimated_cost

    def observe(self, qty):
        if qty < self.alpha:
            self.remove(self.alpha - qty)
        self.alpha = qty

    def as_position(self, addr, netuid):
        priced = self.known + self.probable
        price = None
        if priced and _fully_priced(self.alpha, priced):
            price = round((self.cost + self.estimated_cost) / priced, 12)
        return dict(address=addr, netuid=netuid, alpha=self.alpha,
                    buy_price_tao=price, known_alpha=self.known,
                    known_cost_tao=self.cost, probable_alpha=self.probable,
                    probable_cost_tao=self.estimated_cost,
                    unknown_alpha=max(0., self.alpha - priced))


def _opening_inventory(timeline):
    # Work out the smallest unknown opening stake consistent with both
    # observed movements and the first inventory. This includes unknown
    # stake that was sold or transferred before the first snapshot.
    lots = defaultdict(_Holding)
    net, minimum, seen = defaultdict(float), defaultdict(float), set()
    for _, is_snapshot, e in timeline:
        if is_snapshot:
            addr = e["address"]
            if addr in seen:
                continue
            keys = {(a, n) for a, n in net if a == addr}
            keys.update((addr, n) for n in e["units"])
            for key in keys:
                lots[key].alpha = max(-minimum[key],
                                      e["units"].get(key[1], 0) - net[key])
            seen.add(addr)
        elif e["alpha"] and e["src"] != e["dst"]:
            for addr, sign in ((e["src"], -1), (e["dst"], 1)):
                if addr and addr not in seen:
                    key = addr, e["netuid"]
                    net[key] += sign * e["alpha"]
                    minimum[key] = min(net[key], minimum[key])
    for key in net:
        if key[0] not in seen:
            lots[key].alpha = -minimum[key]
    return lots


def _fully_priced(alpha, known):
    # Allow floating point accumulation, without treating a small unknown
    # fraction of a large holding as a profitable sale.
    return math.isclose(alpha, known, rel_tol=2e-15, abs_tol=1e-13)


def portfolio(events, checkpoints):
    events = sorted(events, key=_chain_order)
    timeline = [(e["time"], False, e) for e in events]
    timeline.extend((p["time"], True, p) for p in checkpoints)
    timeline.sort(key=lambda item: item[:2])
    lots = _opening_inventory(timeline)
    stats = defaultdict(lambda: dict(wins=0, priced_sales=0, unpriced_sales=0,
                                     realized_tao=0.))
    for _, is_snapshot, e in timeline:
        if is_snapshot:
            addr = e["address"]
            nets = set(e["units"]) | {n for a, n in lots if a == addr}
            for n in nets:
                lots[addr, n].observe(e["units"].get(n, 0))
            continue
        qty, n = e["alpha"], e["netuid"]
        if not qty or e["src"] == e["dst"]:
            continue
        moved = _Holding(qty)
        if e["src"]:
            moved = lots[e["src"], n].remove(qty)
        if e["kind"] == "sell":
            s = stats[e["src"]]
            if _fully_priced(qty, moved.known):
                pnl = e["tao"] - moved.cost
                s["realized_tao"] += pnl
                s["priced_sales"] += 1
                s["wins"] += pnl > 1e-12
            else:
                s["unpriced_sales"] += 1
        elif e["dst"]:
            if e["kind"] == "buy":
                moved = _Holding(qty, qty, e["tao"])
            elif e["evidence"] == "snapshot_match":
                moved.probable += moved.known
                moved.estimated_cost += moved.cost
                moved.known = moved.cost = 0.
            lots[e["dst"], n].add(moved)
    positions = {key: lot.as_position(*key) for key, lot in lots.items()
                 if lot.alpha > 1e-12}
    for s in stats.values():
        s["realized_tao"] = round(s["realized_tao"], 9)
    return positions, stats


def timing(events, prices):
    """First observed subnet quote 24 to 30 hours after each trade."""
    quotes = defaultdict(list)
    for p in prices:
        if p["price_tao"] and p["price_tao"] > 0:
            quotes[p["netuid"]].append((
                datetime.fromisoformat(p["fetched_at"]), p["price_tao"]))
    for rows in quotes.values():
        rows.sort()
    result = defaultdict(lambda: {"evaluated": 0, "favorable": 0})
    for e in events:
        if e["kind"] not in ("buy", "sell") or not e["alpha"] or not e["tao"]:
            continue
        start = datetime.fromisoformat(e["time"]) + timedelta(hours=24)
        rows = quotes[e["netuid"]]
        i = bisect_left(rows, (start,))
        if i == len(rows) or rows[i][0] > start + timedelta(hours=6):
            continue
        p = e["tao"] / e["alpha"]
        s = result[e["src"] or e["dst"]]
        s["evaluated"] += 1
        s["favorable"] += rows[i][1] > p if e["kind"] == "buy" else (
            rows[i][1] < p)
    return result
