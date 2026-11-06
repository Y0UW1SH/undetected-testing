"""Build a searchable offline view of Taostats' saved Accounts directory."""

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from functools import lru_cache
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

from seo.tao_view import _report_date, run_snapshot


def _json(path):
    with Path(path).open(encoding="utf8") as f:
        return json.load(f)


def _new():
    return dict(fills=0, buys=0, sells=0, examples=[], trade_subnets=[],
                subnets=[], screens=[], gaps=[], ranks={}, priced_sales=0,
                unpriced_sales=0, known_realized=None, known_open=None,
                known_value_fraction=None, unresolved_proceeds=None,
                screen_status="unassessed", win_rate=None,
                current_inventory=False, balance_tao=None,
                balance_fetched_at=None, timing={},
                stamp=None, coverage={}, in_directory=False,
                accounting_source=None)


def _screen_status(wallet, coverage, settings, stamp):
    if wallet.get("screens"):
        return "excluded"
    if (isinstance(wallet.get("screens"), list)
            and (settings.get("max_balance_tao") or 0) > 0
            and (settings.get("max_activity_24h") or 0) > 0
            and wallet.get("balance_tao") is not None
            and wallet.get("balance_fetched_at") == stamp
            and coverage.get("account") == "complete"
            and wallet.get("activity_24h_complete") is True):
        return "assessed"
    return "unassessed"


def _open(wallet, positions):
    if not wallet.get("current_inventory"):
        return None, None
    evidence = wallet.get("open") or {}
    pnl = evidence.get("known_pnl_tao")
    fraction = evidence.get("known_value_fraction")
    # Older reports and incomplete subnet marks can still price specific lots.
    if pnl is None:
        marked = [p["known_unrealized_tao"] for p in positions
                  if p.get("known_unrealized_tao") is not None
                  and p.get("current_quote") and (p.get("known_alpha") or 0)]
        pnl = sum(marked) if marked else wallet.get("gross_unrealized_tao")
    return pnl, fraction


@lru_cache(maxsize=128)
def _identity(path, modified, size):
    raw = path.read_bytes()
    data = json.loads(raw)
    _report_date(data)
    return (data['checked_at'], hashlib.sha256(raw).hexdigest(),
            data.get('local_evidence') or {})


def _file_identity(path):
    stat = path.stat()
    return _identity(path, stat.st_mtime_ns, stat.st_size)


def _cached(cache):
    if cache is None:
        return set()
    paths = set()
    try:
        folders = list(Path(cache).iterdir())
    except OSError:
        print("Wallet cache is unavailable; using explicit reports only.",
              file=sys.stderr)
        return paths
    for folder in folders:
        if not folder.name.isascii() or not folder.name.isdigit():
            continue
        try:
            path = run_snapshot(folder)
            if path is not None:
                paths.add(path.resolve())
        except (OSError, ValueError):
            continue
    # Local corrections keep the source observation date. Only use them while
    # their exact downloaded source artifact remains available and unchanged.
    sources = {}
    for path in paths:
        try:
            stamp, digest, _ = _file_identity(path)
            sources[digest] = stamp
        except (OSError, ValueError, TypeError):
            continue
    for folder in folders:
        if not re.fullmatch(r'local-native(?:-[a-z0-9_-]+)?', folder.name):
            continue
        path = folder / 'tao_collect.json'
        try:
            stamp, _, evidence = _file_identity(path)
            source_stamp = sources.get(evidence.get('source_report_sha256'))
            if stamp == source_stamp == evidence.get('source_snapshot'):
                paths.add(path.resolve())
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return paths


