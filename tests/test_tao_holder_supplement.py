from types import SimpleNamespace
import sqlite3

from seo import tao_collect
from seo.sources import tao_holders


def test_supplement_uses_persistent_queue_without_rescanning_market(
        monkeypatch):
    from seo.sources import tao_holder_registry
    from seo import tao_holder_queue

    args = SimpleNamespace(netuids="8,23", addresses_only=False,
                           holders_only=True, holder_pages=4, top=2)
    sources = tao_collect.defaultdict(set)
    a, b, c, d = ("5" + ch * 47 for ch in "ABCD")
    monkeypatch.setattr(tao_collect.taostats, "fetch_subnets",
                        lambda _: [SimpleNamespace(netuid=8)])
    monkeypatch.setattr(tao_holders, "fetch_holders", lambda *a, **k:
                        (_ for _ in ()).throw(AssertionError("old sample")))
    db = sqlite3.connect(":memory:")
    tao_holder_registry.schema(db)
    tao_holder_queue.schema(db)
    calls = []

    def capture(session, conn, n, stamp, pages):
        calls.append((n, pages))
        for rank, address in enumerate({8: [a, b], 23: [c, d]}[n], 1):
            conn.execute("INSERT OR IGNORE INTO holder_registry VALUES "
                         "(?,?,?,?,?,?)", (n, address, rank, "100", stamp,
                                           stamp))
        return dict(status="partial", captured=2, next_page=5)

    monkeypatch.setattr(tao_holder_registry, "collect_pages", capture)
    store = SimpleNamespace(save=lambda rows, stamp: None, conn=db)
    graph, holders = {}, {}
    found, market = tao_collect._discover(
        args, None, store, "2026-11-06T00:00:00+00:00", graph, holders,
        sources)
    assert list(found) == [b, d]
    assert graph == {} and market == "skipped"
    assert calls == [(8, 4), (23, 4)]
    assert holders[8]["captured"] == 2
    assert sources[b] == {"Taostats holders: SN8"}
    db.close()
