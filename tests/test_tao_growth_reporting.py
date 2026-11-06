"""Preserve original entry and separate staking growth from trading gains."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from seo import storage
from tests.test_tao_ledger import day, event
from tests.test_tao_performance import COMPLETE, price, record
from tests.test_tao_performance_cli import run


def proof(address="A", before=10., after=30.):
    return dict(id="growth", address=address, netuid=8, start_time=day(3),
                time=day(4), before_alpha=before, after_alpha=after,
                evidence="verified_stake_growth")


def test_native_rounding_preserves_original_entry_in_report(tmp_path):
    events = [event(1, 'buy', None, 'A', 10, 10),
              dict(event(2, 'stake_reallocation', 'A', 'A', 10),
                   evidence='native_move', received_alpha=10 - 1e-9,
                   rounding_loss_alpha=1e-9)]
    result = record(tmp_path, events, [price(8, 1, 4)], day(4),
                    {'A': COMPLETE})
    position = result['wallets'][0]['positions'][0]
    assert position['known_buy_price_tao'] == 1
    assert position['known_cost_tao'] == 10


def test_transferred_growth_preserves_entry_and_price_gain(tmp_path):
    events = [event(1, "buy", None, "A", 10, 10),
              event(5, "stake_transfer", "A", "B", 15)]
    result = record(tmp_path, events, [price(8, .5, 6)], day(6),
                    {"A": COMPLETE, "B": COMPLETE}, growth_events=[proof()])
    b = next(w for w in result["wallets"] if w["address"] == "B")
    pos = b["positions"][0]
    assert pos["known_buy_price_tao"] == pos["buy_price_tao"] == 1.
    assert pos["known_effective_cost_per_alpha_tao"] == pytest.approx(1 / 3)
    assert pos["known_cost_tao"] == 5.
    assert pos["known_growth_alpha"] == 10.
    assert pos["growth_value_tao"] == pos["known_growth_value_tao"] == 5.
    assert pos["known_trading_unrealized_tao"] == -2.5
    assert pos["known_unrealized_tao"] == pos["gross_unrealized_tao"] == 2.5
    assert b["open"]["known_growth_value_tao"] == 5.
    assert b["open"]["known_trading_pnl_tao"] == -2.5
    assert b["gross_total_pnl_tao"] == 2.5


def test_sold_growth_is_economic_profit_without_trading_wins(tmp_path, capsys):
    events = [event(1, "buy", None, "A", 10, 10),
              event(5, "sell", "A", None, 30, 15)]
    result = record(tmp_path, events, [price(8, .5, 6)], day(6),
                    {"A": COMPLETE}, growth_events=[proof()], min_sales=1)
    wallet = result["wallets"][0]
    assert wallet["gross_total_pnl_tao"] == 5.
    assert wallet["economic_realized_tao"] == 5.
    assert wallet["known_realized_tao"] == 5.
    assert wallet["known_trading_realized_tao"] == -5.
    assert wallet["growth_proceeds_tao"] == 10.
    assert wallet["priced_sales"] == wallet["wins"] == 0
    assert wallet["unpriced_sales"] == 0
    assert wallet["win_rate"] is None
    assert result["rankings"]["profit"] == []
    store = storage.Storage(str(tmp_path / "wallets.sqlite"))
    try:
        assert run(store, "A") == 0
        text = capsys.readouterr().out
        assert "No sales observed" not in text
        assert "Economic realized +5 TAO" in text
        assert "trading price gain -5 TAO" in text
        assert "growth proceeds 10 TAO" in text
    finally:
        store.close()


def test_proven_growth_does_not_make_unknown_acquisition_cost_known(tmp_path):
    events = [event(1, "stake_transfer", None, "A", 10)]
    result = record(tmp_path, events, [price(8, .5, 6)], day(6),
                    {"A": COMPLETE}, growth_events=[proof()])
    wallet = result["wallets"][0]
    pos = wallet["positions"][0]
    assert pos["growth_value_tao"] == 10.
    assert pos["known_buy_price_tao"] is None
    assert pos["known_effective_cost_per_alpha_tao"] is None
    assert pos["known_trading_unrealized_tao"] is None
    assert pos["known_growth_value_tao"] is None
    assert wallet["gross_total_pnl_tao"] is None


def test_partial_sale_rank_excludes_growth_from_trading_gain(tmp_path):
    events = [event(1, "buy", None, "A", 10, 10),
              event(2, "stake_transfer", None, "A", 10),
              event(5, "sell", "A", None, 60, 30)]
    result = record(tmp_path, events, [price(8, .5, 6)], day(6),
                    {"A": COMPLETE},
                    growth_events=[proof(before=20, after=60)],
                    min_sales=1)
    wallet = result["wallets"][0]
    assert wallet["known_realized_tao"] == 5.
    assert wallet["known_sale_return_pct"] == -50.
    assert wallet["economic_realized_tao"] is None
    assert wallet["gross_total_pnl_tao"] is None
    assert result["rankings"]["profit_partial"] == []


def test_cli_distinguishes_original_entry_and_effective_cost(tmp_path, capsys):
    record(tmp_path, [event(1, "buy", None, "A", 10, 10)],
           [price(8, .5, 6)], day(6), {"A": COMPLETE}, growth_events=[proof()])
    store = storage.Storage(str(tmp_path / "wallets.sqlite"))
    try:
        assert run(store, "A") == 0
        text = capsys.readouterr().out
        assert "original entry 1" in text
        assert "effective cost 0.333333333" in text
        assert "proven growth 20 alpha" in text
        assert "growth value 10 TAO" in text
        assert "trading price gain -5 TAO" in text
    finally:
        store.close()


def test_viewer_shows_growth_and_retains_source_snapshot_date(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("viewer test needs Node")
    events = [event(1, "buy", None, "A", 10, 10),
              event(5, "sell", "A", None, 15, 7.5)]
    performance = record(tmp_path, events, [price(8, .5, 6)], day(6),
                         {"A": COMPLETE}, growth_events=[proof()])
    report = dict(checked_at=day(6), coverage={"A": COMPLETE}, events=events,
                  performance=performance, local_evidence=dict(
                      verified_at=day(8), source_snapshot=day(6),
                      growth_intervals=1,
                      scope="Verified stake-share intervals"))
    code = r"""
