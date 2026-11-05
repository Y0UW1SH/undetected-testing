"""taostats.io wallet data, read from pages the stealth browser loads.

taostats.io is a Next.js app that server-renders its React Query cache
into the page (`self.__next_f.push([1, "..."])` chunks). Two pages carry
what we need without the Cloudflare check that guards the /accounts
list:

  /subnets/<netuid>/metagraph   all 256 neurons of a subnet, each with
                                its coldkey and daily rewards in TAO
  /account/<coldkey>            the wallet's balances now and 24h ago

Metagraphs identify earners. The collector reads each selected account
and checks its transaction and transfer tabs for trading evidence.
The /accounts leaderboard itself loads only after a human check, so it
is not used, and nothing here tries to get past such a check."""
import json
import re

from seo import models
from seo.sources.taostats import is_blocked

BASE = "https://taostats.io"
RAO_PER_TAO = 1e9
PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


def account_url(address):
    return "%s/account/%s" % (BASE, address)


def metagraph_url(netuid):
    return "%s/subnets/%d/metagraph" % (BASE, int(netuid))


def rao_to_tao(value):
    if value in (None, ""):
        return None
    try:
        return int(str(value)) / RAO_PER_TAO
    except ValueError:
        try:
            return float(value) / RAO_PER_TAO
        except ValueError:
            return None


def _ss58(value):
    if isinstance(value, dict):
        return value.get("ss58") or value.get("hex")
    return value


# ---- Next.js payload ------------------------------------------------------
def flight_text(html):
    """Concatenate the server-rendered flight chunks of a Next.js page."""
    parts = []
    for m in PUSH_RE.finditer(html or ""):
        try:
            parts.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return "".join(parts)


def queries(html):
    """[(query_key, data)] for every dehydrated React Query in the page
    whose fetch succeeded."""
    text = flight_text(html)
    decoder = json.JSONDecoder()
    out = []
    pos = 0
    while True:
        start = text.find('{"dehydratedAt":', pos)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            pos = start + 1
            continue
        pos = end
        state = obj.get("state") or {}
        if state.get("status") == "success" and "queryKey" in obj:
            out.append((obj["queryKey"], state.get("data")))
    return out


def find_query(html, name):
    """Data of the first query whose key starts with `name`."""
    for key, data in queries(html):
        if isinstance(key, list) and key and key[0] == name:
            return data
    return None


def _rows(data):
    if isinstance(data, dict):
        rows = data.get("data")
        if isinstance(rows, dict):
            return [rows]
        return rows or []
    return data or []


# ---- parsers --------------------------------------------------------------
def parse_metagraph(html, netuid=None):
    """Neurons of one subnet: dicts with netuid, uid, coldkey, hotkey,
    name, daily_rewards_tao, validator."""
    out = []
    for r in _rows(find_query(html, "metagraph")):
        coldkey = _ss58(r.get("coldkey"))
        if not coldkey:
            continue
        rewards = rao_to_tao(r.get("daily_total_rewards_as_tao"))
        out.append(dict(
            netuid=r.get("netuid", netuid), uid=r.get("uid"),
            coldkey=coldkey, hotkey=_ss58(r.get("hotkey")),
            name=r.get("name"), daily_rewards_tao=rewards or 0.0,
            validator=bool(r.get("validator_permit"))))
    return out


def parse_account(html, address=None, source="scan"):
    """WalletSnapshot from an /account/<address> page, or None."""
    rows = _rows(find_query(html, "account"))
    if not rows:
        return None
    return _snapshot(rows[0], address, source)


def _snapshot(r, address, source):
    me = _ss58(r.get("address")) or address
    swap = r.get("coldkey_swap") or {}
    swapped_to = None
    if swap and _ss58(swap.get("old_coldkey")) == me:
        swapped_to = _ss58(swap.get("new_coldkey"))
    return models.WalletSnapshot(
        address=me, rank=r.get("rank"), swapped_to=swapped_to,
        positions_complete=isinstance(r.get("alpha_balances"), list) and all(
            isinstance(b, dict) and b.get("netuid") is not None
            and rao_to_tao(b.get("balance")) is not None
            for b in r["alpha_balances"]),
        balance_free=rao_to_tao(r.get("balance_free")),
        balance_staked=rao_to_tao(r.get("balance_staked")),
        balance_staked_root=rao_to_tao(r.get("balance_staked_root")),
        balance_staked_alpha=rao_to_tao(
            r.get("balance_staked_alpha_as_tao")),
        balance_total=rao_to_tao(r.get("balance_total")),
        balance_total_24h_ago=rao_to_tao(r.get("balance_total_24hr_ago")),
        block_number=r.get("block_number"), snapshot_at=r.get("timestamp"),
        source=source, source_url=account_url(
            _ss58(r.get("address")) or address))


def parse_positions(html, address=None):
    """WalletPositions (one per subnet + hotkey) from an account page,
    merging the current alpha balances with the 24h-ago ones."""
    rows = _rows(find_query(html, "account"))
    if not rows:
        return []
    return _positions(rows[0], address)


def _positions(r, address):
    address = _ss58(r.get("address")) or address
    merged = {}
    for field, suffix in (("alpha_balances", ""),
                          ("alpha_balances_24hr_ago", "_24h_ago")):
        for b in r.get(field) or []:
            if not isinstance(b, dict) or b.get("netuid") is None:
                continue
            key = (b.get("netuid"), _ss58(b.get("hotkey")))
            pos = merged.setdefault(key, models.WalletPosition(
                address=address, netuid=key[0], hotkey=key[1],
                snapshot_at=r.get("timestamp")))
            setattr(pos, "alpha" + suffix, rao_to_tao(b.get("balance")))
            setattr(pos, "alpha_as_tao" + suffix,
                    rao_to_tao(b.get("balance_as_tao")))
    return [merged[k] for k in sorted(merged, key=lambda k: (k[0] or 0,
                                                             k[1] or ""))]


def earners(neurons):
    """Aggregate neurons by coldkey: {coldkey: {rewards_per_day,
    subnets, neurons, names}}."""
    out = {}
    for n in neurons:
        e = out.setdefault(n["coldkey"], dict(
            coldkey=n["coldkey"], rewards_per_day=0.0, subnets=set(),
            neurons=0, names=set()))
        e["rewards_per_day"] += n["daily_rewards_tao"] or 0.0
        e["subnets"].add(n["netuid"])
        e["neurons"] += 1
        if n.get("name"):
            e["names"].add(n["name"])
    return out


# ---- fetching -------------------------------------------------------------
def _load(session, url):
    """Load a page and return its HTML. A full-page bot wall raises
    ChallengeError; this never tries to solve a challenge."""
    session.goto(url, check_google=False)
    html = session.html()
    reason = is_blocked(session.url(), session.title(), html)
    if reason and not flight_text(html):
        raise models.ChallengeError("%s at %s" % (reason, url))
    return html


def fetch_metagraph(session, netuid):
    return parse_metagraph(_load(session, metagraph_url(netuid)), netuid)


def fetch_account(session, address, source="scan"):
    """(WalletSnapshot or None, [WalletPosition]) for one wallet."""
    html = _load(session, account_url(address))
    rows = _rows(find_query(html, "account"))
    if not rows:
        return None, []
    r = rows[0]
    return _snapshot(r, address, source), _positions(r, address)
