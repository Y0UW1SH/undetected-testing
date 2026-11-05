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

## Collect wallets and transfers

`tao collect` runs the subnet, metagraph and wallet scans in one browser
session. It discovers miners and validators on the selected metagraphs
and samples recently active traders on the public delegation page. It
saves subnet prices, wallet balances and positions. It requests each wallet's staking
transactions and native TAO transfers from the account page. The collector
uses the Taostats website through Chrome. No API key is required.

Start with one subnet and two candidate wallets.

```bash
tao collect --netuids 8 --top 2 --history-pages 1 --delay 2-4
```

For a wider pass, select all non-root subnets with `--subnets 0` (the CLI
default). Use `--netuids 8,64` for specific subnets. `--top 20` selects
up to 20 discovered candidates. `--top 0` selects all discovered
candidates. Followed wallets are included even when they fall
outside the selection. Include a specific wallet with `--address <address>`
(repeatable), or enter additional addresses in the workflow's **addresses**
input. Transfer recipients can add to the wallet count. `--new-wallets`
limits these extra visits to 10 per pass by default. The report lists any
`deferred_recipients` and gives the market page's `recent_trades_status`.
Large scans can exceed a single GitHub job's time limit.

`--history-pages` sets the page limit for each tab, starting with the
newest activity. A limit of 1 is useful for checking site access. Raising
the limit retrieves older activity when the site serves it. Set `--delay 2`
for a fixed pause or `--delay 2-4` for a range in seconds. To set the
default in PowerShell, use `$env:TAO_DELAY = '2-4'`.

The default database is `data/tao.sqlite`. For a separate local wallet
history, pass the same `--db` before each command.

```bash
tao --db data/wallets.sqlite follow <address> --label research
tao --db data/wallets.sqlite collect --subnets 8 --top 20 --follow-top 3 --out data/tao_collect.json
tao --db data/wallets.sqlite following
```

`tao wallets --by earnings` ranks mining and validator rewards.
`--by growth` ranks balance changes, which include deposits. For trade
performance, open the collector's `tao_collect.json` report.

* `coverage` records what loaded for each account in this pass. A complete
  empty result means the site returned no rows. Partial means the scan
  reached a page limit or could read only part of the history. Unavailable
  means the tab supplied no usable result. A successful GitHub job can
  still report unavailable history.
* `positions` records quantities per wallet and subnet with their weighted
  buy price when it can be traced. `transfers` includes native TAO and
  stake movements. A stake transfer can carry known basis from the sender
  to the receiver. Observed coldkey swaps and unique matching changes in
  stake are labeled separately. Snapshot matches include a confidence
  score. A missing buy price is shown as null. A watched wallet's native
  TAO transfer can prompt a check for subnet holdings at the destination.
  Only transfers of stake carry the subnet position's original basis.
* `traders` lists wallets with costed sales, ordered by observed realized
  TAO before separate fees. The win rate uses only priced sales. See `unpriced_sales` for the
  number omitted, and check how many sales and accounts contributed.
  `--follow-top N` adds up to N wallets with a complete current transaction
  tab, zero unpriced sales, positive gross realized TAO, at least two priced
  sales and a win rate above 50%. A receiving address linked by
  a stake move from a followed wallet can also join the watchlist.
* Trade timing compares a buy or sale with the first available subnet
  quote 24 to 30 hours later. A subsequent rise counts in favor of a buy,
  and a fall counts in favor of a sale. `evaluated` is the number with a
  qualifying later quote. This can stay at zero until more price snapshots
  are collected. Establishing who traded first would require other
  traders' fills.

`tao track --threshold 5` refreshes the watchlist, compares balances with
the previous run, and writes balance alerts to `data/tao_wallet_alerts.json`.
`tao unfollow <address>` removes a wallet. The collector produces a
research report each time it runs.

## Run the wallet collector on GitHub

Open **Actions → Bittensor wallet collection → Run workflow** and choose
`master`. For the first run, enter `8` for **netuids**, `2` for **top**
and `1` for **history_pages**, or run:

```bash
gh workflow run tao-collect.yml --ref master -f netuids=8 -f top=2 -f history_pages=1 -f delay=2-4
```

The run summary shows tab coverage, rows read and cumulative priced and
unpriced sales. Download **wallet-collection** for the JSON report and
SQLite database. This workflow runs manually on standard `ubuntu-latest`,
has a 90-minute time limit, and keeps downloadable artifacts for three
days. It saves wallet history to `wallets.sqlite` on the dedicated
`tao-wallet-data` branch so it can be restored after artifact expiry.
The repository must allow Actions to write that branch. This repository
is public, so the followed addresses and saved results are public too.

GitHub charges no runner time for standard hosted runners on public
repositories. Private repositories draw eligible runner time from the
owner's included compute allowance, and storage has its own allowance.
The CLI can also run directly on a local computer. [GitHub Actions
billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

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
