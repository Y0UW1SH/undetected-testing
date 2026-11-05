"""Build an offline address-to-address transfer explorer from saved data."""

import argparse
from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from seo.tao_view import _write


FIELDS = ("id", "time", "src", "dst", "kind", "extrinsic", "alpha", "tao",
          "netuid", "dst_netuid", "dst_alpha", "event_index", "transfer_side",
          "evidence", "confidence", "basis_status", "known_buy_price_tao",
          "inherited_buy_price_tao", "known_alpha", "probable_alpha",
          "unknown_alpha", "origins")
EXT = re.compile(r"[0-9]+-[0-9]+")


def _status(r):
    ev = r.get("evidence")
    if ev == "snapshot_match":
        return "Inferred"
    if ev == "coldkey_swap":
        return "Coldkey swap"
    if (r.get("transfer_side") in {"in", "out", "unknown"} or not r["src"]
            or not r["dst"]):
        return "One-sided"
    return "Confirmed" if ev in {"transaction", "finalized_event"} else (
        "Unclassified")


def _read(path):
    with Path(path).open(encoding="utf8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Expected an object in " + Path(path).name)
    return data


def _add(rows, item, source, stamp):
    if (item.get("kind") not in {"stake_transfer", "tao_transfer"}
            or not item.get("src") and not item.get("dst")
            or item.get("src") == item.get("dst")):
        return
    row = {k: item.get(k) for k in FIELDS}
    if not row["id"]:
        raise ValueError("Transfer record is missing its stable identity")
    ext = row["extrinsic"]
    if ext and not EXT.fullmatch(str(ext)):
        row["extrinsic"] = None
    if source == "Finney":
        row["evidence"] = "finalized_event"
    row.update(source=source, stamp=stamp, status=_status(row))
    idx = row["event_index"]
    if (source == "Taostats" and row["kind"] == "tao_transfer"
            and type(idx) is int and idx > 0 and row["extrinsic"]):
        # The sender's and recipient's histories can name the same event
        # differently. Separate equally sized transfers in a batch by index.
        key = (source, ext, idx, row["kind"], row["src"], row["dst"])
    else:
        key = (source, row["id"])
    if key not in rows or stamp >= rows[key]["stamp"]:
        rows[key] = row


def _census(path, endpoints):
    if not path:
        return None
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        pin = db.execute("SELECT height, complete, pages FROM census_pin "
                         "WHERE id=1").fetchone()
        if not pin:
            raise ValueError("Census has no pinned finalized block yet")
        count = db.execute("SELECT COUNT(*) FROM native_accounts").fetchone()
        enrolled = []
        for i in range(0, len(endpoints), 400):
            batch = endpoints[i:i + 400]
            marks = ",".join("?" for _ in batch)
            enrolled.extend(a for a, in db.execute(
                "SELECT address FROM native_accounts "
                f"WHERE address IN ({marks})", batch))
    return dict(height=pin[0], complete=bool(pin[1]), pages=pin[2],
                accounts=count[0], enrolled=sorted(enrolled))


def build(census=None, reports=(), window=None):
    rows, audits, corroborated = {}, [], set()
    for path in reports:
        data = _read(path)
        stamp = data.get("checked_at")
        if not stamp or not any(k in data for k in ("transfer_details",
                                                    "transfers")):
            raise ValueError("Expected a downloaded Taostats collector report")
        audit = dict(source="Taostats", checked_at=stamp)
        if audit not in audits:
            audits.append(audit)
        combined = {r["id"]: dict(r) for r in data.get("transfers", [])}
        for r in data.get("transfer_details", []):
            combined.setdefault(r["id"], {}).update(r)
        for r in combined.values():
            if r.get("paired_event_id"):
                corroborated.add(r["paired_event_id"])
            _add(rows, r, "Taostats", stamp)
    if window:
        data = _read(window)
        if data.get("source") != "local-finney-window-v1":
            raise ValueError("Expected a local finalized Finney window")
        stamp = data.get("checked_at", "")
        audits.append(dict(source="Finney", checked_at=stamp,
                           first=data.get("first_block"),
                           last=data.get("last_block"),
                           scanned=data.get("last_scanned_block"),
                           status=data.get("status")))
        for r in data.get("transfer_edges", []):
            _add(rows, r, "Finney", stamp)
    groups = defaultdict(list)
    transactions, endpoints = set(), set()
    for row in rows.values():
        if row["source"] == "Taostats" and row["id"] in corroborated:
            continue
        groups[row["src"], row["dst"]].append(row)
        endpoints.update(a for a in (row["src"], row["dst"]) if a)
        if (row["extrinsic"] and row["evidence"] in
                {"transaction", "finalized_event"}):
            transactions.add(row["extrinsic"])
    pairs = []
    for (a, b), records in groups.items():
        records.sort(key=lambda r: (r["time"] or "", r["extrinsic"] or ""),
                     reverse=True)
        pairs.append(dict(src=a, dst=b, records=records))
    pairs.sort(key=lambda p: (-len(p["records"]), p["src"] or "",
                              p["dst"] or ""))
    paired = [p for p in pairs if p["src"] and p["dst"]]
    return dict(census=_census(census, sorted(endpoints)), sources=audits,
                counts=dict(pairs=len(paired), confirmed_pairs=sum(
                    any(r["status"] == "Confirmed" for r in p["records"])
                    for p in paired), endpoints=len(endpoints),
                    transactions=len(transactions),
                    records=sum(len(p["records"]) for p in pairs)),
                pairs=pairs)


