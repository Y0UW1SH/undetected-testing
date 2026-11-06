"""Accounting for observed trades, transfers and subnet position snapshots."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta, timezone
import math

from seo.tao_wallets import _parse_ts


def _chain_order(event):
    numbers = str(event.get("extrinsic", "")).split("-")
    sequence = tuple(int(n) for n in numbers if n.isdigit())
    if event["kind"] == "subnet_liquidation":
        # Automatic subnet settlement runs at block finalization, after
        # that block's extrinsics; it has no user extrinsic of its own.
        sequence = (event.get("block_number") or math.inf, math.inf)
    return (_parse_ts(event["time"]), sequence,
            int(event.get("event_index") or 0), event["id"])


def _stake_side(event):
    side = event.get("transfer_side")
    if side:
        return side
    owner = event.get("owner")
    if owner is None:
        return "both"  # An already-normalized full movement.
    if event["src"] != event["dst"]:
        return "out" if owner == event["src"] else "in"
    return "unknown"


def _stake_legs(rows):
    """A wallet's table describes only its own subnet and quantity. Carry
    basis only when the opposite leg corroborates the same units and asset.
    A conversion keeps independent observed debits and credits."""
    sides = defaultdict(list)
    for row in sorted(rows, key=_chain_order):
        sides[_stake_side(row)].append(row)
    incoming, outgoing = sides["in"], sides["out"]
    pairs = min(len(incoming), len(outgoing))
    matched = [dict(src, transfer_side="both", paired_event_id=dst["id"])
               for src, dst in zip(outgoing, incoming)]
    return (matched + [dict(e, transfer_side="out") for e in outgoing[pairs:]]
            + [dict(e, transfer_side="in") for e in incoming[pairs:]]
            + sides["both"] + [dict(e, transfer_side="unknown")
                               for e in sides["unknown"]])


def _inventory_endpoints(event):
    side = event.get("transfer_side")
    if side == "out":
        return event["src"], None
    if side == "in":
        return None, event["dst"]
    if side == "unknown":
        return None, None
    return event["src"], event["dst"]


def _reallocation_pairs(events):
    # Moving stake to another validator can appear in the Taostats table as
    # an ordinary sell immediately followed by an ordinary buy. Require the
    # same cash and consecutive chain-event indices. A one-nano token loss
    # is a candidate only: it must be confirmed by the native receipt.
    fills = sorted((e for e in events if e["kind"] in ("sell", "buy")),
                   key=_chain_order)
    for sell, buy in zip(fills, fills[1:]):
        if (sell["kind"] == "sell" and buy["kind"] == "buy"
                and sell["src"] == buy["dst"]
                and _parse_ts(sell["time"]) == _parse_ts(buy["time"])
                and sell.get("extrinsic")
                and sell["extrinsic"] == buy.get("extrinsic")
                and sell["netuid"] == buy["netuid"]
                and sell["alpha"] is not None and buy["alpha"] is not None
                and 0 <= sell["alpha"] - buy["alpha"] <= 1.1e-9
                and sell["tao"] == buy["tao"]
                and isinstance(sell.get("event_index"), int)
                and buy.get("event_index") == sell["event_index"] + 1):
            yield sell, buy


def matching_reallocation_ids(events):
    """Native extrinsics worth checking before trusting a same-wallet move."""
    unique = {e["id"]: e for e in events}
    return {sell["extrinsic"] for sell, _ in _reallocation_pairs(
        unique.values())}


def net_transfers(events, *, verified_moves=()):
    """Count matching in/out observations once per actual movement.

    Stable row IDs deduplicate repeat scans. Distinct equal movements in a
    single extrinsic keep the multiplicity observed on either account.
    """
    groups = defaultdict(list)
    unique = {e["id"]: e for e in events}
    for sell, buy in _reallocation_pairs(unique.values()):
        verified = sell["extrinsic"] in verified_moves
        loss = sell["alpha"] - buy["alpha"]
        if loss and not verified:
            continue
        unique[sell["id"]] = dict(
            sell, kind="stake_reallocation", dst=buy["dst"],
            received_alpha=buy["alpha"], rounding_loss_alpha=loss,
            paired_event_id=buy["id"], evidence=(
                "native_move" if verified else "matching_trade_legs"))
        del unique[buy["id"]]
    result = []
    for e in unique.values():
        if e["kind"] not in ("stake_transfer", "tao_transfer"):
            result.append(e)
            continue
        amount = e["tao"] if e["alpha"] is None else e["alpha"]
        key = (e.get("extrinsic", e["id"]), e["kind"], e["src"], e["dst"],
               e["netuid"], amount)
        groups[key].append(e)
    for rows in groups.values():
        if rows[0]["kind"] == "stake_transfer":
            result.extend(_stake_legs(rows))
            continue
        sides = defaultdict(list)
        for e in rows:
            sides[e.get("owner")].append(e)
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
    when = min(src.current["time"], dst["time"]).isoformat()
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
    Scoped checkpoints are not complete account inventories and are ignored.
    """
    activity = defaultdict(list)
    for e in events:
        for a in (e["src"], e["dst"]):
            if a and e["netuid"] is not None:
                activity[a, e["netuid"]].append(_parse_ts(e["time"]))
    for times in activity.values():
        times.sort()
    changes = defaultdict(list)
    previous, first = {}, {}
    dated = (dict(p, time=_parse_ts(p["time"])) for p in checkpoints
             if "netuid_scope" not in p)
    for p in sorted(dated, key=lambda x: (x["time"], x["address"])):
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


