"""Evidence-preserving transfer groups for saved wallet collection reports.

``build_groups([report, ...])`` is pure: reports are decoded JSON mappings,
optionally carrying ``_source`` for their filename. Groups are connected
components of documented transfers, never claims of friendship, common
ownership, or coordinated trading. Inferred snapshot matches remain visible
as edges but cannot merge groups. Screened/hub edges remain visible too.

Group economics use current wallet inventories, never transfer receipt costs.
Mixed-date partial subtotals include only the freshest collection's members,
so an old sender's stake cannot be counted again at its newer recipient.
"""

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re


_KINDS = {"stake_transfer", "tao_transfer", "coldkey_swap"}
_ANCHOR = re.compile(r"[0-9]+-[0-9]+\Z")


def _packed(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def _identity(prefix, value):
    return prefix + sha256(_packed(value).encode()).hexdigest()[:16]


def _when(stamp):
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError
        return when.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            "Wallet report timestamp must include timezone") from exc


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _positive(value):
    return (_number(value) or 0) > 0


def _source(report):
    return dict(checked_at=report["checked_at"], source=report.get("_source"))


def _event_key(event):
    """Collapse mirrored views, retaining distinct chain event indexes."""
    anchor = event.get("extrinsic")
    if anchor and _ANCHOR.fullmatch(str(anchor)):
        return [anchor, event.get("event_index"), event.get("kind"),
                event.get("src"), event.get("dst"), event.get("netuid"),
                event.get("alpha"), event.get("tao"), event.get("time")]
    if event.get("kind") == "coldkey_swap":
        return ["coldkey_swap", event.get("src"), event.get("dst")]
    return [event.get("id"), event.get("kind"), event.get("src"),
            event.get("dst"), event.get("netuid"), event.get("time"),
            event.get("alpha"), event.get("tao"), event.get("evidence")]


def _status(event):
    if (event.get("success") is False
            or event.get("transfer_side") == "unknown"):
        return "unverified", None, "failed_or_unknown_direction"
    evidence = event.get("evidence")
    if evidence == "snapshot_match":
        return "inferred", _number(event.get("confidence")), "snapshot_match"
    if evidence == "coldkey_swap":
        return "documented", 1.0, None
    amount = event.get("tao" if event["kind"] == "tao_transfer" else "alpha")
    if (evidence == "transaction" and _positive(amount)
            and _ANCHOR.fullmatch(str(event.get("extrinsic") or ""))):
        return "confirmed", 1.0, None
    return ("unverified", _number(event.get("confidence")),
            "missing_transaction_proof")


