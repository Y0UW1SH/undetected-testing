const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../seo/wallet_view.html'), 'utf8');
const source = template.match(/<script>([\s\S]*?)<\/script>/)[1];

// Enough of the DOM to run the standalone viewer's real handlers in Node.
class Element {
  constructor(text = '') { this.text = text; this.children = []; this.value = ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(c => c.textContent ?? c).join(' '); }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; this.text = ''; }
  setAttribute(name, value) { this[name] = value; }
  focus() {}
  scrollIntoView() {}
  findTitle(title) {
    if (this.title === title) return this;
    return this.children.map(c => c.findTitle?.(title)).find(Boolean);
  }
}

function view(report) {
  const nodes = new Map();
  const id = name => {
    if (!nodes.has(name)) nodes.set(name, new Element());
    return nodes.get(name);
  };
  id('report').text = JSON.stringify(report);
  id('board').value = 'all';
  id('move').value = 'scan';
  const context = { Node: Element, location: {hash:''}, setInterval: () => {},
    document: {getElementById: id, createElement: () => new Element()} };
  vm.runInNewContext(source, context);
  function select(side, threshold) {
    id('board').value = side;
    id('board').onchange();
    if (threshold) { id('move').value = threshold; id('move').onchange(); }
    return { text: id('results').textContent, rule: id('rule').textContent,
      disabled: id('move').disabled, hidden: id('moveControl').hidden };
  }
  return {select, id};
}

function fixture() {
  const matches = [];
  const wallets = [];
  const stamp = '2026-11-05T00:00:00Z';
  function add(address, side, percents, mature, screens = [], days = false, gaps = []) {
    const empty = {mature_trades:0,evaluated:0,favorable:0,favorable_days:0};
    const timing = {buy:{...empty},sell:{...empty}};
    timing[side] = {...empty, mature_trades:mature, evaluated:percents.length};
    wallets.push({address, screens, evidence_gaps:gaps, snapshot_recent:true,
      timing, positions:[], ranks:{}, coverage:{account:'complete'}});
    percents.forEach((pct,i) => {
      const day = days ? 1 : 1 + i % 3;
      matches.push({id:address + i, address, kind:side, netuid:64,
        trade_time:'2026-10-0'+day+'T00:00:00Z', quote_time:'2026-10-0'+(day+1)+'T00:00:00Z',
        fill_price_tao:1, price_tao:1+(side === 'buy' ? 1 : -1)*pct/100,
        market_move:pct === 0 ? 'flat' : (pct>0) === (side==='buy') ? 'rise' : 'fall',
        favorable_move_pct: Number(pct.toFixed(5))});
    });
  }
  add('Buyer', 'buy', [2,2.1,3,4,2,1,0,-1], 10);       // 5/8 at 2%; 6/8 positive
  add('Seller', 'sell', [.3,.4,.7,1,.1,1.1,-1,0], 8);
  add('TooFew', 'buy', [3,3,3,3,3,3,3], 7);
  add('OneDay', 'buy', [3,3,3,3,3,3,3,3], 8, [], true);
  add('NoCoverage','buy',[3,3,3,3,3,3,3,3],17);
  add('Screened','buy',[3,3,3,3,3,3,3,3],8,['balance_above_limit']);
  add('Missing','buy',[3,3,3,3,3,3,3,3],8,[],false,['account_unavailable']);
  return {checked_at:stamp, coverage:Object.fromEntries(wallets.map(w=>[w.address,w.coverage])),
    following:[],events:[],transfers:[],performance:{settings:{move_pct:5,min_timing:3},
      wallets,rankings:{buy:[],sell:[]}}, timing_evidence:{as_of:stamp, matches,
      matched_trades:matches.length,match_details_omitted:0}};
}

function runTests() {
  const report = fixture();
  const page = view(report);
  assert.equal(page.select('all').hidden, true);
  assert.match(page.select('buy','scan').text, /No wallets/);
  const buy = page.select('buy','2');
  assert.match(buy.text, /Buyer/);
  assert.match(buy.text, /62.5%/);
  assert.doesNotMatch(buy.text, /TooFew|OneDay|NoCoverage|Screened|Missing/);
  assert.match(page.select('buy','any').text, /75%/);
  page.id('results').findTitle('Buyer').onclick();
  assert.match(page.id('detail').textContent, /buy timing uses any rise/);
  assert.match(page.id('detail').textContent, /75% \(6\/8\)/);
  assert.match(page.select('sell','2').text, /No wallets/);
  assert.match(page.select('sell','any').text, /Seller/);
  assert.match(page.select('all').text, /Buyer/);
  // A capped or mismatched report must still display its original rankings.
  for (const change of [r=>delete r.timing_evidence,
                        r=>r.timing_evidence.match_details_omitted=1,
                        r=>r.performance.wallets[0].timing.buy.evaluated++]) {
    const partial = fixture();
    change(partial);
    partial.performance.rankings.buy = ['Buyer'];
    const result = view(partial).select('buy','any');
    assert.equal(result.disabled, true);
    assert.match(result.text, /Buyer/);
    assert.match(result.rule, /saved scan/i);
  }
}

if (process.argv[2]) {
  const report = JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
  const page = view(report);
  for (const side of ['buy','sell']) for (const threshold of ['scan','2','any']) {
    const row = page.select(side,threshold);
    console.log(side,threshold,row.disabled,row.text.slice(0,1500));
  }
} else {
  runTests();
}
