from contextlib import closing
from http.client import HTTPConnection
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading

import pytest

from seo import tao_watchlist as wl

A = "5EspQViAKxkKGD6xdRwdMju3SFHSQwKhbtEWhTxUoMsMVQzE"
B = "5FRu1T5Rr6LVw3nu2RmRhxRyYnvJJ9o98a1DNvnZAyGuNMKu"
GROUP = "group-0123456789abcdef"


def test_group_watch_preserves_private_details_and_mutes(tmp_path):
    path = tmp_path / "prefs.sqlite"
    wl.save(path, dict(address=A, choice="auto", label="Private", note="Keep"))
    wl.save(path, dict(address=B, choice="mute", label="Later", note="Muted"))
    result = wl.save_group(path, [A, B])
    assert result == dict(watched=[A], muted=[B])
    assert wl.preferences(path) == {
        A: dict(address=A, choice="watch", label="Private", note="Keep"),
        B: dict(address=B, choice="mute", label="Later", note="Muted"),
    }
    assert wl.apply(path, {B: ["automatic"]}) == {A: ["personal"]}


def test_group_watch_is_atomic_on_database_failure(tmp_path):
    path = tmp_path / "prefs.sqlite"
    wl.save(path, dict(address=A, choice="auto", label="Keep"))
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER reject_new BEFORE INSERT ON local_choices "
                   "WHEN NEW.address = '%s' BEGIN "
                   "SELECT RAISE(ABORT, 'storage failure'); END" % B)
    with pytest.raises(sqlite3.Error):
        wl.save_group(path, [A, B])
    assert wl.preferences(path) == {
        A: dict(address=A, choice="auto", label="Keep", note="")}


@pytest.mark.parametrize("members", [[], [A, "invalid"], [A] * 51])
def test_group_watch_validates_every_member_before_writing(tmp_path, members):
    path = tmp_path / "prefs.sqlite"
    with pytest.raises(ValueError):
        wl.save_group(path, members)
    assert not path.exists()