@dataclass(frozen=True)
class _Purchase:
    address: str
    time: str
    event_id: str
    extrinsic: str
    unit_price: float


def _origin_order(item):
    (purchase, estimated), qty = item
    return -qty, purchase.time, purchase.event_id, estimated


@dataclass
class _Holding:
    alpha: float = 0.
    known: float = 0.
    cost: float = 0.
    probable: float = 0.
    estimated_cost: float = 0.
    # Sampled purchases explain basis without changing the aggregate ledger.
    # Key is (original buy, basis estimated somewhere along its path).
    origins: dict = field(default_factory=dict)
    # Growth is covered by its source's basis provenance, but is not a buy.
    growth: float = 0.
    known_growth: float = 0.
    probable_growth: float = 0.
    origin_growth: dict = field(default_factory=dict)
    # Paid units lost to a verified native rounding adjustment retain cost.
    known_rounding: float = 0.
    probable_rounding: float = 0.
    origin_rounding: dict = field(default_factory=dict)

    def remove(self, qty):
        # Unknown units and both kinds of cost move in the same proportion
        # as the wallet's weighted-average inventory. A sale or transfer
        # larger than the observed balance has unknown cost on the excess.
        share = min(qty / self.alpha, 1.) if self.alpha else 0.
        portion = _Holding(qty, self.known * share, self.cost * share,
                           self.probable * share, self.estimated_cost * share,
                           growth=self.growth * share,
                           known_growth=self.known_growth * share,
                           probable_growth=self.probable_growth * share,
                           known_rounding=self.known_rounding * share,
                           probable_rounding=self.probable_rounding * share)
        for origin, units in self.origins.items():
            portion.origins[origin] = units * share
        portion.origin_growth = {
            origin: units * share
            for origin, units in self.origin_growth.items()}
        portion.origin_rounding = {
            origin: units * share
            for origin, units in self.origin_rounding.items()}
        self.origins = {origin: units * (1. - share)
                        for origin, units in self.origins.items() if share < 1}
        self.origin_growth = {origin: units * (1. - share)
                              for origin, units in self.origin_growth.items()
                              if share < 1}
        self.origin_rounding = {
            origin: units * (1. - share)
            for origin, units in self.origin_rounding.items() if share < 1}
        self.alpha = max(0., self.alpha - qty)
        self.known *= 1. - share
        self.cost *= 1. - share
        self.probable *= 1. - share
        self.estimated_cost *= 1. - share
        self.growth *= 1. - share
        self.known_growth *= 1. - share
        self.probable_growth *= 1. - share
        self.known_rounding *= 1. - share
        self.probable_rounding *= 1. - share
        return portion

    def add(self, part):
        self.alpha += part.alpha
        self.known += part.known
        self.cost += part.cost
        self.probable += part.probable
        self.estimated_cost += part.estimated_cost
        self.growth += part.growth
        self.known_growth += part.known_growth
        self.probable_growth += part.probable_growth
        self.known_rounding += part.known_rounding
        self.probable_rounding += part.probable_rounding
        for key, qty in part.origins.items():
            self.origins[key] = self.origins.get(key, 0.) + qty
        for key, qty in part.origin_growth.items():
            self.origin_growth[key] = self.origin_growth.get(key, 0.) + qty
        for key, qty in part.origin_rounding.items():
            self.origin_rounding[key] = self.origin_rounding.get(key, 0.) + qty
        if len(self.origins) > 5:
            self.origins = dict(sorted(self.origins.items(),
                                       key=_origin_order)[:5])
            self.origin_growth = {
                key: qty for key, qty in self.origin_growth.items()
                if key in self.origins}
            self.origin_rounding = {
                key: qty for key, qty in self.origin_rounding.items()
                if key in self.origins}

    def lose_rounding_units(self, qty):
        """A verified native loss changes units, never acquisition cost."""
        share = qty / self.alpha
        self.known_rounding += (self.known - self.known_growth) * share
        self.probable_rounding += (
            self.probable - self.probable_growth) * share
        for origin, purchased in self.origins.items():
            lost = self.origin_rounding.get(origin, 0.)
            self.origin_rounding[origin] = lost + (purchased - lost) * share
        self.origin_growth = {key: qty * (1. - share)
                              for key, qty in self.origin_growth.items()}
        self.alpha -= qty
        self.known *= 1. - share
        self.probable *= 1. - share
        self.growth *= 1. - share
        self.known_growth *= 1. - share
        self.probable_growth *= 1. - share

    def grow(self, qty):
        """Expand proven units without manufacturing a purchase or cost."""
        delta = qty - self.alpha
        known_delta = self.known / self.alpha * delta
        probable_delta = self.probable / self.alpha * delta
        self.known_growth += known_delta
        self.probable_growth += probable_delta
        self.known += known_delta
        self.probable += probable_delta
        for origin, purchased in self.origins.items():
            earned = self.origin_growth.get(origin, 0.)
            current = purchased + earned - self.origin_rounding.get(origin, 0.)
            self.origin_growth[origin] = (
                earned + current / self.alpha * delta)
        self.growth += delta
        self.alpha = qty

    def observe(self, qty):
        if qty < self.alpha:
            self.remove(self.alpha - qty)
        self.alpha = qty

    def mark_probable(self):
        self.probable += self.known
        self.estimated_cost += self.cost
        self.known = self.cost = 0.
        self.probable_growth += self.known_growth
        self.known_growth = 0.
        self.probable_rounding += self.known_rounding
        self.known_rounding = 0.
        origins = defaultdict(float)
        earned = defaultdict(float)
        rounded = defaultdict(float)
        for (purchase, _), qty in self.origins.items():
            origins[purchase, True] += qty
        for (purchase, _), qty in self.origin_growth.items():
            earned[purchase, True] += qty
        for (purchase, _), qty in self.origin_rounding.items():
            rounded[purchase, True] += qty
        self.origins = dict(origins)
        self.origin_growth = dict(earned)
        self.origin_rounding = dict(rounded)

    def provenance_fields(self):
        return dict(growth_alpha=self.growth,
                    known_growth_alpha=self.known_growth,
                    probable_growth_alpha=self.probable_growth,
                    unknown_growth_alpha=max(
                        0., self.growth - self.known_growth
                        - self.probable_growth),
                    known_purchased_alpha=(self.known - self.known_growth
                                           + self.known_rounding),
                    probable_purchased_alpha=(
                        self.probable - self.probable_growth
                        + self.probable_rounding),
                    known_purchased_rounding_loss_alpha=self.known_rounding,
                    probable_purchased_rounding_loss_alpha=(
                        self.probable_rounding))

    def as_position(self, addr, netuid):
        priced = self.known + self.probable
        purchased = (priced - self.known_growth - self.probable_growth
                     + self.known_rounding + self.probable_rounding)
        price = effective = None
        if priced and _fully_priced(self.alpha, priced):
            effective = round((self.cost + self.estimated_cost) / priced, 12)
            if purchased > 0:
                price = round(
                    (self.cost + self.estimated_cost) / purchased, 12)
        return dict(address=addr, netuid=netuid, alpha=self.alpha,
                    buy_price_tao=price, known_alpha=self.known,
                    effective_cost_per_alpha_tao=effective,
                    known_cost_tao=self.cost, probable_alpha=self.probable,
                    probable_cost_tao=self.estimated_cost,
                    unknown_alpha=max(0., self.alpha - priced),
                    **self.provenance_fields())