def _gather(reports):
    """Resolve newer event classifications and wallet snapshots first."""
    dated = []
    for report in reports:
        stamp = report.get("checked_at")
        # Verified local replay overlays have the same collection time and
        # supersede that report's original ledger classification/cost basis.
        priority = (_when(stamp), bool(report.get("local_evidence")),
                    _identity("", report))
        dated.append((priority, report))
    records, receipts, profiles = {}, {}, {}
    observations = defaultdict(dict)
    for priority, report in sorted(dated, key=lambda item: item[0]):
        source = _source(report)
        source_key = _packed(source)
        coverage = report.get("coverage") or {}
        for wallet in (report.get("performance") or {}).get("wallets", []):
            address = wallet.get("address")
            if not address:
                continue
            observed = address in coverage
            score = (observed, priority)
            if address not in profiles or score > profiles[address][0]:
                profiles[address] = (
                    score, wallet,
                    coverage.get(address) or wallet.get("coverage") or {},
                    report["checked_at"] if observed else None)
        # Receipt bodies carry basis provenance; the normalized event stream
        # has authority over event kind/direction in the same report.
        for field in ("transfer_details", "transfers", "events"):
            for event in report.get(field) or []:
                if not isinstance(event, dict):
                    continue
                ident = str(event.get("id") or _identity(
                    "anonymous-", _event_key(event)))
                records[ident] = event
                observations[ident][source_key] = source
                if field == "transfer_details":
                    receipts[ident] = dict(
                        deepcopy(event), checked_at=report["checked_at"],
                        source_report=report.get("_source"))
        for swap in report.get("swaps") or []:
            if isinstance(swap, (list, tuple)) and len(swap) == 2:
                src, dst = swap
            elif isinstance(swap, dict):
                src, dst = swap.get("src"), swap.get("dst")
            else:
                continue
            event = dict(src=src, dst=dst, kind="coldkey_swap",
                         evidence="coldkey_swap")
            ident = _identity("swap-", [src, dst])
            event["id"] = ident
            records[ident] = event
            observations[ident][source_key] = source
    aliases, paired = defaultdict(set), set()
    for ident, event in records.items():
        counterpart = event.get("paired_event_id")
        other = records.get(counterpart, {})
        if (event.get("kind") == "stake_transfer"
                and event.get("transfer_side") == "both"
                and other.get("transfer_side") in {"in", "out"}
                and all(event.get(field) == other.get(field) for field in
                        ("kind", "src", "dst", "extrinsic", "netuid",
                         "alpha"))):
            aliases[ident].add(counterpart)
            paired.add(counterpart)
    edges = {}
    for ident, event in sorted(records.items()):
        if ident in paired:
            continue
        src, dst = event.get("src"), event.get("dst")
        if event.get("kind") not in _KINDS or not src or not dst or src == dst:
            continue
        key = _identity("edge-", _event_key(event))
        status, confidence, reason = _status(event)
        if key not in edges:
            edges[key] = {field: deepcopy(event.get(field)) for field in (
                "src", "dst", "kind", "evidence", "alpha", "tao", "netuid",
                "time", "extrinsic", "event_index", "transfer_side")}
            edges[key].update(
                id=key, status=status, confidence=confidence,
                amount_is_inferred=event.get("evidence") in {
                    "snapshot_match", "coldkey_swap"}
                and event.get("kind") != "coldkey_swap",
                event_ids=[], source_reports=[], receipts=[],
                group_eligible=status in {"confirmed", "documented"},
                exclusion_reasons=[reason] if reason else [])
        edge = edges[key]
        for event_id in sorted({ident} | aliases[ident]):
            edge["event_ids"].append(event_id)
            edge["source_reports"].extend(observations[event_id].values())
        if ident in receipts:
            edge["receipts"].append(receipts[ident])
    for edge in edges.values():
        edge["event_ids"] = sorted(set(edge["event_ids"]))
        for field in ("source_reports", "receipts"):
            unique = {_packed(row): row for row in edge[field]}
            edge[field] = [unique[key] for key in sorted(unique)]
    return [edges[key] for key in sorted(edges)], profiles


def _wallet_metrics(wallet, coverage, stamp):
    current = wallet.get("current_inventory") is True and stamp is not None
    positions = wallet.get("positions") or []
    held = [p for p in positions if _positive(p.get("alpha"))]
    realized = next((_number(wallet.get(key)) for key in (
        "economic_realized_tao", "known_realized_tao", "realized_tao")
        if _number(wallet.get(key)) is not None), None)
    # A fully empty and freshly observed account has zero open P/L; unknown
    # inventory never gets the same zero by default.
    opened = _number(wallet.get("gross_unrealized_tao")) if current else None
    if current and not held:
        opened = 0
    known_open = _number((wallet.get("open") or {}).get("known_pnl_tao"))
    if known_open is None:
        marked = [_number(p.get("known_unrealized_tao")) for p in held
                  if p.get("current_quote")
                  and _positive(p.get("known_alpha"))]
        values = [value for value in marked if value is not None]
        known_open = sum(values) if values else opened
    costs = [_number(p.get("known_cost_tao")) for p in held
             if _positive(p.get("known_alpha"))]
    costs = [value for value in costs if value is not None]
    known_cost = (sum(costs) if costs or not held else None)
    if not current:
        known_cost = None
    fully_costed = bool(
        current and all(coverage.get(k) == "complete" for k in
                        ("account", "transactions", "transfers"))
        and wallet.get("unpriced_sales") == 0 and realized is not None
        and not wallet.get("unpriced_liquidations")
        and opened is not None
        and all(p.get("current_quote")
                and _number(p.get("known_cost_tao")) is not None
                and _number(p.get("known_alpha")) is not None
                and math.isclose(p["known_alpha"], p["alpha"],
                                 abs_tol=1e-13, rel_tol=2e-15)
                and not _positive(p.get("probable_alpha"))
                and not _positive(p.get("unknown_alpha")) for p in held))
    return dict(current=current, fully_costed=fully_costed,
                realized=realized, unrealized=opened,
                known_unrealized=known_open if current else None,
                known_cost=known_cost)


