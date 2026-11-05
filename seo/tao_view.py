"""Build an offline wallet report from a GitHub artifact."""
import argparse
from datetime import datetime, timezone
from functools import lru_cache
from html import escape
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import quote


REPO = "Y0UW1SH/undetected-testing"
WORKFLOW = "tao-collect.yml"


class SnapshotError(Exception):
    """Safe to print without exposing GitHub CLI diagnostics or credentials."""


def _gh(args, timeout):
    # Let gh use its normal locally configured login. Only pass the OS paths
    # needed to run gh and locate its config; never forward watcher secrets.
    paths = {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "HOME", "USERPROFILE",
             "APPDATA", "LOCALAPPDATA", "TMP", "TEMP", "TMPDIR"}
    env = {k: v for k, v in os.environ.items() if k.upper() in paths}
    env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1",
               GH_HOST="github.com")
    try:
        return subprocess.run(
            ["gh", *args], check=True, timeout=timeout, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except subprocess.TimeoutExpired:
        raise SnapshotError("GitHub snapshot check timed out.") from None
    except (OSError, subprocess.CalledProcessError):
        raise SnapshotError("GitHub snapshot unavailable. Check the local "
                            "gh login and repository access.") from None


def _latest_artifact():
    # A finished workflow may have uploaded a report and then failed its
    # coverage check, or skipped the watchlist job with no artifact at all.
    runs = json.loads(_gh(["run", "list", "-R", REPO, "-w", WORKFLOW,
                           "--branch", "master", "--status", "completed",
                           "-L", "30", "--json", "databaseId"], 12))
    artifacts = json.loads(_gh([
        "api", "repos/%s/actions/artifacts?name=wallet-collection&per_page=100"
        % REPO, "--jq", "[.artifacts[] | select(.expired == false) | "
        "{run: .workflow_run.id, id: .id, created: .created_at}]"], 12))
    completed = {row["databaseId"] for row in runs}
    candidates = []
    for artifact in artifacts:
        run, aid = artifact["run"], artifact["id"]
        if run in completed and type(run) is int and type(aid) is int:
            candidates.append((artifact.get("created") or "", aid, run))
    if candidates:
        _, aid, run = max(candidates)
        return str(run), str(aid)
    raise SnapshotError("No retained wallet artifact from the latest "
                        "completed collections.")


def _report_date(data):
    if not isinstance(data, dict) or not isinstance(data.get("performance"),
                                                    dict):
        raise ValueError("invalid wallet snapshot")
    wallets = data["performance"].get("wallets")
    if (not isinstance(wallets, list) or any(
            not isinstance(w, dict) or not isinstance(w.get("address"), str)
            for w in wallets)):
        raise ValueError("invalid wallet list")
    rankings = data["performance"].get("rankings", {})
    if not isinstance(rankings, dict):
        raise ValueError("invalid rankings")
    for group in (data.get("following", []), rankings.get("buy", []),
                  rankings.get("sell", [])):
        if not isinstance(group, list) or any(
                not isinstance(a, str) for a in group):
            raise ValueError("invalid wallet selections")
    if not isinstance(data.get("mode", ""), str):
        raise ValueError("invalid snapshot mode")
    stamp = data.get("checked_at")
    if not isinstance(stamp, str):
        raise ValueError("missing scan timestamp")
    date = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("scan timestamp must have a timezone")
    return date.astimezone(timezone.utc)


def load_snapshot(path):
    data = json.loads(path.read_text(encoding="utf8"))
    _report_date(data)
    return data


@lru_cache(maxsize=256)
def _cached_date(path, modified, size):
    try:
        return _report_date(load_snapshot(path))
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _date(path):
    try:
        stat = path.stat()
        return _cached_date(path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def run_snapshot(folder):
    """Newest valid scan in a run, including later workflow attempts."""
    candidates = []
    for path in folder.rglob("tao_collect.json"):
        date = _date(path)
        if date is None:
            continue
        parts = path.relative_to(folder).parts
        generation = parts[1].split("-")[0] if parts[0] == "artifacts" else ""
        aid = int(generation) if generation.isascii() and (
            generation.isdigit()) else 0
        candidates.append((date, aid, path))
    return max(candidates)[2] if candidates else None


def newest_cached(cache):
    candidates = []
    for folder in cache.glob("*"):
        if folder.is_dir() and folder.name.isascii() and folder.name.isdigit():
            path = run_snapshot(folder)
            if path is not None:
                candidates.append((_date(path), int(folder.name), path))
    return max(candidates)[2] if candidates else None


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", delete=False, dir=path.parent,
                                     encoding="utf8") as fh:
        tmp = Path(fh.name)
        try:
            fh.write(content)
            fh.close()
            tmp.replace(path)
        finally:
            fh.close()
            tmp.unlink(missing_ok=True)


def fetch(run, cache):
    artifact_id = None
    if run == "latest":
        run, artifact_id = _latest_artifact()
    if not run.isascii() or not run.isdecimal():
        raise SnapshotError("Use 'latest' or a numeric GitHub Actions run ID.")
    folder = cache / run
    existing = run_snapshot(folder)
    if existing and artifact_id is None:
        return existing
    if existing:
        for report in folder.rglob("tao_collect.json"):
            marker = report.with_name(".wallet-artifact-id")
            if (marker.is_file() and marker.read_text().strip() == artifact_id
                    and _date(report) is not None):
                return existing
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=cache) as temp:
        stage = Path(temp) / "payload"
        stage.mkdir()
        _gh(["run", "download", run, "-R", REPO, "-n", "wallet-collection",
             "-D", str(stage)], 45)
        files = list(stage.rglob("tao_collect.json"))
        if len(files) != 1 or _date(files[0]) is None:
            raise SnapshotError("Downloaded artifact has no valid wallet "
                                "snapshot.")
        report = files[0]
        if artifact_id:
            report.with_name(".wallet-artifact-id").write_text(artifact_id)
        # Publish the complete downloaded tree in one rename. In a rerun,
        # retain the prior JSON, SQLite and diagnostics as a consistent set.
        target = folder
        if folder.exists():
            name = artifact_id or Path(temp).name
            target = folder / "artifacts" / name
            if target.exists():
                target = target.with_name(name + "-" + Path(temp).name)
            target.parent.mkdir(parents=True, exist_ok=True)
        stage.rename(target)
        return run_snapshot(folder)