def _opening_inventory(timeline):
    # Work out the smallest unknown opening stake consistent with both
    # observed movements and the first inventory. This includes unknown
    # stake that was sold or transferred before the first snapshot.
    lots = defaultdict(_Holding)
    net, minimum = defaultdict(float), defaultdict(float)
    seen_all, seen_keys = set(), set()
    for _, is_snapshot, e in timeline:
        if is_snapshot:
            addr = e["address"]
            if addr in seen_all:
                continue
            if "netuid_scope" in e:
                keys = {(addr, n) for n in e["netuid_scope"]}
            else:
                keys = {(a, n) for a, n in net if a == addr}
                keys.update((addr, n) for n in e["units"])
                seen_all.add(addr)
            for key in keys - seen_keys:
                lots[key].alpha = max(-minimum[key],
                                      e["units"].get(key[1], 0) - net[key])
            seen_keys.update(keys)
        elif e["alpha"]:
            src, dst = _inventory_endpoints(e)
            key = src, e["netuid"]
            if (e["kind"] == "stake_reallocation" and src not in seen_all
                    and key not in seen_keys):
                minimum[key] = min(minimum[key], net[key] - e["alpha"])
                net[key] -= e.get("rounding_loss_alpha", 0.)
            if src == dst:
                continue
            for addr, sign in ((src, -1), (dst, 1)):
                key = addr, e["netuid"]
                if addr and addr not in seen_all and key not in seen_keys:
                    net[key] += sign * e["alpha"]
                    minimum[key] = min(net[key], minimum[key])
    for key in net:
        if key[0] not in seen_all and key not in seen_keys:
            lots[key].alpha = -minimum[key]
    return lots


