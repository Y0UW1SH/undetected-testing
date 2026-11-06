import sqlite3
from contextlib import closing
from http.client import HTTPConnection
import json
import threading

import pytest

from seo import tao_watchlist as wl

A = "5EspQViAKxkKGD6xdRwdMju3SFHSQwKhbtEWhTxUoMsMVQzE"
B = "5FRu1T5Rr6LVw3nu2RmRhxRyYnvJJ9o98a1DNvnZAyGuNMKu"


def test_private_watch_mute_and_return_to_automatic(tmp_path):
    db = tmp_path / "prefs.sqlite"
    original = {B: ["followed"]}
    assert wl.apply(db, original) == original and not db.exists()
    wl.save(db, dict(address=A, choice="watch", label="Research", note="SN28"))
    wl.save(db, dict(address=B, choice="mute", label="", note="check later"))
    assert wl.apply(db, original) == {A: ["personal"]}
    wl.save(db, dict(address=B, choice="auto", label="", note=""))
    assert wl.apply(db, original) == {B: ["followed"], A: ["personal"]}
    assert wl.preferences(db)[A]["note"] == "SN28"
    with pytest.raises(ValueError):
        wl.save(db, dict(address=A[:-1] + "A", choice="watch"))
    assert original == {B: ["followed"]}


def test_directory_search_does_not_invent_profit(tmp_path):
    cache = tmp_path / "github_out"
    cache.mkdir()
    with sqlite3.connect(cache / "taostats-directory.sqlite") as db:
        db.execute("CREATE TABLE census_accounts(address,network)")
        db.execute("INSERT INTO census_accounts VALUES(?,?)", (A, "finney"))
    app = wl.Model(tmp_path)
    assert app.data()["census_rows"] == 1
    wallet, = app.search(A[:8])
    assert wallet["address"] == A and not wallet["inspected"]
    assert wallet["known_realized"] is None and wallet["known_open"] is None
    assert wallet["choice"] == "auto"


def test_local_editor_save_and_watcher_override(tmp_path):
    with wl.server(tmp_path, 0) as server:
        task = threading.Thread(target=server.serve_forever, daemon=True)
        task.start()
        try:
            assert wl.same_instance(tmp_path, server.server_port)
            assert not wl.same_instance(tmp_path / "other", server.server_port)
            url = "/api/watchlist"
            data = json.dumps(dict(address=A, choice="watch"))
            headers = {"Content-Type": "application/json",
                       "Origin": "http://" + server.authority}
            with closing(HTTPConnection("127.0.0.1", server.server_port)) as c:
                for extra, expected in [({}, 403), (
                        {"X-Watchlist-Token": server.token,
                         "Origin": "https://other.invalid"}, 403), (
                        {"X-Watchlist-Token": server.token}, 200)]:
                    c.request("POST", url, data, headers | extra)
                    response = c.getresponse()
                    assert response.status == expected
                    response.read()
            assert wl.apply(tmp_path / wl.PREFS, {}) == {A: ["personal"]}
        finally:
            server.shutdown()
            task.join()