def _research(paths, cached):
    wallets, events, updated, snapshots = {}, {}, {}, set()
    explicit = {Path(path).resolve() for path in paths}
    for path in sorted(explicit | cached):
        try:
            data = _json(path)
        except (OSError, ValueError):
            if path in explicit:
                raise
            print("Skipped a cached wallet report that became unavailable.",
                  file=sys.stderr)
            continue
        stamp = data["checked_at"]
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError("Wallet report timestamp must include timezone")
        priority = when, bool(data.get('local_evidence'))
        snapshots.add(stamp)
        coverage = data.get("coverage") or {}
        performance = data.get("performance") or {}
        for wallet in performance.get("wallets", []):
            a = wallet["address"]
            if a not in coverage or priority < updated.get(a, priority):
                continue
            updated[a] = priority
            w = wallets.setdefault(a, _new())
            positions = wallet.get("positions") or []
            subnets = sorted({p["netuid"] for p in positions
                              if wallet.get("current_inventory")
                              and (p.get("alpha") or 0) > 0
                              and (p.get("netuid") or 0) > 0})
            known = wallet.get("known_realized_tao")
            if known is None and wallet.get("priced_sales"):
                known = wallet.get("realized_tao")
            opened, fraction = _open(wallet, positions)
            screen = _screen_status(wallet, coverage[a],
                                    performance.get("settings") or {}, stamp)
            position_fields = ("netuid", "alpha", "known_alpha",
                               "probable_alpha", "unknown_alpha",
                               "known_buy_price_tao", "price_tao",
                               "known_unrealized_tao", "current_quote",
                               "growth_alpha", "known_growth_value_tao",
                               "known_effective_cost_per_alpha_tao")
            held = [{k: p.get(k) for k in position_fields} for p in positions
                    if wallet.get("current_inventory") and p.get("netuid")
                    and (p.get("alpha") or 0) > 0]
            timing_fields = ("evaluated", "mature_trades", "favorable",
                             "trade_days", "quote_coverage")
            timing = {side: {k: sample.get(k) for k in timing_fields}
                      for side, sample in (wallet.get("timing") or {}).items()
                      if side in ("buy", "sell")}
            w.update(stamp=stamp, subnets=subnets, held=held,
                     accounting_source=('native_replay' if data.get(
                         'local_evidence') else 'hosted'),
                     current_inventory=wallet.get("current_inventory") is True,
                     balance_tao=wallet.get("balance_tao"),
                     balance_fetched_at=wallet.get("balance_fetched_at"),
                     timing=timing,
                     known_realized=known,
                     known_open=opened, known_value_fraction=fraction,
                     unresolved_proceeds=wallet.get(
                         "unresolved_sale_proceeds_tao"), screen_status=screen,
                     priced_sales=wallet.get("priced_sales", 0),
                     unpriced_sales=wallet.get("unpriced_sales", 0),
                     win_rate=wallet.get("win_rate"),
                     screens=wallet.get("screens") or [],
                     gaps=wallet.get("evidence_gaps") or [],
                     ranks={k: v for k, v in
                            (wallet.get("ranks") or {}).items()
                            if v and k in ("profit", "open", "buy", "sell")},
                     coverage={k: coverage[a].get(k) for k in
                               ("account", "transactions", "transfers")})
        for e in data.get("events", []):
            ident = e.get("id")
            if ident and (ident not in events or events[ident][0] <= priority):
                events[ident] = (priority, e)
    subnets = defaultdict(set)
    for _, e in sorted(events.values(), key=lambda pair: (
            pair[1].get("time") or "", pair[1]["id"]), reverse=True):
        kind = e.get("kind")
        if (kind not in ("buy", "sell") or e.get("evidence") != "transaction"
                or not re.fullmatch(r"[0-9]+-[0-9]+", e.get("extrinsic") or "")
                or (e.get("alpha") or 0) <= 0 or (e.get("tao") or 0) <= 0
                or not isinstance(e.get("netuid"), int) or e["netuid"] <= 0):
            continue
        a = e.get("dst" if kind == "buy" else "src")
        if not a:
            continue
        w = wallets.setdefault(a, _new())
        w["fills"] += 1
        w["buys" if kind == "buy" else "sells"] += 1
        subnets[a].add(e["netuid"])
        if len(w["examples"]) < 4:
            w["examples"].append({k: e.get(k) for k in (
                "time", "kind", "netuid", "alpha", "tao", "extrinsic")})
    for a, nets in subnets.items():
        wallets[a]["trade_subnets"] = sorted(nets)
    return wallets, sorted(snapshots)