def _fully_priced(alpha, known):
    # Allow floating point accumulation, without treating a small unknown
    # fraction of a large holding as a profitable sale.
    return math.isclose(alpha, known, rel_tol=2e-15, abs_tol=1e-13)


def _sale_stats():
    return dict(wins=0, priced_sales=0, unpriced_sales=0, realized_tao=0.,
                partial_sales=0, known_sale_proceeds_tao=0.,
                known_sale_cost_tao=0., unresolved_sale_proceeds_tao=0.,
                known_sales=0, growth_sales=0, growth_proceeds_tao=0.,
                known_growth_proceeds_tao=0., liquidations=0,
                priced_liquidations=0, unpriced_liquidations=0,
                partial_liquidations=0, known_liquidations=0,
                liquidation_proceeds_tao=0.,
                known_liquidation_proceeds_tao=0.,
                known_liquidation_cost_tao=0.,
                unresolved_liquidation_proceeds_tao=0.)


def _record_sale(event, sold, totals):
    # Weighted-average lots split both the cost and the proceeds of a
    # partial sale proportionally. Only a completely priced sale can win.
    proceeds = event["tao"]
    fully_priced = _fully_priced(sold.alpha, sold.known)
    share = 1. if fully_priced else min(1., sold.known / sold.alpha)
    known_proceeds = proceeds * share
    pnl = known_proceeds - sold.cost if share else None
    growth_proceeds = proceeds * min(1., sold.growth / sold.alpha)
    known_growth_proceeds = proceeds * min(1., sold.known_growth / sold.alpha)
    trading_pnl = (known_proceeds - known_growth_proceeds - sold.cost
                   if share else None)
    trading_eligible = fully_priced and not sold.growth
    unresolved = proceeds - known_proceeds
    totals["known_sale_proceeds_tao"] += known_proceeds
    totals["known_sale_cost_tao"] += sold.cost
    totals["unresolved_sale_proceeds_tao"] += unresolved
    totals["known_sales"] += bool(share)
    totals["growth_sales"] += bool(sold.growth)
    totals["growth_proceeds_tao"] += growth_proceeds
    totals["known_growth_proceeds_tao"] += known_growth_proceeds
    if trading_eligible:
        totals["priced_sales"] += 1
        totals["realized_tao"] += pnl
        totals["wins"] += pnl > 1e-12
    if fully_priced:
        status = "fully_priced"
    else:
        totals["unpriced_sales"] += 1
        totals["partial_sales"] += bool(share)
        status = "partially_priced" if share else "unpriced"
    return dict(id=event["id"], time=event["time"], kind=event["kind"],
                forced_disposal=False, evidence=event.get("evidence"),
                block_number=event.get("block_number"),
                source_event_id=event.get("source_event_id"),
                extrinsic=event.get("extrinsic"),
                event_index=event.get("event_index"), address=event["src"],
                netuid=event["netuid"], alpha=sold.alpha,
                proceeds_tao=proceeds,
                basis_status=status, known_alpha=sold.known,
                known_proceeds_tao=known_proceeds, known_cost_tao=sold.cost,
                known_realized_tao=pnl, probable_alpha=sold.probable,
                known_trading_realized_tao=trading_pnl,
                trading_eligible=trading_eligible,
                growth_proceeds_tao=growth_proceeds,
                known_growth_proceeds_tao=known_growth_proceeds,
                probable_cost_tao=sold.estimated_cost,
                unknown_alpha=max(0., sold.alpha - sold.known - sold.probable),
                unresolved_proceeds_tao=unresolved, **sold.provenance_fields())