def _economics(members, profiles):
    rows = []
    for address in members:
        _, wallet, coverage, stamp = profiles.get(
            address, (None, {}, {}, None))
        rows.append(dict(address=address, stamp=stamp,
                         **_wallet_metrics(wallet, coverage, stamp)))
    stamps = {row["stamp"] for row in rows if row["stamp"]}
    latest = (max(stamps, key=lambda stamp: (_when(stamp), stamp))
              if stamps else None)
    fresh = [r for r in rows if latest and r["stamp"]
             and _when(r["stamp"]) == _when(latest)]
    current = sum(r["current"] for r in fresh)
    costed = sum(r["fully_costed"] for r in fresh)
    complete = len(fresh) == len(rows) and costed == len(rows)
    mixed = len({_when(stamp) for stamp in stamps}) > 1
    reasons = []
    if mixed:
        reasons.append("members_have_different_snapshot_dates")
    if len(fresh) < len(rows):
        reasons.append("members_missing_from_latest_snapshot")
    if current < len(fresh):
        reasons.append("current_inventory_incomplete")
    if costed < len(fresh):
        reasons.append("cost_basis_or_history_incomplete")

    def subtotal(key):
        values = [r[key] for r in fresh if r[key] is not None]
        return sum(values) if values else None

    realized = subtotal("realized") if complete else None
    unrealized = subtotal("unrealized") if complete else None
    return dict(
        status=("complete_same_snapshot" if complete else
                "mixed_snapshots" if mixed else "partial"),
        as_of=latest if complete else None, subtotal_as_of=latest,
        member_count=len(rows), current_members=current,
        fully_costed_members=costed, coverage_fraction=costed / len(rows),
        subtotal_member_count=len(fresh), realized_tao=realized,
        unrealized_tao=unrealized,
        total_pnl_tao=realized + unrealized if complete else None,
        known_realized_subtotal_tao=subtotal("realized"),
        known_unrealized_subtotal_tao=subtotal("known_unrealized"),
        known_cost_subtotal_tao=subtotal("known_cost"),
        rankable=complete, reasons=reasons,
        scope="Observed subnet trades and current stake; fees excluded")


def _components(edges):
    adjacency = defaultdict(set)
    for edge in edges:
        if edge["group_eligible"]:
            adjacency[edge["src"]].add(edge["dst"])
            adjacency[edge["dst"]].add(edge["src"])
    visited = set()
    for start in sorted(adjacency):
        if start in visited:
            continue
        pending, members = [start], set()
        while pending:
            address = pending.pop()
            if address in members:
                continue
            members.add(address)
            pending.extend(adjacency[address] - members)
        visited.update(members)
        yield members


