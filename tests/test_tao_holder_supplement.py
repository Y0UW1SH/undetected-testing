from types import SimpleNamespace

from seo import tao_collect
from seo.sources import tao_holders


def test_supplement_collects_holders_without_rescanning_market(monkeypatch):
    # Holder ranks 2+ on both subnets precede the single largest accounts.
    args = SimpleNamespace(netuids="8,23", addresses_only=False,
                           holders_only=True, top=2)
    sources = tao_collect.defaultdict(set)
    a, b, c, d = ("5" + ch * 47 for ch in "ABCD")
    monkeypatch.setattr(tao_collect.taostats, "fetch_subnets",
                        lambda _: [SimpleNamespace(netuid=8)])
    monkeypatch.setattr(tao_holders, "fetch_holders", lambda _, n, **k: (
        {8: [a, b], 23: [c, d]}[n], "partial"))
    store = SimpleNamespace(save=lambda rows, stamp: None)
    graph, holders = {}, {}
    found, market = tao_collect._discover(args, None, store, "now", graph,
                                          holders, sources)
    assert list(found) == [b, d]
    assert graph == {} and market == "skipped"
    assert holders[8]["sampled_wallets"] == [a, b]
    assert sources[b] == {"Taostats holders: SN8"}