def test_group_api_requires_local_token_and_server_membership(
        tmp_path, monkeypatch):
    graph = dict(groups=[dict(id=GROUP, members=[A, B])], edges=[], nodes={})
    monkeypatch.setattr(wl.Model, "groups", lambda self: graph, raising=False)
    with wl.server(tmp_path, 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = "http://" + server.authority
            headers = {"Content-Type": "application/json", "Origin": origin,
                       "X-Watchlist-Token": server.token}
            wl.save(server.model.prefs, dict(address=B, choice="mute"))
            with closing(HTTPConnection("127.0.0.1", server.server_port)) as c:
                c.request("GET", "/api/groups")
                response = c.getresponse()
                assert response.status == 200
                group, = json.loads(response.read())["groups"]
                assert group["members"] == [A, B]
                cases = [
                    ({"group_id": GROUP}, {"X-Watchlist-Token": "bad"}, 403),
                    ({"group_id": GROUP},
                     {"Origin": "https://other.invalid"}, 403),
                    ({"group_id": GROUP}, {"Host": "other.invalid"}, 403),
                    ({"group_id": "group-0000000000000000"}, {}, 400),
                    ({"group_id": GROUP, "members": ["not-a-member"]},
                     {}, 400),
                    ({"group_id": GROUP}, {}, 200),
                ]
                for payload, extra, expected in cases:
                    c.request("POST", "/api/groups/watch", json.dumps(payload),
                              headers | extra)
                    response = c.getresponse()
                    body = json.loads(response.read())
                    assert response.status == expected
                    if expected == 200:
                        assert body == dict(ok=True, watched=[A], muted=[B])
            assert wl.preferences(server.model.prefs)[B]["choice"] == "mute"
            assert wl.apply(server.model.prefs, {}) == {A: ["personal"]}
        finally:
            server.shutdown()
            thread.join()


def test_oversized_group_requires_a_bounded_server_owned_subset(
        tmp_path, monkeypatch):
    group = dict(id=GROUP, members=[A, B], oversized=True, watchable=False)
    monkeypatch.setattr(wl.Model, "groups", lambda self: dict(groups=[group]))
    app = wl.Model(tmp_path)
    for payload in [dict(group_id=GROUP),
                    dict(group_id=GROUP, members=[]),
                    dict(group_id=GROUP, members=[A, A]),
                    dict(group_id=GROUP, members=[A, "not-a-member"]),
                    dict(group_id=GROUP, members=[A] * 25)]:
        with pytest.raises(ValueError):
            app.watch_group(payload)
    assert not app.prefs.exists()
    assert app.watch_group(dict(group_id=GROUP, members=[A])) == {
        "watched": [A], "muted": []}
    assert list(wl.preferences(app.prefs)) == [A]


def test_groups_reuse_unchanged_reports_and_refresh_after_file_change(
        tmp_path, monkeypatch):
    path = tmp_path / "github_out/10/tao_collect.json"
    path.parent.mkdir(parents=True)
    event = dict(id="receipt", kind="stake_transfer", src=A, dst=B,
                 alpha=10, netuid=24, evidence="transaction",
                 extrinsic="123-4", time="2026-11-01T00:00:00Z")
    report = dict(checked_at="2026-11-02T00:00:00Z", events=[event],
                  performance=dict(wallets=[]))
    path.write_text(json.dumps(report), encoding="utf8")
    model = wl.Model(tmp_path)
    first = model.groups()
    assert first["groups"][0]["members"] == [A, B]
    assert first["groups"][0]["economics"]["total_pnl_tao"] is None
    assert first["edges"][0]["source_reports"][0]["source"] == (
        "10/tao_collect.json")
    with monkeypatch.context() as patch:
        def unexpected_parse(*args, **kwargs):
            pytest.fail("An unchanged report was parsed again")
        patch.setattr(wl.json, "load", unexpected_parse)
        assert model.groups()["groups"][0]["members"] == [A, B]
    report["events"] = []
    path.write_text(json.dumps(report), encoding="utf8")
    assert model.groups()["groups"] == []


def test_local_editor_serves_only_allowlisted_native_reports(tmp_path):
    cache = tmp_path / "github_out"
    cache.mkdir()
    for name in ("native-holders.html", "wallets-native.html", "private.html"):
        (cache / name).write_text(name, encoding="utf8")
    with wl.server(tmp_path, 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with closing(HTTPConnection("127.0.0.1", server.server_port)) as c:
                for name, expected in [("native-holders.html", 200),
                                       ("wallets-native.html", 200),
                                       ("private.html", 404),
                                       ("../native-holders.html", 404)]:
                    c.request("GET", "/" + name)
                    response = c.getresponse()
                    assert response.status == expected
                    body = response.read()
                    if expected == 200:
                        assert body == name.encode()
        finally:
            server.shutdown()
            thread.join()


def test_group_ui_keeps_unknown_profit_and_requires_explicit_watch():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the local editor JavaScript")
    page = Path(wl.__file__).with_name("watchlist_editor.html")
    script = r"""
const assert=require('node:assert/strict'), fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(process.argv[1], 'utf8');
class Element {
  constructor(id=''){this.id=id;this.children=[];this.value='';
    this.text='';this.dataset={};this.listeners={};
    this.classList={toggle(){},add(){}};}
  set textContent(v){this.text=String(v);this.children=[];}
  get textContent(){
    return this.text+this.children.map(x=>x.textContent).join(' ');}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.text='';this.children=children;}
  setAttribute(name,value){this[name]=value;}
  addEventListener(event,listener){this.listeners[event]=listener;}
  focus(){this.focused=true;} scrollIntoView(){}
}
const nodes=new Map([...html.matchAll(/\bid="([^"]+)"/g)]
  .map(match=>[match[1],new Element(match[1])]));
nodes.get('filter').value='all';nodes.get('sort').value='research';
nodes.get('edit').elements={choice:[]};
const requests=[];
let context={document:{getElementById:id=>nodes.get(id),
  querySelectorAll:q=>q==='[id]'?[...nodes.values()]:[],
  querySelector:q=>q.startsWith('meta')?{content:'token'}:new Element(),
  createElement:()=>new Element(),createDocumentFragment:()=>new Element()},
  window:{matchMedia:()=>({matches:false})},setTimeout,clearTimeout,
  fetch:async(url,options={})=>{requests.push({url,options});
    return {ok:true,json:async()=>url==='/api/groups/watch'?
      {ok:true,watched:['WalletA'],muted:['WalletB']}:
      url==='/api/groups'?{groups:[],edges:[],nodes:{}}:{wallets:[]}};
  }};
vm.createContext(context);
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
async function test(){
  await new Promise(resolve=>setImmediate(resolve));
  vm.runInContext(`
    state.wallets=index([{address:'WalletA',choice:'auto',label:'Keep',
      note:'Private'}, {address:'WalletB',choice:'mute'}]);
    state.groups=new Map([['group-0123456789abcdef',{
      id:'group-0123456789abcdef',members:['WalletA','WalletB'],
      edge_ids:['edge-1'],bridge_edge_ids:[],economics:{status:'partial',
      rankable:false,total_pnl_tao:900,coverage_fraction:0.5,
      reasons:['unresolved_cost_basis']}}]]);
    state.groupEdges=new Map([['edge-1',{id:'edge-1',src:'WalletA',
      dst:'WalletB',status:'confirmed',kind:'transfer',netuid:7,
      evidence:'native_receipt',extrinsic:'123-4',group_eligible:true}]]);
    selectGroup(state.groups.get('group-0123456789abcdef'),false);
  `,context);
  assert.match(nodes.get('group-specs').textContent,/Unknown/);
  assert.doesNotMatch(nodes.get('group-specs').textContent,/900/);
  assert.match(nodes.get('group-members').textContent,/Keep/);
  assert.match(nodes.get('group-members').textContent,/Muted/);
  assert.match(nodes.get('group-evidence').textContent,/Confirmed transfer/);
  assert.match(nodes.get('group-watch-preview').textContent,/1.*muted/i);
  assert.equal(requests.filter(r=>r.options.method==='POST').length,0);
  await nodes.get('watch-group').onclick();
  const post=requests.find(r=>r.options.method==='POST');
  assert.equal(post.url,'/api/groups/watch');
  assert.deepEqual(JSON.parse(post.options.body),
    {group_id:'group-0123456789abcdef'});
  assert.equal(vm.runInContext("state.wallets.get('WalletB').choice",context),
    'mute');
  assert.equal(vm.runInContext("state.wallets.get('WalletA').label",context),
    'Keep');
  assert.equal(vm.runInContext("state.wallets.get('WalletA').note",context),
    'Private');
  vm.runInContext(`
    const broad=state.groups.get('group-0123456789abcdef');
    broad.oversized=true;broad.watchable=false;
    state.groupSelection.delete(broad.id);selectGroup(broad,false);
  `,context);
  assert.equal(nodes.get('watch-group').disabled,true);
  function descendants(element){
    return [element,...element.children.flatMap(descendants)];
  }
  const checks=descendants(nodes.get('group-members'))
    .filter(element=>element.type==='checkbox');
  assert.equal(checks.length,2);
  assert.equal(checks[0].checked,false);
  assert.equal(checks[1].disabled,true);
  checks[0].checked=true;checks[0].onchange();
  assert.equal(nodes.get('watch-group').disabled,false);
  assert.equal(descendants(nodes.get('group-members'))
    .some(element=>element.type==='checkbox'&&element.focused),true);
  await nodes.get('watch-group').onclick();
  const selectedPost=requests.filter(r=>r.options.method==='POST').at(-1);
  assert.deepEqual(JSON.parse(selectedPost.options.body),
    {group_id:'group-0123456789abcdef',members:['WalletA']});
}
test().catch(error=>{console.error(error);process.exitCode=1;});
"""
    subprocess.run([node, "-e", script, str(page)], check=True, timeout=30)