def _record_liquidation(event, removed, totals):
    # The same weighted cost allocation applies to forced settlement, but
    # none of its proceeds or counts may become evidence of trading skill.
    temporary = _sale_stats()
    detail = _record_sale(event, removed, temporary)
    totals["liquidations"] += 1
    totals["priced_liquidations"] += detail["basis_status"] == "fully_priced"
    for target, source in (
            ("unpriced_liquidations", "unpriced_sales"),
            ("partial_liquidations", "partial_sales"),
            ("known_liquidations", "known_sales"),
            ("known_liquidation_proceeds_tao", "known_sale_proceeds_tao"),
            ("known_liquidation_cost_tao", "known_sale_cost_tao"),
            ("unresolved_liquidation_proceeds_tao",
             "unresolved_sale_proceeds_tao")):
        totals[target] += temporary[source]
    totals["liquidation_proceeds_tao"] += event["tao"]
    return dict(detail, forced_disposal=True, trading_eligible=False,
                known_trading_realized_tao=None)


def _finish_sales(s):
    has_cost = s["known_sales"]
    pnl = s["known_sale_proceeds_tao"] - s["known_sale_cost_tao"]
    forced_pnl = (s["known_liquidation_proceeds_tao"]
                  - s["known_liquidation_cost_tao"])
    s["known_sale_realized_tao"] = round(pnl, 9) if has_cost else None
    s["known_liquidation_realized_tao"] = (
        round(forced_pnl, 9) if s["known_liquidations"] else None)
    s["known_realized_tao"] = (
        round(pnl + forced_pnl, 9) if has_cost or s["known_liquidations"]
        else None)
    s["known_trading_realized_tao"] = (
        round(pnl - s["known_growth_proceeds_tao"], 9) if has_cost else None)
    s["economic_realized_tao"] = (
        None if s["unpriced_sales"] or s["unpriced_liquidations"]
        else s["known_realized_tao"])
    for key in ("realized_tao", "known_sale_proceeds_tao",
                "known_sale_cost_tao", "unresolved_sale_proceeds_tao",
                "growth_proceeds_tao", "known_growth_proceeds_tao",
                "liquidation_proceeds_tao", "known_liquidation_proceeds_tao",
                "known_liquidation_cost_tao",
                "unresolved_liquidation_proceeds_tao"):
        s[key] = round(s[key], 9)