def render(data):
    payload = json.dumps(data, ensure_ascii=True,
                         separators=(",", ":")).replace("<", "\\u003c")
    return HTML.replace("<!--DATA-->", payload)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--census", type=Path, help="Finney census SQLite state")
    p.add_argument("--report", type=Path, action="append", default=[],
                   help="downloaded tao_collect.json; repeat for other scans")
    p.add_argument("--window", type=Path, help="local native window JSON")
    p.add_argument("--out", type=Path, default=Path("github_out/network.html"))
    args = p.parse_args(argv)
    if not (args.census or args.report or args.window):
        p.error("supply --census, --report or --window")
    if args.out.suffix.lower() != ".html" or args.out.resolve() in {
            path.resolve() for path in [args.census, *args.report, args.window]
            if path}:
        p.error("choose a separate .html output file")
    try:
        data = build(args.census, args.report, args.window)
        _write(args.out, render(data))
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        p.exit(1, f"Could not create network view: {exc}\n")
    print(f"{args.out}: {data['counts']['pairs']} directed pairs, "
          f"{data['counts']['transactions']} distinct extrinsics.")
    return 0


HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';
script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none';
base-uri 'none'; form-action 'none'">
<title>Wallet connections · Taostats</title><style>
:root{color-scheme:light;--paper:#f8faf6;--ink:#183129;--muted:#52645b;
--line:#c9d4ca;--accent:#00633e}*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.5 system-ui,sans-serif}main{max-width:1280px;margin:auto;
padding:28px clamp(16px,4vw,62px) 72px}a,button.link{color:var(--accent)}
a{text-underline-offset:3px}header{padding:14px 0 18px;
border-bottom:2px solid var(--ink)}h1,h2{margin:0;line-height:1.08;
font-family:Georgia,serif;font-weight:400}h1{font-size:clamp(36px,5vw,64px);
letter-spacing:-.035em}h2{font-size:30px;margin-top:26px}
p{color:var(--muted);margin:8px 0}p.note{max-width:90ch}.stats{display:flex;
flex-wrap:wrap;gap:12px 44px;margin:18px 0}.stat b{display:block;
font:32px Georgia,serif}.stat span,.sub{color:var(--muted);font-size:13px}
.controls,nav{display:flex;flex-wrap:wrap;gap:12px;align-items:end}
.controls{margin:16px 0}label{display:grid;gap:4px;font-size:13px}
input,select,button{font:inherit;color:var(--ink);background:white;
border:1px solid #98aa9e;border-radius:4px;padding:7px 10px}
button{cursor:pointer}button:disabled{opacity:.45;cursor:default}
input{width:210px}button:focus-visible,a:focus-visible,input:focus-visible,
select:focus-visible,summary:focus-visible{outline:3px solid #168053;
outline-offset:2px}button.link{font:14px ui-monospace,monospace;
background:none;border:0;text-decoration:underline;padding:0}
.pair{padding:12px 0;border-top:1px solid var(--line)}
.pair-head{display:flex;align-items:baseline;flex-wrap:wrap;gap:8px}
.peer{font-weight:600}.pair summary{cursor:pointer;color:var(--accent);
font-size:14px;margin-top:4px}ul{padding-left:23px}li{margin:10px 0}
.mono{font-family:ui-monospace,monospace;overflow-wrap:anywhere}
nav{margin-top:15px}.sources{border-top:1px solid var(--line);
padding-top:12px;margin-top:26px}.sources summary{cursor:pointer}
@media(max-width:620px){main{padding:16px 16px 45px}
.controls label{width:calc(50% - 8px)}input,select{width:100%}
.stats{gap:12px 24px}.stat b{font-size:26px}}
</style></head><body><main><header>
<a href="wallets.html">Wallet ledger</a><h1>Wallet connections</h1>
<p id="coverage"></p></header><section class="stats" id="stats"
aria-label="Network totals"></section><p class="note" id="scope"></p>
<h2>Transfer evidence</h2><div class="controls">
<label>Wallet address or prefix<input type="search" id="address"
placeholder="e.g. 5GEN" autocomplete="off" spellcheck="false"></label>
<label>Other address or prefix<input type="search" id="peer"
placeholder="Optional" autocomplete="off" spellcheck="false"></label>
<label>Direction<select id="direction"><option value="either">Either</option>
<option value="sent">Sent by wallet</option>
<option value="received">Received by wallet</option></select></label>
<label>Asset<select id="asset"><option value="">All assets</option>
<option value="tao_transfer">Native TAO</option>
<option value="stake_transfer">Subnet stake</option></select></label>
<label>Evidence<select id="evidence"><option value="">All evidence</option>
<option>Confirmed</option><option>One-sided</option><option>Inferred</option>
<option>Coldkey swap</option><option>Unclassified</option></select></label>
<button id="clear" type="button">Clear</button></div>
<p id="results" role="status" aria-live="polite"></p>
<div id="list"></div><nav aria-label="Results pages">
<button id="prev" type="button">Previous</button>
<button id="next" type="button">Next</button></nav>
<details class="sources"><summary>Sources and coverage</summary>
<div id="sources"></div></details></main>
<script type="application/json" id="data"><!--DATA--></script><script>
'use strict';
const D=JSON.parse(document.getElementById('data').textContent),
$=id=>document.getElementById(id), enrolled=new Set(D.census?.enrolled||[]);
const el=(tag,txt,cls)=>{const n=document.createElement(tag);
if(txt!==undefined)n.textContent=txt;if(cls)n.className=cls;return n;};
const count=n=>Number(n).toLocaleString(),
num=n=>n==null?'unknown':Number(n).toLocaleString('en-US',
{maximumFractionDigits:9}), short=a=>a&&a.length>18?
a.slice(0,8)+'…'+a.slice(-7):a||'Unknown peer';
function link(x){
 if(!x||!/^\d+-\d+$/.test(x))return el('span','');
 const a=el('a',x);a.href='https://taostats.io/extrinsic/'+x;
 a.target='_blank';a.rel='noopener noreferrer';return a;
}
function address(a){
 if(!a)return el('span','Unknown peer','sub');
 const b=el('button',short(a),'link');b.type='button';
 b.title=a;b.setAttribute('aria-label','Look up '+a);
 b.onclick=()=>{$('address').value=a;$('peer').value='';
 $('direction').value='either';page=0;draw();};return b;
}
function member(a){
 if(!D.census||!a)return '';
 if(enrolled.has(a))return 'present at pin';
 return D.census.complete?'absent at pin':'unseen in partial census';
}
function stat(n,label){
 const wrap=el('div',undefined,'stat');wrap.append(el('b',count(n)),
 el('span',label));$('stats').append(wrap);
}
if(D.census){
 const c=D.census;
 $('coverage').textContent='Finney System.Account at finalized block '+
 count(c.height)+' · '+(c.complete?'Complete census':'Partial census')+
 ' · '+count(c.pages)+' pages.';
 stat(c.accounts,'System accounts at pin');
}else $('coverage').textContent='No Finney system-account census loaded.';
stat(D.counts.endpoints,'Addresses in evidence');
stat(D.counts.transactions,'Distinct transfer extrinsics');
stat(D.counts.pairs,'Directed address pairs');
$('scope').textContent=count(D.counts.confirmed_pairs)+' pairs have '+
 'confirmed transfer evidence. Census membership describes one block. '+
 'The loaded histories cover observed transactions only. A transfer '+
 'between addresses does not establish shared ownership.';
for(const s of D.sources){
 const t=s.source==='Finney'?'Finney finalized events · blocks '+s.first+
 '–'+s.last+' · scanned through '+(s.scanned??'none')+' · '+s.status:
 'Taostats wallet history · snapshot '+s.checked_at;
 $('sources').append(el('p',t));
}
$('sources').append(el('p','Each extrinsic counts once across all sources. '+
 'Separate rows can describe the same transaction. One-sided stake evidence '+
 'shows the observed debit or credit. Snapshot matches are inferred.'));
let page=0;const size=30;
function matches(a,q){return !q||Boolean(a&&a.startsWith(q));}
function lookup(pair){
 const a=$('address').value.trim(),b=$('peer').value.trim(),
 dir=$('direction').value;
 return (dir!=='received'&&matches(pair.src,a)&&matches(pair.dst,b))||
 (dir!=='sent'&&matches(pair.dst,a)&&matches(pair.src,b));
}
function showRow(r){
 const item=el('li'),stake=r.kind==='stake_transfer';
 const c=r.confidence==null?'': ' · confidence '+num(r.confidence);
 let amt=stake?(r.netuid==null?'Subnet unknown':'SN'+r.netuid)+
 ' · '+num(r.alpha)+' α':num(r.tao)+' TAO';
 if(stake&&r.dst_netuid!=null&&r.dst_netuid!==r.netuid)
  amt+=' → SN'+r.dst_netuid+' · '+num(r.dst_alpha)+' α';
 item.append(el('strong',amt),el('span',' · '+r.status+c+' · '+r.source));
 if(r.time)item.append(el('span',' · '+r.time));
 if(r.extrinsic)item.append(' · ',link(r.extrinsic));
 if(r.transfer_side&&r.transfer_side!=='both')
  item.append(el('div','Observed side: '+r.transfer_side,'sub'));
 if(stake){
  let basis=r.inherited_buy_price_tao==null?'Recipient entry unknown':
   'Inherited entry '+num(r.inherited_buy_price_tao)+' TAO/α';
  if(r.basis_status)basis+=' · '+r.basis_status.replaceAll('_',' ');
  if(r.known_buy_price_tao!=null&&r.known_alpha>0)
   basis+=' · known portion '+num(r.known_alpha)+' α at '+
   num(r.known_buy_price_tao)+' TAO/α';
  if(r.unknown_alpha>0)basis+=' · '+num(r.unknown_alpha)+' α unknown basis';
  item.append(el('div',basis,'sub'));
  for(const o of r.origins||[]){
   const ln=el('div',undefined,'sub');
   ln.append('Original fill ',el('span',o.address||'Unknown wallet','mono'),
    ' · '+num(o.alpha)+' α at '+num(o.unit_buy_price_tao)+' TAO/α ');
   if(o.extrinsic)ln.append(link(o.extrinsic));item.append(ln);
  }
 }
 return item;
}
function showPair(p,rows){
 const box=el('article',undefined,'pair'),head=el('div',undefined,'pair-head');
 head.append(address(p.src),el('span','→'),address(p.dst));
 const unique=new Set(rows.filter(r=>r.extrinsic&&
 ['transaction','finalized_event'].includes(r.evidence)).map(r=>r.extrinsic));
 head.append(el('span',count(unique.size)+' extrinsics · '+
 [...new Set(rows.map(r=>r.status))].join(', '),'sub'));box.append(head);
 const det=el('details');det.append(el('summary','Inspect '+
 count(rows.length)+(rows.length===1?' evidence record':' evidence records')));
 for(const [k,a] of [['Sender',p.src],['Receiver',p.dst]]){
  const d=el('div',undefined,'sub');
  d.append(k+' ',el('span',a||'Unknown peer','mono'));
  if(member(a))d.append(' · '+member(a));det.append(d);
 }
 det.addEventListener('toggle',()=>{if(det.open&&!det.querySelector('ul')){
  const ul=el('ul');rows.forEach(r=>ul.append(showRow(r)));det.append(ul);
 }});box.append(det);return box;
}
function draw(){
 const result=[];for(const p of D.pairs){
  if(!lookup(p))continue;
  const rows=p.records.filter(r=>(!$('asset').value||
   $('asset').value===r.kind)&&(!$('evidence').value||
   $('evidence').value===r.status));
  if(rows.length)result.push([p,rows]);
 }
 $('list').replaceChildren();
 const start=page*size,end=Math.min(start+size,result.length);
 $('results').textContent=result.length?
 'Showing '+count(start+1)+'–'+count(end)+' of '+count(result.length)+
 ' address connections and one-sided observations.':
 'No transfer evidence matches this lookup in the loaded sources.';
 for(const [p,rows] of result.slice(start,end))
  $('list').append(showPair(p,rows));
 $('prev').disabled=!page;$('next').disabled=end===result.length;
}
for(const id of ['address','peer','asset','direction','evidence']){
 $(id).addEventListener('input',()=>{page=0;draw();});
}
$('prev').onclick=()=>{page--;draw();};$('next').onclick=()=>{page++;draw();};
$('clear').onclick=()=>{for(const id of ['address','peer','asset','evidence'])
 $(id).value='';$('direction').value='either';page=0;draw();};
draw();
</script></body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
