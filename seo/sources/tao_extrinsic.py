"""Read user-selected Taostats extrinsic and event detail pages in Chrome.

These pages embed native call details and event fields in the rendered HTML.
A block event provided by index is independent evidence. The event page does
not identify its containing extrinsic, and call arguments are not fill data.
"""
import json
import re
from decimal import Decimal
from collections import Counter

from bs4 import BeautifulSoup

from seo.sources.taostats_wallets import BASE, flight_text

TARGET = re.compile(r"([0-9]{1,10})-([0-9]{1,5})(?::(.*))?")
COMPONENT_DATA = re.compile(r'\["\$","\$L[0-9a-f]+",null,\{"data":')
MAX_EVENT_INDICES = 8


def confirms_reallocation(detail, paired_sells):
    """Verify all paired quantities against a successful, same-subnet move.

    A remark can accompany a batch; a mixed batch, different asset, or
    unaccounted-for inner call is not proof that all pairs preserved cost.
    """
    if (detail.get("status") not in ("observed", "partial")
            or detail.get("success") is not True):
        return False
    args = detail.get("call_args")
    if not isinstance(args, dict):
        return False
    if detail.get("call") == "SubtensorModule.move_stake":
        moves = [args]
    elif detail.get("call") == "Utility.batch_all":
        moves = []
        for child in args.get("calls") or []:
            if not isinstance(child, dict) or len(child) != 1:
                return False
            system = child.get("System")
            if isinstance(system, dict) and set(system) in (
                    {"remark"}, {"remark_with_event"}):
                continue
            calls = child.get("SubtensorModule")
            if not isinstance(calls, dict) or set(calls) != {"move_stake"}:
                return False
            moves.append(calls["move_stake"])
    else:
        return False
    try:
        chain = Counter()
        for m in moves:
            net = int(m["origin_netuid"])
            if net != int(m["destination_netuid"]):
                return False
            chain[detail["signer"], net, int(m["alpha_amount"])] += 1
        paired = Counter((e["src"], e["netuid"], round(e["alpha"] * 10**9))
                         for e in paired_sells)
        return bool(paired) and paired == chain
    except (TypeError, ValueError, KeyError, OverflowError):
        return False


def parse_target(text):
    """BLOCK-EXTRINSIC optionally followed by :EVENT,EVENT in that block."""
    m = TARGET.fullmatch(text)
    if not m:
        raise ValueError("expected BLOCK-EXTRINSIC[:EVENT,EVENT]")
    indices = ()
    if m[3] is not None:
        parts = m[3].split(",")
        if len(parts) > MAX_EVENT_INDICES or any(
                not re.fullmatch(r"[0-9]{1,5}", p) for p in parts):
            raise ValueError("use up to 8 numeric event indices per extrinsic")
        indices = tuple(dict.fromkeys(map(int, parts)))
    return "%d-%04d" % (int(m[1]), int(m[2])), indices


def _component_data(html):
    flight = flight_text(html)
    decoder = json.JSONDecoder()
    for match in COMPONENT_DATA.finditer(flight):
        try:
            data, _ = decoder.raw_decode(flight, match.end())
        except ValueError:
            continue
        yield data


def _stake(name, fields):
    if (name not in ("StakeAdded", "StakeRemoved")
            or not isinstance(fields, list) or len(fields) != 6):
        return None
    owner, hotkey, tao, alpha, netuid, fee = fields
    if (not isinstance(owner, str) or not isinstance(hotkey, str)
            or not all(type(n) is int and n >= 0
                       for n in (tao, alpha, netuid, fee))):
        return None
    return dict(owner=owner, hotkey=hotkey, netuid=netuid,
                tao=str(Decimal(tao) / 10**9),
                alpha=str(Decimal(alpha) / 10**9), fee_base_units=str(fee))


def _event_page(session, eid):
    url = BASE + "/event/" + eid
    session.pause()
    session.goto(url, check_google=False)
    html = session.html()
    soup = BeautifulSoup(html, "html.parser")
    label = next((p for p in soup.find_all("p")
                  if p.get_text("", strip=True) == "#" + eid), None)
    fields = list(_component_data(html))
    result = dict(event_id=eid, url=url, status="unavailable")
    if label is None or len(fields) != 1:
        return result
    title = label.parent.parent.find("p", recursive=False)
    if not title or title is label:
        return result
    name = title.get_text(strip=True)
    result.update(status="observed", name=name, raw_fields=fields[0])
    stake = _stake(name, fields[0])
    if stake is not None:
        result["stake"] = stake
    return result


def fetch_evidence(session, extrinsic_id, event_indices=()):
    """Read one call and separately requested events in its block."""
    eid, embedded = parse_target(extrinsic_id)
    if embedded:
        raise ValueError("pass event indices as a separate argument")
    indices = tuple(event_indices)
    if (len(indices) > MAX_EVENT_INDICES or any(
            type(i) is not int or not 0 <= i <= 99999 for i in indices)):
        raise ValueError("use up to 8 numeric event indices per extrinsic")
    block, number = map(int, eid.split("-"))
    url = BASE + "/extrinsic/" + eid
    session.pause()
    session.goto(url, check_google=False)
    result = dict(extrinsic_id=eid, url=url, status="unavailable",
                  requested_block_events=[])
    for record in _component_data(session.html()):
        if (not isinstance(record, dict) or record.get("id") != eid
                or record.get("block_number") != block
                or record.get("index") != number
                or not isinstance(record.get("success"), bool)
                or not record.get("full_name") or "call_args" not in record):
            continue
        result.update(status="observed", block=block, index=number,
                      call=record["full_name"], call_args=record["call_args"],
                      success=record["success"], signer=record.get(
                          "signer_address"), time=record.get("timestamp"),
                      hash=record.get("hash"), fee_rao=record.get("fee"))
        break
    if result["status"] == "observed":
        for index in dict.fromkeys(indices):
            event = _event_page(session, "%d-%04d" % (block, index))
            result["requested_block_events"].append(event)
            if event["status"] == "unavailable":
                result["status"] = "partial"
    if indices:
        result["event_association"] = (
            "User-specified event indices in the same block. The event detail "
            "pages provide no containing extrinsic ID.")
    return result
