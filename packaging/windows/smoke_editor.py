"""Exercise the installed editor on an ephemeral port in the smoke profile."""
from contextlib import closing
from http.client import HTTPConnection
import json
import re
from threading import Thread
from urllib.parse import urljoin, urlsplit

import wallet_monitor
from seo import tao_local_watch, tao_watchlist


def report_links(req, root, port):
    cache = root / "github_out"
    # The census is a separate optional download. Use a small directory
    # fixture while the wallet reports and explorers come from the real import.
    accounts = cache / "accounts.html"
    accounts.write_text('<a href="wallets.html">Wallet ledger</a>')

    def page(path):
        req.request("GET", path)
        response = req.getresponse()
        body = response.read()
        assert response.status == 200, path
        return body

    base = f"http://127.0.0.1:{port}/"
    live = page("/wallets.html").decode()
    for run in ("10", "11"):
        relative = f"{run}/wallets.html"
        assert f'href="{relative}"' in live
        archived = page("/" + relative).decode()
        link, = re.findall(r'href="([^"]*accounts\.html)"', archived)
        file_url = (cache / relative).as_uri()
        assert urljoin(file_url, link) == accounts.as_uri()
        relative_http = urlsplit(urljoin(urljoin(base, relative), link)).path
        assert page(relative_http) == accounts.read_bytes()
    imported = root / "explorer/network.html"
    assert page("/network.html") == imported.read_bytes()
    assert page("/accounts.html") == accounts.read_bytes()
    print("Saved reports resolve directory links on disk and over loopback. "
          "The editor serves the installed transfer explorer.")


def verify():
    root = wallet_monitor.data_root()
    address = tao_local_watch._addr("0x" + "12" * 32)
    with tao_watchlist.server(root, port=0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_port
            assert tao_watchlist.same_instance(root, port)
            assert not tao_watchlist.same_instance(root / "other", port)
            opened = []
            wallet_monitor.webbrowser.open = opened.append
            for _ in range(2):
                assert wallet_monitor.reopen_editor(root, port) == 0
            url = "http://" + server.authority
            assert opened == [url + "/"] * 2
            with closing(HTTPConnection("127.0.0.1", port, timeout=4)) as req:
                report_links(req, root, port)
                req.request("GET", "/")
                result = req.getresponse()
                page = result.read().decode()
                assert result.status == 200 and server.token in page
                assert "__WATCHLIST_TOKEN__" not in page
                body = json.dumps(dict(address=address, choice="watch",
                                       label="Installer fixture"))
                headers = {"Content-Type": "application/json", "Origin": url,
                           "X-Watchlist-Token": server.token}
                req.request("POST", "/api/watchlist", body, headers)
                result = req.getresponse()
                assert result.status == 200 and json.loads(result.read())["ok"]
                req.request("GET", "/api/watchlist")
                result = req.getresponse()
                entries = json.loads(result.read())["wallets"]
                assert any(w["address"] == address and w["choice"] == "watch"
                           for w in entries)
            prefs = root / tao_watchlist.PREFS
            assert prefs.is_file()
            assert tao_watchlist.apply(prefs, {}) == {address: ["personal"]}
        finally:
            server.shutdown()
            thread.join(timeout=5)
    print("Installed editor served the template, reopened the same data "
          "root, and saved a private watch preference.")


if __name__ == "__main__":
    verify()