def refresh_latest(cache):
    """Best usable local report even if the remote is older or unavailable."""
    warning = None
    try:
        fetch("latest", cache)
    except SnapshotError as exc:
        warning = str(exc)
    except (KeyError, TypeError, OSError, ValueError, OverflowError):
        warning = "Could not read the latest GitHub wallet artifact."
    return newest_cached(cache), warning


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


def _archive(html, data, source, cache, output_dir=None):
    """Link saved snapshots from the live view, keeping archives scan-only."""
    try:
        run = source.resolve().relative_to(cache.resolve()).parts[0]
    except (ValueError, IndexError):
        run = None
    index = cache / "wallet_views.json"
    try:
        rows = json.loads(index.read_text(encoding="utf8"))
        if not isinstance(rows, dict):
            rows = {}
    except (ValueError, OSError):
        rows = {}
    changed = False
    if run and run.isascii() and run.isdecimal():
        archive = cache / run / "wallets.html"
        info = [data["checked_at"], data.get("mode", "collection")]
        if (rows.get(run) != info or not archive.is_file()
                or source.stat().st_mtime_ns > archive.stat().st_mtime_ns):
            _write(archive, render(data))
            rows[run] = info
            changed = True
    # Two viewers may publish at once. Recover orphaned archived HTML the
    # next time navigation is built, including after an interrupted write.
    for page in cache.glob("*/wallets.html"):
        rid = page.parent.name
        if not rid.isascii() or not rid.isdecimal() or rid in rows:
            continue
        report = run_snapshot(page.parent)
        if report:
            saved = load_snapshot(report)
            rows[rid] = [saved["checked_at"], saved.get("mode", "collection")]
            changed = True
    if changed:
        _write(index, json.dumps(rows))
    links = []
    for rid, info in rows.items():
        if (rid.isascii() and rid.isdecimal() and isinstance(info, list)
                and len(info) == 2 and all(isinstance(x, str) for x in info)
                and (cache / rid / "wallets.html").is_file()):
            stamp, mode = info
            page = (cache / rid / "wallets.html").resolve()
            try:
                relative = os.path.relpath(page, output_dir or cache)
                href = quote(Path(relative).as_posix())
            except ValueError:  # Windows paths on different drives
                href = page.as_uri()
            links.append((stamp, int(rid), f'<li><a href="{escape(href)}">'
                          f'{escape(stamp[:19])} · {escape(mode)}</a></li>'))
    if not links:
        return html
    nav = '<details><summary>Saved snapshots</summary><ul>' + "".join(
        link for _, _, link in sorted(links, reverse=True)) + '</ul></details>'
    return html.replace("</header>", nav + "</header>", 1)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--github", metavar="latest|RUN_ID")
    source.add_argument("--input", type=Path, help="downloaded JSON")
    p.add_argument("--cache", type=Path, default=Path("github_out"))
    p.add_argument("--out", type=Path, default=Path("github_out/wallets.html"))
    p.add_argument("--feed", type=Path, help="local movement feed JSON")
    args = p.parse_args(argv)
    try:
        path = args.input or fetch(args.github, args.cache)
    except SnapshotError as exc:
        raise SystemExit(str(exc)) from None
    data = json.loads(path.read_text(encoding="utf8"))
    feed_path = args.feed or Path("try_tao_out/local_wallet_activity.json")
    feed = (json.loads(feed_path.read_text(encoding="utf8"))
            if args.feed or feed_path.is_file() else None)
    html = render(data, feed)
    if args.out.parent.resolve() == args.cache.resolve():
        html = _archive(html, data, path, args.cache)
    _write(args.out, html)
    print(args.out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
