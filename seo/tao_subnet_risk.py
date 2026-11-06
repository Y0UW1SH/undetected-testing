"""Qualify saved wallet research with dated native subnet context.

This is a separate screening assessment, never a ledger recalculation. The
defaults (216,000 blocks of subnet age, 1,000 TAO reserve, 5% marked exposure,
24-hour freshness) are configurable research choices, not protocol rules.
Exposure is saved marked value divided by observed TAO reserve, not slippage,
an executable exit quote, or a safety guarantee. Historical P/L is untouched.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
import re


_EXTRINSIC = re.compile(r"([0-9]+)-[0-9]+\Z")
_REASONS = {
    "current_inventory_unknown": "Current wallet inventory is unconfirmed.",
    "wallet_time_unknown": "The wallet observation time is unavailable.",
    "wallet_snapshot_stale": "The wallet snapshot exceeds the freshness "
                             "limit.",
    "context_missing": "No subnet context was available by assessment time.",
    "context_stale": "The chain context exceeds the freshness limit.",
    "context_generation_unknown": "Registration generation is unconfirmed.",
    "generation_counter_inconsistent": "Saved registration counters conflict "
                                       "without a demonstrated new "
                                       "registration block.",
    "subnet_status_unknown": "The subnet's active status is unconfirmed.",
    "subnet_inactive": "The subnet is inactive; retained holdings have no "
                       "validated current exit valuation.",
    "subnet_retiring": "The subnet is dissolving or awaiting cleanup; "
                       "current exit valuation is unconfirmed.",
    "trading_disabled": "The subnet's staking enable flag is false; saved "
                        "marks do not establish an executable exit. This "
                        "flag alone does not prove every exit is blocked.",
    "new_subnet": "Subnet age is within the configured early-stage window.",
    "subnet_in_immunity": "The subnet is in its protocol immunity period.",
    "immunity_unknown": "Subnet immunity status is unavailable.",
    "subnet_generation_changed": "The same subnet ID has a different "
                                 "registration generation in saved context.",
    "subnet_id_reused": "The subnet registration counter records prior use "
                        "of this ID; that does not prove a wallet mismatch.",
    "entry_generation_unknown": "Purchase block provenance is unavailable; "
                                "entry generation cannot be established.",
    "entry_generation_conflict": "Remaining-position purchase provenance "
                                 "predates this registration; current "
                                 "accounting may mix different assets "
                                 "sharing one subnet ID.",
    "historical_generation_overlap": "Historical purchases or transfers "
                                     "predate this registration; they do not "
                                     "alone prove the remaining units' era.",
    "entry_registration_boundary": "A purchase shares the registration "
                                   "block; intra-block generation order "
                                   "is unconfirmed.",
    "mark_generation_conflict": "The saved holding mark belongs to an "
                                "earlier subnet registration generation.",
    "mark_unknown": "A dated current holding mark is unavailable.",
    "mark_future": "The holding quote is later than assessment time.",
    "mark_stale": "The holding quote exceeds the freshness limit.",
    "liquidity_unknown": "A consistent native TAO reserve is unavailable.",
    "low_tao_reserve": "Observed TAO reserve is below the configured screen.",
    "high_pool_exposure": "Saved marked holdings exceed the configured "
                          "fraction of TAO reserve; this is not slippage.",
}
_UNKNOWN = {
    "current_inventory_unknown", "wallet_time_unknown",
    "wallet_snapshot_stale", "context_missing", "context_stale",
    "context_generation_unknown", "subnet_status_unknown", "immunity_unknown",
    "generation_counter_inconsistent",
    "entry_generation_unknown", "mark_unknown", "mark_future", "mark_stale",
    "entry_registration_boundary",
    "liquidity_unknown",
}


def _time(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def _integer(value):
    return value if type(value) is int and value >= 0 else None


def _decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def _block(event):
    match = _EXTRINSIC.fullmatch(str(event.get("extrinsic") or ""))
    if match:
        return int(match[1])
    return _integer(event.get("block_number", event.get("block")))


def _contexts(contexts, now):
    if isinstance(contexts, dict):
        contexts = [contexts]
    candidates = []
    for doc in contexts:
        if isinstance(doc, dict):
            history = doc.get("history") or []
            if isinstance(history, list):
                candidates.extend((old, True) for old in history[-32:])
            candidates.append((doc, False))
    dated = {}
    for doc, archived in candidates:
        if not isinstance(doc, dict):
            continue
        fetched, block_time = _time(doc.get("checked_at")), _time(
            doc.get("block_time"))
        block = _integer(doc.get("finalized_block"))
        if (fetched is None or block_time is None or block is None
                or fetched > now or block_time > now):
            continue
        key = block_time, block, fetched, str(doc.get("block_hash") or "")
        richness = (sum(bool(row.get("liquidity")) for row in
                        doc.get("subnets", []) if isinstance(row, dict)),
                    not archived)
        if key not in dated or richness > dated[key][0]:
            dated[key] = richness, doc
    return [(key[0], key[1], key[2], dated[key][1]) for key in sorted(dated)]


def _entry_evidence(wallet, positions, netuid, events, now, context_block):
    address, evidence = wallet.get("address"), {}

    def add(event, source):
        block = _block(event)
        when = _time(event.get("time"))
        if (block is None or (context_block is not None
                              and block > context_block)
                or (when is not None and when > now)):
            return
        key = (block, str(event.get("extrinsic") or ""), source,
               str(event.get("event_id", event.get("id")) or ""))
        evidence[key] = dict(block=block, source=source,
                             extrinsic=event.get("extrinsic"),
                             event_id=event.get("event_id", event.get("id")),
                             address=event.get("address", address))

    def origins(row, source="purchase_origin"):
        for origin in row.get("origins") or []:
            if isinstance(origin, dict):
                amount = _decimal(origin.get("alpha"))
                if amount is None or amount > 0:
                    add(origin, source)

    for position in positions:
        origins(position, "current_purchase_origin")
    for event in events:
        if not isinstance(event, dict) or event.get("netuid") != netuid:
            continue
        event_time = _time(event.get("time"))
        if event_time is not None and event_time > now:
            continue
        if event.get("kind") == "buy" and event.get("dst") == address:
            add(event, "wallet_buy")
        elif event.get("kind") == "stake_transfer" and event.get(
                "dst") == address:
            origins(event)
            for receipt in event.get("receipts") or []:
                if (isinstance(receipt, dict)
                        and receipt.get("dst", address) == address
                        and receipt.get("netuid", netuid) == netuid):
                    origins(receipt)
    for event in wallet.get("examples") or []:
        if event.get("kind") == "buy" and event.get("netuid") == netuid:
            add(event, "wallet_buy_sample")
    return [evidence[key] for key in sorted(evidence)]


def _mark(positions, wallet_stamp):
    value, dates, complete = Decimal(0), [], True
    for position in positions:
        stamp = position.get("quote_at") or wallet_stamp
        when = _time(stamp)
        amount = _decimal(position.get("marked_value_tao"))
        if amount is None:
            alpha, price = (_decimal(position.get(key)) for key in
                            ("alpha", "price_tao"))
            amount = alpha * price if alpha is not None and (
                price is not None) else None
        current = position.get("current_quote") is True
        if current and when is not None:
            dates.append((when, stamp))
        if not current or amount is None or when is None:
            complete = False
            continue
        value += amount
    when, stamp = min(dates) if dates else (None, None)
    latest, latest_stamp = max(dates) if dates else (None, None)
    return value if complete else None, when, stamp, latest, latest_stamp


def _reserve(row):
    liquidity = row.get("liquidity") or {}
    rao = _decimal(liquidity.get("tao_reserve_rao"))
    tao = _decimal(liquidity.get("tao_reserve_tao"))
    if rao is not None:
        if rao != rao.to_integral_value():
            return None
        exact = rao / Decimal(10**9)
        if tao is not None and tao != exact:
            return None
        return exact
    return tao


def assess_wallet(wallet, contexts=(), *, checked_at, events=(),
                  max_context_age_hours=24, new_subnet_blocks=216000,
                  min_tao_reserve=1000, max_exposure_ratio=.05):
    """Return risk fields without changing wallet P/L or supplied evidence.

    ``contexts`` is a native context document or iterable of dated documents.
    ``events`` contains latest normalized events with transfer receipt origins
    merged into matching transfers. Raw receipt rows and graph edges with
    nested receipts are also accepted. Callers must resolve reclassifications
    before passing events. Missing purchase evidence remains unknown.
    ``qualified_candidate`` means only that these contextual screens passed;
    it does not establish a winning strategy or an executable exit price.
    """
    now = _time(checked_at)
    if now is None:
        raise ValueError("Assessment timestamp must include timezone")
    settings = dict(max_context_age_hours=max_context_age_hours,
                    new_subnet_blocks=new_subnet_blocks,
                    min_tao_reserve=min_tao_reserve,
                    max_exposure_ratio=max_exposure_ratio)
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or v < 0 for v in settings.values()):
        raise ValueError("Risk thresholds must be finite nonnegative numbers")
    max_age = timedelta(hours=max_context_age_hours)
    events = list(events)
    docs = _contexts(contexts, now)
    latest = docs[-1] if docs else None
    latest_rows = {row.get("netuid"): row for row in latest[3].get(
        "subnets", []) if isinstance(row, dict)} if latest else {}
    wallet_stamp = wallet.get("stamp") or wallet.get("checked_at")
    inventory_stamp = wallet.get("account_as_of")
    if _time(inventory_stamp) is None:
        inventory_stamp = wallet_stamp
    wallet_time = _time(inventory_stamp)
    cutoff = min(now, wallet_time) if wallet_time is not None else now
    flags, rows = set(), []
    current = wallet.get("current_inventory") is True
    if not current:
        flags.add("current_inventory_unknown")
    elif wallet_time is None or wallet_time > now:
        flags.add("wallet_time_unknown")
    elif now - wallet_time > max_age:
        flags.add("wallet_snapshot_stale")
    positions = wallet.get("positions", wallet.get("held")) or []
    held = {}
    if current:
        for position in positions:
            netuid, alpha = position.get("netuid"), _decimal(
                position.get("alpha"))
            if (_integer(netuid) is not None and alpha is not None
                    and alpha > 0):
                held.setdefault(netuid, []).append(position)
    for netuid, positions in sorted(held.items()):
        row_flags = set()
        context = latest_rows.get(netuid)
        doc = latest[3] if context is not None else {}
        context = context or {}
        pool_model = (context.get("liquidity") or {}).get("model")
        root = netuid == 0 and pool_model == "root_no_pool"
        context_block = _integer(doc.get("finalized_block"))
        context_time = _time(doc.get("block_time"))
        fresh = context_time is not None and now - context_time <= max_age
        if not context:
            row_flags.add("context_missing")
        elif not fresh:
            row_flags.add("context_stale")
        active = context.get("active")
        invalid = active is False
        lifecycle = str(context.get("lifecycle_status", "")).lower()
        retiring = (context.get("chain_lifecycle_state") in {
            "PendingDissolution", "Dissolving"}
            or lifecycle in {"dissolving", "retiring", "cleanup"}
            or context.get("is_dissolving") is True)
        if retiring:
            row_flags.add("subnet_retiring")
            invalid = True
        if not root and context.get("trading_enabled") is False:
            row_flags.add("trading_disabled")
            invalid = True
        if active is False:
            row_flags.add("subnet_inactive")
        elif active is not True:
            row_flags.add("subnet_status_unknown")
        registered = _integer(context.get("registration_block"))
        generation = context.get("generation_id") or (
            f"finney:{netuid}:registration:{registered}"
            if registered is not None else None)
        counter = _integer(context.get("registered_subnet_counter"))
        counter_inconsistent = context.get(
            "generation_counter_inconsistent") is True
        if not root and counter_inconsistent:
            row_flags.add("generation_counter_inconsistent")
        if not root and counter is not None and counter > 1:
            row_flags.add("subnet_id_reused")
        if not root and (registered is None or not generation):
            row_flags.add("context_generation_unknown")
        age = (context_block - registered if context_block is not None
               and registered is not None and registered <= context_block
               else None)
        if (not root and active is True and age is not None
                and age < new_subnet_blocks):
            row_flags.add("new_subnet")
        immune = context.get("subnet_immune")
        if not root and immune is True:
            row_flags.add("subnet_in_immunity")
        elif not root and immune is not False and active is True:
            row_flags.add("immunity_unknown")
        previous = []
        for _, _, _, earlier in docs[:-1]:
            previous.extend(
                (earlier, old) for old in earlier.get("subnets", [])
                if old.get("netuid") == netuid)
        prior_registrations = {_integer(old.get("registration_block"))
                               for _, old in previous}
        prior_registrations.discard(None)
        changed = not root and (
            (registered is not None and any(
                old != registered for old in prior_registrations))
            or (not prior_registrations
                and context.get("generation_changed") is True))
        if changed:
            row_flags.add("subnet_generation_changed")
        entries = _entry_evidence(wallet, positions, netuid, events, cutoff,
                                  context_block)
        entry_blocks = sorted({entry["block"] for entry in entries})
        current_blocks = sorted({entry["block"] for entry in entries
                                 if entry["source"]
                                 == "current_purchase_origin"})
        if not root and not entry_blocks:
            row_flags.add("entry_generation_unknown")
        if not root and registered is not None:
            if current_blocks and min(current_blocks) < registered:
                row_flags.add("entry_generation_conflict")
                invalid = True
            elif entry_blocks and min(entry_blocks) < registered:
                row_flags.add("historical_generation_overlap")
            relevant = current_blocks or entry_blocks
            if registered in relevant:
                row_flags.add("entry_registration_boundary")
        mark, mark_time, mark_stamp, mark_latest, latest_stamp = _mark(
            positions, wallet_stamp)
        if mark is None:
            row_flags.add("mark_unknown")
        if mark_latest is not None and mark_latest > now:
            row_flags.add("mark_future")
            invalid = True
        elif mark_time is not None and now - mark_time > max_age:
            row_flags.add("mark_stale")
        if not root and mark_time is not None and any(
                registered is not None
                and _integer(old.get("registration_block")) is not None
                and old["registration_block"] != registered
                and _time(earlier.get("block_time")) >= mark_time
                for earlier, old in previous):
            row_flags.add("mark_generation_conflict")
            invalid = True
        reserve = _reserve(context)
        if reserve is None and not root:
            row_flags.add("liquidity_unknown")
        elif (not root and reserve is not None
              and reserve < Decimal(str(min_tao_reserve))):
            row_flags.add("low_tao_reserve")
        compatible = bool(
            fresh and current and active is True and not invalid
            and not counter_inconsistent
            and "entry_registration_boundary" not in row_flags
            and not ("historical_generation_overlap" in row_flags
                     and not current_blocks)
            and (root or (registered is not None and generation
                          and entry_blocks))
            and mark is not None and mark_latest <= now
            and now - mark_time <= max_age
            and wallet_time is not None and wallet_time <= now
            and now - wallet_time <= max_age)
        ratio = (mark / reserve if not root and compatible
                 and reserve is not None
                 and reserve > 0 else None)
        if ratio is not None and ratio > Decimal(str(max_exposure_ratio)):
            row_flags.add("high_pool_exposure")
        row = dict(
            netuid=netuid, flags=sorted(row_flags),
            reasons=[_REASONS[flag] for flag in sorted(row_flags)],
            active=active, registration_block=registered,
            trading_enabled=context.get("trading_enabled"),
            chain_lifecycle_state=context.get("chain_lifecycle_state"),
            is_dissolving=retiring,
            generation_id=generation, registered_subnet_counter=counter,
            generation_counter_inconsistent=counter_inconsistent,
            age_blocks=age,
            immunity_remaining_blocks=context.get(
                "subnet_immunity_remaining_blocks"),
            in_immunity=immune, generation_changed=changed,
            early_entry_observed=bool(registered is not None and any(
                registered < block < registered + new_subnet_blocks
                for block in entry_blocks)),
            entry_blocks=entry_blocks, current_entry_blocks=current_blocks,
            entry_evidence=entries,
            context_checked_at=doc.get("checked_at"),
            context_block=context_block,
            context_block_time=doc.get("block_time"),
            mark_as_of=mark_stamp, mark_latest_as_of=latest_stamp,
            mark_compatible=compatible,
            held_marked_value_tao=(float(mark) if mark is not None
                                   and "mark_future" not in row_flags
                                   else None),
            tao_reserve_tao=(format(reserve, "f") if reserve is not None
                             else None),
            exposure_ratio=float(ratio) if ratio is not None else None,
            liquidity_model=pool_model, liquidity_applicable=not root,
            exit_quote_tao=None,
            invalid_current_valuation=invalid,
            provenance=dict(source=doc.get("source"),
                            block_hash=doc.get("block_hash")))
        rows.append(row)
        flags.update(row_flags)
    status = ("flagged" if flags - _UNKNOWN else "unknown" if flags
              else "no_flags_observed")
    return dict(
        address=wallet.get("address"), status=status, assessed_at=checked_at,
        wallet_as_of=wallet_stamp, inventory_as_of=inventory_stamp,
        flags=sorted(flags),
        reasons=[_REASONS[flag] for flag in sorted(flags)],
        candidate_visible=True, provisional=bool(flags),
        qualified_candidate=not flags,
        invalid_current_valuation=any(r["invalid_current_valuation"]
                                      for r in rows),
        subnets=rows, settings=settings,
        qualification_interpretation="Passes these contextual screens only; "
                                     "not proof of a winning strategy or "
                                     "executable exit value.",
        exposure_interpretation="Marked value / TAO reserve; not slippage "
                                "or an executable exit quote.")


def assess_wallets(wallets, contexts=(), *, checked_at, events=(), **settings):
    """Return address -> risk for raw profiles or directory wallet mappings."""
    profiles = wallets.values() if isinstance(wallets, dict) else wallets
    contexts = contexts if isinstance(contexts, dict) else list(contexts)
    events = list(events)
    return {wallet["address"]: assess_wallet(
        wallet, contexts, checked_at=checked_at, events=events, **settings)
        for wallet in profiles}