def _census(db, summary):
    state = db.execute("SELECT page_size, latest_total, latest_checked_at "
                       "FROM census_state WHERE id=1").fetchone()
    if not state:
        raise ValueError("Census checkpoint has no verified pages")
    size, total, stamp = state
    page_count = (total + size - 1) // size
    pages, low, high = db.execute(
        "SELECT COUNT(*),MIN(min_block),MAX(max_block) FROM census_pages "
        "WHERE page<=?", (page_count,)).fetchone()
    rows, unique, holders = db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT address),"
        "SUM(CASE WHEN staked_alpha_as_tao_rao>0 THEN 1 ELSE 0 END) "
        "FROM census_accounts").fetchone()
    status = "partial" if pages < page_count else "pages_stored"
    if summary:
        info = _json(summary)
        if ((info.get("checked_at"), info.get("pages_stored"),
             info.get("network_accounts"), info.get("total_reported"))
                == (stamp, pages, rows, total)
                and info.get("status") in {"partial", "complete_single_block",
                                           "traversed_live"}):
            status = info["status"]
    return dict(captured=rows, unique=unique, total=total, pages=pages,
                total_pages=page_count, checked_at=stamp, status=status,
                positive_alpha=holders or 0, min_block=low, max_block=high)


def _safe_json(obj):
    return json.dumps(obj, ensure_ascii=True,
                      separators=(",", ":")).replace("<", "\\u003c")


def write_view(census, reports, out, summary=None, *, cache=None):
    """Read one consistent DB snapshot and stream searchable address chunks."""
    census, out = Path(census), Path(out)
    if out.suffix.lower() != ".html" or out.resolve() in {
            Path(p).resolve() for p in [census, summary, *reports] if p}:
        raise ValueError("Choose a separate .html output file")
    wallets, stamps = _research(reports, _cached(cache))
    uri = census.resolve().as_uri() + "?mode=ro"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            metadata = _census(db, summary)
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf8", dir=out.parent,
                    delete=False) as html:
                tmp = Path(html.name)
                html.write(HEAD)
                rows = db.execute(
                    "SELECT address, network, rank, balance_total_rao,"
                    "staked_alpha_as_tao_rao, block_number, last_page "
                    "FROM census_accounts ORDER BY address, network")
                while True:
                    batch = rows.fetchmany(1024)
                    if not batch:
                        break
                    for address, network, *_ in batch:
                        if network.lower() == "finney" and address in wallets:
                            wallets[address]["in_directory"] = True
                    first, last = escape(batch[0][0]), escape(batch[-1][0])
                    html.write('<script type="application/json" class="chunk" '
                               f'data-first="{first}" data-last="{last}">')
                    # Base units may exceed JavaScript's safe integer limit.
                    html.write(_safe_json([
                        [a, net, rank, str(total) if total is not None else
                         None, str(alpha) if alpha is not None else None,
                         block, page]
                        for a, net, rank, total, alpha, block, page in batch]))
                    html.write("</script>\n")
                shortlist = [a for a, w in wallets.items()
                             if w["fills"] and not w["screens"]]
                shortlist.sort(key=lambda a: (
                    not wallets[a]["ranks"].get("profit"),
                    not (wallets[a]["ranks"].get("buy")
                         or wallets[a]["ranks"].get("sell")),
                    wallets[a]["screen_status"] != "assessed",
                    -wallets[a]["fills"], a))
                model = dict(census=metadata, research=wallets,
                             shortlist=shortlist, reports=stamps,
                             candidates=[a for a in shortlist if wallets[a]
                                         ["screen_status"] == "assessed"],
                             leads=[a for a in shortlist if wallets[a]
                                    ["screen_status"] == "unassessed"])
                html.write('<script type="application/json" id="model">')
                html.write(_safe_json(model))
                html.write("</script>\n" + TAIL)
        os.replace(tmp, out)
        return model
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--census", required=True, type=Path,
                   help="Taostats accounts.sqlite")
    p.add_argument("--summary", type=Path, help="matching account-census.json")
    p.add_argument("--report", action="append", type=Path, default=[],
                   help="public tao_collect.json from a scan; repeatable")
    p.add_argument("--cache", type=Path,
                   help="also read the latest valid report per cached run")
    p.add_argument("--out", type=Path,
                   default=Path("github_out/accounts.html"))
    a = p.parse_args(argv)
    try:
        m = write_view(a.census, a.report, a.out, a.summary, cache=a.cache)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        p.exit(1, f"Could not build account directory: {exc}\n")
    print(f"{a.out}: {m['census']['captured']:,} directory rows, "
          f"{len(m['candidates'])} screened candidates, "
          f"{len(m['leads'])} leads with incomplete screens.")
    return 0


HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';
script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none';
base-uri 'none'; form-action 'none'"><title>Taostats account directory</title>
<style>:root{color-scheme:light;--ink:#183129;--muted:#52645b;--line:#cbd6ca}
*{box-sizing:border-box}body{margin:0;background:#f8faf6;color:var(--ink);
font:15px/1.5 system-ui,sans-serif}main{max-width:1200px;margin:auto;
padding:25px clamp(16px,4vw,54px) 60px}header{padding:16px 0;
border-bottom:2px solid var(--ink)}h1,h2{font:38px/1.1 Georgia,serif;
letter-spacing:-.025em;margin:0}h2{font-size:27px;margin-top:22px}
p{color:var(--muted);margin:8px 0;max-width:94ch}a{color:#00603b}
[hidden]{display:none!important}
nav,.controls{display:flex;align-items:end;flex-wrap:wrap;gap:9px;
margin:16px 0}button,input,select{font:inherit;color:var(--ink);background:
white;border:1px solid #9eaca0;border-radius:4px;padding:7px 9px}
button,summary{cursor:pointer}button[aria-pressed=true]{background:var(--ink);
color:white}button:disabled{opacity:.45;cursor:default}
label{display:grid;gap:3px;font-size:13px;color:var(--muted)}
button:focus-visible,input:focus-visible,select:focus-visible,
summary:focus-visible,a:focus-visible{outline:3px solid #168053}
details.row{padding:9px 0;border-top:1px solid var(--line)}
summary{overflow-wrap:anywhere}.mono{font-family:ui-monospace,monospace;
overflow-wrap:anywhere}.sub{font-size:13px;color:var(--muted)}
#result{min-height:1.5em}li{margin:5px 0}.fill{margin-left:12px}
@media(max-width:600px){main{padding:14px}.controls label{width:100%}}
</style></head><body><main><header><a href="wallets.html">Wallet ledger</a>
 · <a href="network.html">Wallet connections</a><h1>Account directory</h1>
<p id="coverage"></p></header><nav aria-label="Directory views">
<button id="traders">Trader research</button><button id="accounts">
Captured accounts</button></nav><h2 id="title"></h2><p id="scope"></p>
<div class="controls"><label>Wallet address or prefix
<input id="search" type="search" placeholder="e.g. 5GEN" spellcheck="false"
autocomplete="off"></label><label id="leadFilter">Evidence<select id="lead">
<option value="">All wallets with observed fills</option>
<option value="assessed">Screened candidates</option>
<option value="unassessed">Leads with incomplete screens</option>
<option value="profit">Ranked realized winners</option>
<option value="buy">Ranked buys before rises</option>
<option value="sell">Ranked sales before drops</option>
<option value="captured">Also in directory</option></select></label>
<label id="dirFilter">Rows<select id="holding">
<option value="">All captured accounts</option><option value="stake">
Positive subnet stake value</option><option value="checked">
Also in a wallet report</option></select></label></div>
<p id="result" role="status" aria-live="polite"></p><div id="rows"></div>
<nav aria-label="Results pages"><button id="prev">Previous</button>
<button id="next">Next</button></nav><details><summary>Sources</summary>
<p id="sources"></p></details></main>
"""


TAIL = r"""<script>
'use strict';
const $=id=>document.getElementById(id),M=JSON.parse($('model').textContent);
const C=M.census, chunks=[...document.querySelectorAll('.chunk')], cache=[];
const n=x=>x==null?'unknown':Number(x).toLocaleString('en-US',
 {maximumFractionDigits:8});
const el=(tag,txt,cls)=>{const e=document.createElement(tag);
 if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e};
function tao(rao){
 if(rao==null)return 'unknown';
 const u=BigInt(rao),unit=1000000000n,frac=String(u%unit).padStart(9,'0')
 .replace(/0+$/,'');return (u/unit).toLocaleString('en-US')+
 (frac?'.'+frac:'')+' TAO';
}
const status={partial:'Partial coverage',pages_stored:'Pages stored',
 traversed_live:'Directory traversed at moving blocks',
 complete_single_block:'Complete at one verified block'}[C.status];
$('coverage').textContent=status+' · '+n(C.captured)+' network rows ('+
 n(C.unique)+' unique addresses) captured · '+n(C.pages)+' of '+
 n(C.total_pages)+' pages · '+n(C.total)+' network accounts reported.';
$('sources').textContent='Taostats Accounts directory checked '+C.checked_at+
 '. Recorded row blocks '+n(C.min_block)+'–'+n(C.max_block)+'. '+
 (M.reports.length?'Wallet history snapshots: '+M.reports.join(', ')+'.':
 'No wallet history reports loaded.');
let mode=M.shortlist.length?'traders':'accounts',page=0,token=0,timer;
const size=35;
function detail(a,w){
 const box=el('div');
 box.append(el('p',a,'mono'));
 const publicLink=el('a','Open public wallet');
 publicLink.href='https://taostats.io/account/'+encodeURIComponent(a);
 publicLink.target='_blank';publicLink.rel='noopener noreferrer';
 box.append(publicLink);
 if(!w){box.append(el('p','No matched wallet history or position evidence '+
 'in the supplied reports.'));return box}
 if(w.stamp)box.append(el('p','Inspected '+w.stamp+
 ' · transactions '+(w.coverage.transactions||'unchecked')+
 ' · transfers '+(w.coverage.transfers||'unchecked')+'.'));
 box.append(el('p',n(w.buys)+' buy fills · '+n(w.sells)+' sale fills'+
 (w.trade_subnets.length?' · traded SN '+w.trade_subnets.join(', '):'')+
 (w.subnets.length?' · holdings at inspection: SN '+w.subnets.join(', '):'')+
 '.'));
 if(w.stamp){
  box.append(el('p','Realized P&L on known cost: '+n(w.known_realized)+
   ' TAO · Open P&L on marked units with known cost: '+n(w.known_open)+
   ' TAO.'));
  const share=w.known_value_fraction==null?'unknown':
   n(100*w.known_value_fraction)+'%';
  box.append(el('p','Current stake value with known cost: '+share+
   ' · sale proceeds with unresolved cost: '+n(w.unresolved_proceeds)+
   ' TAO · '+n(w.priced_sales)+' priced sales · '+n(w.unpriced_sales)+
   ' sales with unresolved cost.'));
 }
 box.append(el('p',{assessed:'Passed the balance and 24-hour activity '+
 'limits at this inspection.',unassessed:'Trading lead only. No complete '+
 'balance and activity screen is available.',excluded:'Excluded by the '+
 'report’s balance or activity limits.'}[w.screen_status],'sub'));
 if(w.win_rate!=null)box.append(el('p','Win rate on priced sales: '+
 n(100*w.win_rate)+'%.'));
 if(w.screens.length||w.gaps.length)box.append(el('p',[
 ...w.screens,...w.gaps].join(', ').replaceAll('_',' '),'sub'));
 for(const e of w.examples){
  const row=el('p',undefined,'fill');
  row.append((e.time||'Time unavailable')+' · '+e.kind+' · SN'+e.netuid+
   ' · '+n(e.alpha)+' α for '+n(e.tao)+' TAO · ');
  const link=el('a',e.extrinsic);link.target='_blank';
  link.rel='noopener noreferrer';
  link.href='https://taostats.io/extrinsic/'+e.extrinsic;row.append(link);
  box.append(row);
 }
 return box;
}
function show(a,label,w,extra){
 const d=el('details',undefined,'row'),s=el('summary');
 s.append(el('strong',a.length>20?a.slice(0,9)+'…'+a.slice(-7):a,'mono'),
  ' · '+label);d.append(s);d.ontoggle=()=>{if(d.open&&d.children.length===1){
  if(extra)d.append(el('p',extra,'sub'));
  d.append(detail(a,w));
 }};return d;
}
function ranks(w){
 return [['profit','Realized winner'],['buy','Buy timing'],
 ['sell','Sell timing']].filter(([k])=>w.ranks[k]).map(([k,v])=>
  v+' #'+w.ranks[k]).join(', ');
}
function getChunk(i){
 const hit=cache.find(x=>x.i===i);if(hit)return hit.rows;
 const rows=JSON.parse(chunks[i].textContent);
 cache.push({i,rows});if(cache.length>3)cache.shift();return rows;
}
async function draw(){
 const mine=++token,q=$('search').value.trim(),tr=mode==='traders';
 const f=$('holding').value,lead=$('lead').value,start=page*size,out=[];
 $('traders').setAttribute('aria-pressed',String(tr));
 $('accounts').setAttribute('aria-pressed',String(!tr));
 $('dirFilter').hidden=tr;$('leadFilter').hidden=!tr;
 $('title').textContent=tr?'Trader research':'Captured accounts';
 $('scope').textContent=tr? n(M.candidates.length)+
 ' wallets with observed fills passed their report’s balance and 24-hour '+
 'activity limits. '+n(M.leads.length)+' more leads have incomplete '+
 'screens. These limits do not identify who operates an address. Each rank '+
 'is from that wallet’s dated inspection. Timing and realized profit differ.':
 n(C.positive_alpha)+' captured rows show positive subnet stake value. '+
 'The directory ranks by balance. Holdings can identify an address for '+
 'research; they do not establish a trade, return or common owner.';
 let seen=0;
 if(tr){
  for(const a of M.shortlist){
   const w=M.research[a];
   if(!a.startsWith(q)||(lead==='captured'?!w.in_directory:
    ['assessed','unassessed'].includes(lead)?lead!==w.screen_status:
    lead&&!w.ranks[lead]))continue;
   if(seen++>=start&&out.length<size)out.push(show(a,
    (ranks(w)||'Observed trading')+' · '+n(w.fills)+' fills · '+
    (w.screen_status==='assessed'?'screened candidate':'unassessed lead')+
    ' · '+
    (w.in_directory?'in directory':'outside captured directory'),w));
  }
 }else{
  const bound=q+'\uffff';
  for(let i=0;i<chunks.length;i++){
   const ch=chunks[i];
   if(q&&ch.dataset.last<q)continue;
   if(q&&ch.dataset.first>bound)break;
   for(const [a,net,rank,total,stake,block,p] of getChunk(i)){
    const w=net.toLowerCase()==='finney'?M.research[a]:null;
    if(!a.startsWith(q)||(f==='stake'&&!(stake&&BigInt(stake)>0n))||
      (f==='checked'&&!w))continue;
    if(seen++<start)continue;
    if(out.length===size)break;
    out.push(show(a,net+(rank==null?'':' #'+n(rank))+' · total '+
      tao(total)+' · subnet stake '+tao(stake),w,
      'Directory page '+n(p)+' · row block '+n(block)+'.'));
   }
   if(seen>start+size)break;
   if(i%8===7){await new Promise(r=>setTimeout(r,0));
    if(token!==mine)return;}
  }
 }
 if(token!==mine)return;
 $('rows').replaceChildren(...out);
 $('result').textContent=out.length?'Showing '+n(start+1)+'–'+
  n(start+out.length)+(tr?' of '+n(seen):' captured matches')+'.':
  tr?'No candidate matches the supplied history and filters.':
  'No matching row in the captured directory pages.';
 $('prev').disabled=!page;$('next').disabled=seen<=start+size;
}
for(const x of ['traders','accounts'])$(x).onclick=()=>{
 mode=x;page=0;draw()};
function changeFilter(){
 page=0;token++;clearTimeout(timer);timer=setTimeout(draw,80);
}
$('search').oninput=changeFilter;
for(const x of ['holding','lead'])$(x).onchange=changeFilter;
$('prev').onclick=()=>{page--;draw()};$('next').onclick=()=>{page++;draw()};
draw();
</script></body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