def _transfer_receipt(e, part):
    row = {key: e.get(key) for key in (
        "id", "time", "extrinsic", "src", "dst", "netuid", "alpha", "tao",
        "evidence", "transfer_side", "confidence", "kind")}
    row["origins"] = []
    inherited = entry = None
    if e["kind"] == "tao_transfer":
        return dict(row, basis_status="native_tao",
                    inherited_buy_price_tao=None,
                    known_alpha=None, known_cost_tao=None,
                    known_buy_price_tao=None, probable_alpha=None,
                    probable_cost_tao=None, unknown_alpha=None,
                    other_origin_alpha=None, other_origin_cost_tao=None,
                    effective_cost_per_alpha_tao=None, growth_alpha=None,
                    known_growth_alpha=None, probable_growth_alpha=None,
                    unknown_growth_alpha=None)
    for (purchase, estimated), units in sorted(part.origins.items(),
                                               key=_origin_order):
        if units <= 0:
            continue
        row["origins"].append(dict(
            address=purchase.address, time=purchase.time,
            event_id=purchase.event_id, extrinsic=purchase.extrinsic or None,
            unit_buy_price_tao=purchase.unit_price,
            alpha=(units + part.origin_growth.get((purchase, estimated), 0.)
                   - part.origin_rounding.get((purchase, estimated), 0.)),
            purchased_alpha=units,
            purchased_rounding_loss_alpha=part.origin_rounding.get(
                (purchase, estimated), 0.),
            growth_alpha=part.origin_growth.get((purchase, estimated), 0.),
            cost_tao=purchase.unit_price * units,
            basis_status="estimated" if estimated else "confirmed"))
    priced = part.known + part.probable
    purchased = (priced - part.known_growth - part.probable_growth
                 + part.known_rounding + part.probable_rounding)
    cost = part.cost + part.estimated_cost
    if part.known > part.known_growth:
        entry = part.cost / (
            part.known - part.known_growth + part.known_rounding)
    side = e.get("transfer_side")
    if side == "unknown" or e["src"] == e["dst"]:
        status = "direction_unknown"
    elif side == "out" or not e["dst"]:
        status = "debit_only"
    elif side == "in" or not e["src"]:
        status = "credit_only"
    elif not priced:
        status = "unknown"
    elif not _fully_priced(part.alpha, priced):
        status = "partial"
    else:
        inherited = cost / purchased if purchased > 0 else None
        status = "estimated" if part.probable else "confirmed"
    sampled_qty = sum(o["alpha"] for o in row["origins"])
    sampled_cost = sum(o["cost_tao"] for o in row["origins"])
    return dict(
        row, basis_status=status, inherited_buy_price_tao=inherited,
        effective_cost_per_alpha_tao=(
            cost / priced if priced and _fully_priced(part.alpha, priced)
            else None),
        known_alpha=part.known, known_cost_tao=part.cost,
        known_buy_price_tao=entry, probable_alpha=part.probable,
        probable_cost_tao=part.estimated_cost,
        unknown_alpha=max(0., part.alpha - priced),
        other_origin_alpha=max(0., priced - sampled_qty),
        other_origin_cost_tao=max(0., cost - sampled_cost),
        **part.provenance_fields())


def _growth_inventory_matches(actual, expected):
    # Chain stake quantities can differ by one nano-alpha through integer
    # conversion. This is only an endpoint check, never a priced-sale rule.
    return math.isclose(actual, expected, rel_tol=2e-15, abs_tol=1.1e-9)