def build_groups(reports, *, max_degree=12, max_activity_24h=100,
                 excluded_addresses=(), min_cash_transfers=2, min_cash_tao=0,
                 max_cash_degree=12, max_group_size=24):
    """Return groups, nodes, edges and excluded_nodes as JSON-compatible data.

    Zero disables a limit. ``max_degree`` counts distinct stake/swap peers;
    ``max_cash_degree`` suppresses native-funding edges only. Repeated native
    transfers require distinct extrinsics above ``min_cash_tao``; one-off
    funding remains a visible lead. Oversized components lose cash bridges,
    and any still-oversized stake network is visible but not bulk watchable.
    Group IDs depend only on sorted membership, never input order.
    """
    for value in (max_degree, max_activity_24h, min_cash_transfers,
                  min_cash_tao, max_cash_degree, max_group_size):
        if _number(value) is None or value < 0:
            raise ValueError("Group limits must be finite nonnegative numbers")
    edges, profiles = _gather(reports)
    neighbors, stake_peers, cash_peers = (defaultdict(set) for _ in range(3))
    cash_extrinsics = defaultdict(set)
    addresses = set(profiles)
    for edge in edges:
        addresses.update((edge["src"], edge["dst"]))
        if edge["group_eligible"]:
            neighbors[edge["src"]].add(edge["dst"])
            neighbors[edge["dst"]].add(edge["src"])
            peers = (cash_peers if edge["kind"] == "tao_transfer"
                     else stake_peers)
            peers[edge["src"]].add(edge["dst"])
            peers[edge["dst"]].add(edge["src"])
            if edge["kind"] == "tao_transfer" and edge["tao"] >= min_cash_tao:
                cash_extrinsics[tuple(sorted((edge["src"], edge["dst"])))].add(
                    edge["extrinsic"])
    nodes, excluded = {}, {}
    explicit = set(excluded_addresses)
    for address in sorted(addresses):
        _, wallet, coverage, stamp = profiles.get(
            address, (None, {}, {}, None))
        reasons = ["screen:" + str(reason)
                   for reason in wallet.get("screens") or []]
        if address in explicit:
            reasons.append("explicit_exclusion")
        if max_degree and len(stake_peers[address]) > max_degree:
            reasons.append("degree_above_limit")
        activity = wallet.get("activity_24h") or {}
        count = sum(_number(value) or 0 for value in activity.values())
        if max_activity_24h and count > max_activity_24h:
            reasons.append("activity_above_limit")
        current = wallet.get("current_inventory") is True and stamp is not None
        subnets = sorted({p["netuid"] for p in wallet.get("positions") or []
                          if _positive(p.get("alpha"))
                          and _positive(p.get("netuid"))})
        nodes[address] = dict(address=address, checked_at=stamp,
                              current_inventory=current,
                              holds_now=bool(subnets) if current else None,
                              held_netuids=subnets if current else [],
                              screened_reasons=sorted(set(reasons)),
                              degree=len(neighbors[address]),
                              stake_degree=len(stake_peers[address]),
                              cash_degree=len(cash_peers[address]))
        if reasons:
            excluded[address] = sorted(set(reasons))
    for edge in edges:
        for endpoint in (edge["src"], edge["dst"]):
            edge["exclusion_reasons"].extend(
                endpoint + ":" + reason
                for reason in excluded.get(endpoint, []))
            if (edge["kind"] == "tao_transfer" and max_cash_degree
                    and len(cash_peers[endpoint]) > max_cash_degree):
                edge["exclusion_reasons"].append(
                    endpoint + ":cash_degree_above_limit")
        if edge["kind"] == "tao_transfer" and edge["group_eligible"]:
            pair = tuple(sorted((edge["src"], edge["dst"])))
            if edge["tao"] < min_cash_tao:
                edge["exclusion_reasons"].append(
                    "cash_below_materiality_limit")
            if len(cash_extrinsics[pair]) < min_cash_transfers:
                edge["exclusion_reasons"].append("single_native_funding_lead")
        if edge["exclusion_reasons"]:
            edge["group_eligible"] = False
    if max_group_size:
        large = set().union(*(part for part in _components(edges)
                              if len(part) > max_group_size))
        for edge in edges:
            if (edge["group_eligible"] and edge["kind"] == "tao_transfer"
                    and edge["src"] in large):
                edge["group_eligible"] = False
                edge["exclusion_reasons"].append("large_component_cash_bridge")
    groups = []
    for members in _components(edges):
        members = sorted(members)
        member_set = set(members)
        within, bridges = [], []
        for edge in edges:
            ends = {edge["src"], edge["dst"]}
            if edge["group_eligible"] and ends <= member_set:
                within.append(edge["id"])
            elif ends & member_set and not edge["group_eligible"]:
                bridges.append(edge["id"])
        oversized = bool(max_group_size and len(members) > max_group_size)
        groups.append(dict(id=_identity("group-", members), members=members,
                           edge_ids=within, bridge_edge_ids=bridges,
                           relationship="observed_transfer_connections",
                           ownership="unproven", coordination="unproven",
                           oversized=oversized, watchable=not oversized,
                           economics=_economics(members, profiles)))
    return dict(groups=sorted(groups, key=lambda group: group["id"]),
                nodes=nodes, edges=edges, excluded_nodes=excluded)
