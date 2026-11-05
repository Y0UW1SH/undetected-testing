"""Build an offline wallet report from a GitHub artifact."""
import argparse
from html import escape
import json
from pathlib import Path
import subprocess


REPO = "Y0UW1SH/undetected-testing"


def fetch(run, cache):
    if run == "latest":
        result = subprocess.check_output([
            "gh", "run", "list", "-R", REPO, "-w", "tao-collect.yml",
            "--branch", "master", "--status", "success", "-L", "1",
            "--json", "databaseId"], text=True)
        rows = json.loads(result)
        if not rows:
            raise SystemExit("No successful wallet collection found.")
        run = str(rows[0]["databaseId"])
    if not run.isascii() or not run.isdecimal():
        raise SystemExit("Use 'latest' or a numeric GitHub Actions run ID.")
    folder = cache / run
    files = sorted(folder.rglob("tao_collect.json"))
    if not files:
        subprocess.run(["gh", "run", "download", run, "-R", REPO, "-n",
                        "wallet-collection", "-D", str(folder)], check=True)
        files = sorted(folder.rglob("tao_collect.json"))
    if not files:
        raise SystemExit("The artifact did not contain tao_collect.json.")
    return files[0]


def render(report, local_feed=None):
    if not isinstance(report.get("performance", {}).get("wallets"), list):
        raise ValueError("This report has no wallet performance data.")
    # Raw public data is untrusted, including addresses and labels.
    data = json.dumps(dict(report, local_feed=local_feed),
                      separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).replace("<", "\\u003c")
    template = Path(__file__).with_name("wallet_view.html")
    page = template.read_text(encoding="utf8")
    return page.replace("<!--PAYLOAD-->", data)


def _archive(html, data, source, cache):
    """Keep prior discovery accessible after a watchlist-only refresh."""
    run = next((p.name for p in source.parents if p.name.isdecimal()), None)
    if not run:
        return html
    folder = cache / run
    folder.mkdir(parents=True, exist_ok=True)
    tag = data.get("mode", "collection")
    index = cache / "wallet_views.json"
    rows = (json.loads(index.read_text(encoding="utf8"))
            if index.exists() else {})
    rows[run] = [data["checked_at"], tag]
    temp = index.with_suffix(".tmp")
    temp.write_text(json.dumps(rows), encoding="utf8")
    temp.replace(index)
    (folder / "wallets.html").write_text(html, encoding="utf8")
    links = ['<details><summary>Saved snapshots</summary><ul>']
    for rid, (stamp, mode) in sorted(rows.items(), reverse=True):
        if rid.isdecimal():
            links.append(f'<li><a href="{rid}/wallets.html">'
                         f'{escape(stamp[:19])} · {escape(mode)}</a></li>')
    links.append("</ul></details>")
    return html.replace("</header>", "".join(links) + "</header>", 1)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--github", metavar="latest|RUN_ID")
    source.add_argument("--input", type=Path, help="downloaded JSON")
    p.add_argument("--cache", type=Path, default=Path("github_out"))
    p.add_argument("--out", type=Path, default=Path("github_out/wallets.html"))
    p.add_argument("--feed", type=Path, help="local movement feed JSON")
    args = p.parse_args(argv)
    path = args.input or fetch(args.github, args.cache)
    data = json.loads(path.read_text(encoding="utf8"))
    feed_path = args.feed or Path("try_tao_out/local_wallet_activity.json")
    feed = (json.loads(feed_path.read_text(encoding="utf8"))
            if args.feed or feed_path.is_file() else None)
    html = render(data, feed)
    if args.out.parent.resolve() == args.cache.resolve():
        html = _archive(html, data, path.resolve(), args.cache)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".html.tmp")
    tmp.write_text(html, encoding="utf8")
    tmp.replace(args.out)
    print(args.out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