def _growth_timeline(growth_events, events, checkpoints):
    """Normalize trusted proof intervals; never infer growth from a delta.

    The caller must verify unchanged stake shares, compatible pool epochs,
    complete movement coverage, and whole-wallet/subnet endpoint balances.
    The ledger additionally rejects contradictory observed movements.
    """
    unique = {}
    for proof in growth_events:
        try:
            start = _parse_ts(proof["start_time"])
            end = _parse_ts(proof["time"])
            before = float(proof["before_alpha"])
            after = float(proof["after_alpha"])
            valid = (proof["evidence"] == "verified_stake_growth"
                     and proof["id"] and proof["address"]
                     and isinstance(proof["netuid"], int)
                     and start is not None and end is not None and start < end
                     and math.isfinite(before) and math.isfinite(after)
                     and 0 < before < after)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Invalid verified stake growth proof") from exc
        if not valid:
            raise ValueError("Invalid verified stake growth proof")
        normalized = dict(proof, before_alpha=before, after_alpha=after)
        if proof["id"] in unique and unique[proof["id"]] != normalized:
            raise ValueError("Conflicting stake growth proof ID")
        unique[proof["id"]] = normalized
    intervals = defaultdict(list)
    timeline = []
    dated = sorted(unique.values(), key=lambda p: _parse_ts(p["start_time"]))
    for proof in dated:
        start, end = _parse_ts(proof["start_time"]), _parse_ts(proof["time"])
        key = proof["address"], proof["netuid"]
        if intervals[key] and intervals[key][-1] > start:
            raise ValueError("Overlapping stake growth proof intervals")
        intervals[key].append(end)
        for event in events:
            if (event["netuid"] == key[1] and event.get("alpha")
                    and key[0] in (event["src"], event["dst"])
                    and start < _parse_ts(event["time"]) <= end):
                raise ValueError(
                    "Observed movement inside stake growth interval")
        for checkpoint in checkpoints:
            observed = _parse_ts(checkpoint["time"])
            expected = (proof["after_alpha"] if observed == end
                        else proof["before_alpha"])
            if (checkpoint["address"] == key[0]
                    and ("netuid_scope" not in checkpoint
                         or key[1] in checkpoint["netuid_scope"])
                    and start <= observed <= end
                    and not _growth_inventory_matches(
                        checkpoint["units"].get(key[1], 0.),
                        expected)):
                raise ValueError(
                    "Stake growth inventory conflicts with dated snapshot")
        anchor = dict(address=key[0], time=proof["start_time"],
                      units={key[1]: proof["before_alpha"]},
                      netuid_scope=[key[1]], growth_start=proof["id"])
        earned = dict(proof, kind="stake_growth", src=None, dst=key[0],
                      alpha=proof["after_alpha"] - proof["before_alpha"],
                      tao=None)
        timeline.extend([(start, True, anchor), (end, False, earned)])
    return timeline


