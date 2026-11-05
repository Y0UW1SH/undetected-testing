# `tao`: Bittensor subnet stats and price warnings from taostats.io

Collects the subnet table from https://taostats.io/subnets with the
stealth browser, stores every run in SQLite, and warns when a subnet's
alpha price or market cap moves more than a threshold since the last run.

## Quick start

```bash
python -m pip install --upgrade pip wheel setuptools
pip install "git+https://github.com/Y0UW1SH/undetected-testing@master"

tao subnets                      # fetch and print netuid, name, price τ, 24h %, market cap, emission
tao init --netuids 1,8,19 --threshold 10   # seo/config/tao.json
tao watch                        # fetch again, compare with the previous run, print WARNING lines
tao subnet 8                     # latest row for one subnet plus its price history
tao report                       # data/tao_report.html
tao export --format csv          # data/subnet_stats.csv
```

`tao watch --fail-on-alert` exits 2 when any alert fires, which makes it
usable as a check in cron or CI. Under GitHub Actions each alert is also
emitted as a `::warning::` annotation. Alerts are written to
`data/tao_alerts.json` with the threshold and the run they were compared
against.

## How alerts are defined

Each run is one batch (same `fetched_at`). `watch` compares the newest
batch with the one before it, subnet by subnet:

* `price_up` / `price_down`: alpha price in TAO moved by at least the
  threshold percent (default 10, or `alert_threshold_pct` in the config).
* `mcap_up` / `mcap_down`: market cap in TAO moved by at least the
  threshold.
* `new` / `gone`: a subnet appeared in or disappeared from the list.

`--netuids 1,8,19` (or `netuids` in `seo/config/tao.json`) limits the
alerts and the printed table to those subnets. The first run only records
data; alerts start with the second.

## Schedule

`.github/workflows/tao.yml` runs `tao watch` every 4 hours, commits
`data/tao.sqlite`, `data/subnet_stats.csv`, `data/tao_report.html` and
`data/tao_alerts.json` back to the repo, and uploads them as an artifact.
Dispatch it by hand with a threshold and netuids. Set a `TAO_PROXY`
secret (`user:pass@host:port`) to route through a proxy.

## Parser notes

The parser was written before a real taostats page had been captured. It
finds any table (or ARIA grid) whose header mentions a price, maps the
columns by header names (netuid, name/subnet, price, 24h, 7d, market cap,
emission, volume, pool/liquidity), reads numbers like `τ1.2M`, `+12.4%`
and `−3.1%`, and keeps the whole row as JSON in the `raw` column. If a
run saves nothing, capture the live page and compare it with
`tests/fixtures/taostats_subnets.html`:

```bash
seo capture taostats --out data/live_taostats.html
# or dispatch the SEO tracking workflow with capture_extra "taostats:"
```


## Parser status

The subnet list on taostats.io is an AG Grid rather than an HTML table.
The parser reads the grid's `col-id` cells (`price`,
`price_change_one_day`, `market_cap`, `emission_total_pct`, `liquidity`,
...) and joins the pinned name column ("lium.io SN51") with the data
columns by row index. It was validated against a live capture from a
GitHub Actions runner; the grid shows about 25 rows at a time, so
`tao subnets` scrolls the grid viewport and merges rows by netuid until
no new ones appear. Refresh the fixture with the SEO workflow's
`capture_extra` input (`taostats:`) or `seo capture taostats --out ...`.