const fs = require('node:fs'), vm = require('node:vm');
const assert = require('node:assert/strict');
const report = JSON.parse(fs.readFileSync(0, 'utf8'));
const template = fs.readFileSync('seo/wallet_view.html', 'utf8');
const source = template.match(/<script>([\s\S]*?)<\/script>/)[1];
class Element {
  constructor() { this.text = ''; this.children = []; this.value = ''; }
  set textContent(v) { this.text = String(v); this.children = []; }
  get textContent() {
    return this.text + this.children.map(c => c.textContent ?? c).join(' ');
  }
  get childNodes() { return this.children; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; this.text = ''; }
  setAttribute(name, value) { this[name] = value; }
}
function render(data) {
  const nodes = new Map(), id = name => {
    if (!nodes.has(name)) nodes.set(name, new Element());
    return nodes.get(name);
  };
  id('report').text = JSON.stringify(data);
  id('board').value = 'all'; id('move').value = 'scan';
  const context = {Node:Element, location:{hash:''}, setInterval:()=>{},
    document:{getElementById:id,createElement:()=>new Element()}};
  vm.runInNewContext(source, context); context.show('A', false);
  return id;
}
const page = render(report), detail = page('detail').textContent;
assert.match(detail, /Original entry/);
assert.match(detail, /Effective cost/);
assert.match(detail, /Proven growth/);
assert.match(detail, /Economic realized on known units: 2\.5 TAO/);
assert.match(detail, /trading price gain on known units: -2\.5 TAO/);
assert.match(detail, /1 sales containing growth/);
assert.match(page('localEvidence').textContent,
             /Local native accounting replay/);
assert.match(page('localEvidence').textContent, /2026-10-08/);
assert.match(page('stamp').textContent, /2026-10-06/);
assert.doesNotMatch(page('stamp').textContent, /2026-10-08/);
delete report.local_evidence;
for (const wallet of report.performance.wallets) {
  for (const row of [wallet, ...wallet.positions]) {
    for (const key of Object.keys(row)) {
      if (/growth|trading|effective/.test(key)) delete row[key];
    }
  }
}
assert.doesNotThrow(() => render(report));
"""
    subprocess.run([node, "-e", code], input=json.dumps(report), text=True,
                   cwd=Path(__file__).resolve().parents[1],
                   check=True, timeout=30)