def portfolio(events, checkpoints, *, subnet_stats=None, sale_details=None,
              transfer_details=None, growth_events=()):
    """Replay once; optionally write the per-wallet/subnet sale breakdown
    into the supplied mapping. A checkpoint with ``netuid_scope`` observes
    only those assets; an omitted scope observes the complete account.

    ``growth_events`` contains externally verified, dated stake-share growth
    intervals with id, address, netuid, start_time, time, before_alpha,
    after_alpha and evidence="verified_stake_growth". These are trusted
    proofs, not guesses from snapshots. Growth retains acquisition cost and
    source uncertainty; its proceeds are excluded from trading signals.
    """
    events = sorted(events, key=_chain_order)
    if any(e["kind"] == "stake_growth" for e in events):
        raise ValueError("Stake growth requires verified growth_events input")
    checkpoints = list(checkpoints)
    timeline = [(_parse_ts(e["time"]), False, e) for e in events]
    timeline.extend((_parse_ts(p["time"]), True, p) for p in checkpoints)
    timeline.extend(_growth_timeline(growth_events, events, checkpoints))
    timeline.sort(key=lambda item: item[:2])
    lots = _opening_inventory(timeline)
    sales = defaultdict(_sale_stats)
    if sale_details is not None:
        sale_details.clear()
    if transfer_details is not None:
        transfer_details.clear()
    for _, is_snapshot, e in timeline:
        if is_snapshot:
            addr = e["address"]
            nets = (set(e["netuid_scope"]) if "netuid_scope" in e else
                    set(e["units"]) | {n for a, n in lots if a == addr})
            for n in nets:
                if e.get("growth_start"):
                    if not _growth_inventory_matches(
                            lots[addr, n].alpha, e["units"][n]):
                        raise ValueError(
                            "Stake growth start inventory conflicts "
                            "with ledger")
                    continue
                lots[addr, n].observe(e["units"].get(n, 0))
            continue
        qty, n = e["alpha"], e["netuid"]
        if e["kind"] == "tao_transfer":
            if transfer_details is not None:
                transfer_details.append(_transfer_receipt(e, None))
            continue
        src, dst = _inventory_endpoints(e)
        if e["kind"] == "stake_growth":
            lot = lots[dst, n]
            if (lot.alpha <= 0 or lot.alpha > e["after_alpha"]
                    or not _growth_inventory_matches(
                        lot.alpha, e["before_alpha"])):
                raise ValueError(
                    "Stake growth end inventory conflicts with ledger")
            lot.grow(e["after_alpha"])
            continue
        if e["kind"] == "stake_reallocation":
            lot = lots[src, n]
            lot.observe(max(lot.alpha, qty))
            if e["evidence"] == "native_move" and e.get("rounding_loss_alpha"):
                part = lot.remove(qty)
                part.lose_rounding_units(e["rounding_loss_alpha"])
                lot.add(part)
            elif e["evidence"] != "native_move":
                # Both sides cancel economically, but this could be a real
                # sale and re-entry. The old cost on these units is only
                # an estimate until the native call resolves the ambiguity.
                part = lot.remove(qty)
                part.mark_probable()
                lot.add(part)
            continue
        if e.get("transfer_side") == "unknown":
            # Older self-transfer rows can lack direction. Their previous
            # cost can no longer support a priced sale on this subnet.
            key = e["owner"], n
            lots[key] = _Holding(lots[key].alpha)
        if not qty or src == dst:
            if (transfer_details is not None and qty
                    and e["kind"] == "stake_transfer"):
                transfer_details.append(_transfer_receipt(e, _Holding(qty)))
            continue
        moved = _Holding(qty)
        if src:
            moved = lots[src, n].remove(qty)
        if e["kind"] in ("sell", "subnet_liquidation"):
            record = (_record_liquidation if e["kind"] == "subnet_liquidation"
                      else _record_sale)
            detail = record(e, moved, sales[src, n])
            if sale_details is not None:
                sale_details.append(detail)
        elif dst:
            if e["kind"] == "buy":
                moved = _Holding(qty, qty, e["tao"])
                if transfer_details is not None:
                    purchase = _Purchase(dst, e["time"], e["id"],
                                         e.get("extrinsic") or "",
                                         e["tao"] / qty)
                    moved.origins[purchase, False] = qty
            elif e["evidence"] == "snapshot_match":
                moved.mark_probable()
            lots[dst, n].add(moved)
        if transfer_details is not None and e["kind"] == "stake_transfer":
            transfer_details.append(_transfer_receipt(e, moved))
    positions = {key: lot.as_position(*key) for key, lot in lots.items()
                 if lot.alpha > 1e-12}
    stats = defaultdict(_sale_stats)
    for (addr, _), s in sales.items():
        for key, value in s.items():
            stats[addr][key] += value
        _finish_sales(s)
    if subnet_stats is not None:
        subnet_stats.clear()
        subnet_stats.update(sales)
    for s in stats.values():
        _finish_sales(s)
    return positions, stats


def timing_observations(events, prices, *, with_timestamp=False):
    """Yield each trade and the first subnet quote 24 to 30 hours later."""
    quotes = defaultdict(list)
    for p in prices:
        if p["price_tao"] and p["price_tao"] > 0:
            quotes[p["netuid"]].append((
                _parse_ts(p["fetched_at"]), p["price_tao"],
                _parse_ts(p.get("registered_at"))))
    for rows in quotes.values():
        rows.sort(key=lambda row: row[0])
    for e in events:
        if e["kind"] not in ("buy", "sell") or not e["alpha"] or not e["tao"]:
            continue
        traded = _parse_ts(e["time"])
        start = traded + timedelta(hours=24)
        rows = quotes[e["netuid"]]
        i = bisect_left(rows, (start,))
        if i == len(rows) or rows[i][0] > start + timedelta(hours=6):
            continue
        at, price, registered = rows[i]
        if registered is None or registered <= traded:
            if with_timestamp:
                yield e, price, at.astimezone(timezone.utc).isoformat()
            else:
                yield e, price


def timing(events, prices):
    result = defaultdict(lambda: {"evaluated": 0, "favorable": 0})
    for e, quote in timing_observations(events, prices):
        p = e["tao"] / e["alpha"]
        s = result[e["src"] or e["dst"]]
        s["evaluated"] += 1
        s["favorable"] += quote > p if e["kind"] == "buy" else quote < p
    return result
