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
input. The collector checks incoming stake to find the purchasing wallet
and follows outgoing stake up to `--link-depth` hops (default 2).
Linked wallets can add to the wallet count. `--new-wallets`
limits these extra visits to 10 per pass by default. The report lists any
`deferred_recipients` and gives the market page's `recent_trades_status`.
Large scans can exceed a single GitHub job's time limit.

On the project PC, `collect_tao.cmd --addresses-only --address <wallet>`
runs the same collector in local Chrome and writes the database and report
in `try_tao_out/`. Use `--history-pages 15` to look farther back for an
original purchase. `--max-balance-tao` and `--max-activity-24h` also bound
further link discovery. For known native transactions, pass e.g.
`--extrinsic 9344821-0027:198,199` to verify the Taostats detail pages
independently. These records do not imply wallet history coverage.

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
  means the tab supplied no usable result. The workflow saves partial data and flags the run if
  every attempted wallet lacks readable data for either history tab.
* `positions` records quantities per wallet and subnet with their weighted
  buy price when it can be traced. `transfers` includes native TAO and
  stake movements. A stake transfer can carry known basis from the sender
  to the receiver. Observed coldkey swaps and unique matching changes in
  stake are labeled separately. Snapshot matches include a confidence
  score. A missing buy price is shown as null. A watched wallet's native
  TAO transfer can prompt a check for subnet holdings at the destination.
  Only transfers of stake carry the subnet position's original basis.
  For a transfer with a recorded extrinsic, the summary's evidence link
  opens its transaction on Finney, Bittensor's native mainnet. For
  example, transfer `9441573-0003` appears in block 9441573 at extrinsic
  index 3. Snapshot matches show their confidence score instead.
  [View the native transfer](https://taostats.io/extrinsic/9441573-0003)
  and [browse native blocks](https://taostats.io/blocks).
  Taostats also has a separate [EVM block explorer](https://taostats.io/evm/blocks)
  for Ethereum-compatible transactions.
* `traders` lists wallets with costed sales, ordered by observed realized
  TAO before separate fees. The win rate uses only priced sales. See `unpriced_sales` for the
  number omitted, and check how many sales and accounts contributed.
  `--follow-top N` interleaves the strict realized and strict open rankings
  and deduplicates wallets before following them. Partial rankings are for
  research only. A receiving address linked by
  a stake move from a followed wallet can also join the watchlist after
  inspection and balance/activity screening. Pending recipients stay linked
  in the report without joining the watchlist.
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

### Compare wallet performance over time

`tao performance` reads the last saved collection from SQLite.
`tao performance <address> --history 10` shows that wallet's positions
and recent ranks. A collector report also includes this run's
`performance.wallets`, `performance.rankings` and `performance.settings`.
Earlier runs preserve the evidence and thresholds used at the time.

* **Profit** ranks wallets by cumulative gross realized TAO on priced
  sales, before separate fees. To qualify, a wallet needs positive
  realized TAO, wins on more than half its priced sales, complete current
  account and trade coverage, zero unpriced sales, and at least
  `--min-sales` priced sales (default 2). The separate open-gain rank
  can include wallets with no sales.
* **Buy timing** and **sell timing** use the first subnet quote observed
  24 to 30 hours after each execution. A buy is favorable when the price
  rises by at least `--timing-move-pct` (default 5%). A sell is favorable
  when it falls by that much. Each side needs a majority of favorable
  results and at least `--min-timing` evaluated trades (default 3), hits
  on two or more UTC dates, and quotes on at least half the mature trades
  observed on that side. Missing quotes are excluded from the hit rate
  but reduce coverage. The JSON includes each sampled trade's execution
  and forward quote. Taostats pool history and subnet charts supply older
  prices without waiting for new daily scans.
* **Holdings** show the confirmed, estimated and unknown quantities for
  each subnet. `known_buy_price_tao` and `known_unrealized_tao` cover the
  traceable portion even when another part of the position has unknown
  cost. The full `gross_unrealized_tao` requires known basis throughout
  and a current inventory and quote. The report includes the actual
  quote time. A stale quote is visible but cannot price a position in
  this run. Closed subnets still show their realized results.
* **Screening** defaults to a ceiling of 100,000 TAO in total balance and
  100 observed trade and transfer movements in the 24 hours up to the run.
  Set `--max-balance-tao` or `--max-activity-24h` to change a ceiling, or
  set either to 0 to disable it. `screens` records observed limit breaches.
  `evidence_gaps` separately identifies missing information. A capped
  tab can establish the activity count when its contiguous rows cover
  the full 24-hour window. These screens identify balance and activity
  levels. The output makes no claim about who owns a wallet.
* **Transfers** retain the source, destination, subnet, evidence type
  and latest observed extrinsic when available. Each address keeps its
  own profit and ranks, even when its acquisition cost follows the stake.

## Run the wallet collector on GitHub

For a local wallet view, run `view_wallets_github.cmd` (or supply an Actions
run ID as its first argument). This downloads the GitHub wallet artifact,
caches it under `github_out/<run>/`, and opens `github_out/wallets.html`.
To open it again offline, open the HTML directly. To render a saved JSON
without GitHub or Taostats, run `python -m seo.tao_view --input PATH`.
The view includes all inspected accounts below the screens, even when their
trading record is incomplete. Select a wallet to inspect the sale receipts,
positions, acquisition prices, transfers, and quotes behind the timing scores.

The realized column includes gross gain/loss on the confirmed fraction of
partially costed sales. Unknown-cost proceeds are separately identified, never
booked as gains. Same-wallet moves between validators are not market trades.
Only confirmed native moves keep confirmed cost on the moved units; ambiguous
pairs cannot establish a new purchase price. Open positions can qualify
without a sale. Open gains on traceable fractions are shown separately from
fully costed holdings. The source account keeps its trading history even when
stake moves; the receiving account inherits the traceable acquisition cost.

The daily GitHub watchlist reconciliation uses Taostats only. Specify
`watch_addresses` on a manual collection to save wallets on the data branch.
The first readable check of each tab sets a baseline; subsequent checks
record indexed movements, and an unreadable interval remains marked as a gap.
An outgoing TAO transfer is not sale proceeds just because it followed a sale.

For a five-minute movement feed, download the wallet report and then run
`watch_wallets.cmd --setup` once and `watch_wallets.cmd` on your PC.
The local watcher reads the latest cached watchlist and asks for your Dwellir
key in a masked console if it isn't already set locally. Leave that console
running; refresh the HTML to see its latest successful block, lag, and
movements. It reads finalized blocks and resumes from its local checkpoint.
It cannot send five-minute updates while the PC is asleep or off. Local
stake increases/decreases are observations; the Taostats data confirms
trades, stake transfers, and entry prices. The GitHub workflow never installs
or calls Dwellir. See [Dwellir's Bittensor limits](https://www.dwellir.com/docs/bittensor).

Open **Actions → Bittensor wallet collection → Run workflow** and choose
`master`. For the first run, enter `8` for **netuids**, `2` for **top**
and `1` for **history_pages**, or run:

```bash
gh workflow run tao-collect.yml --ref master -f netuids=8 -f top=2 -f history_pages=1 -f delay=2-4
```

If the wallet verification widget stays pending, the artifact can include
one screenshot of the public page and its query state in the JSON.
The run summary shows tab coverage, rows read, screened rankings and up
to five recent transfers with the evidence for each move. It shows fully
priced and unresolved sales separately. Download
**wallet-collection** for the JSON report and
SQLite database. Select the **runner** when dispatching. The default
`ubuntu-latest` runs headed Chrome on a 1366×768 Xvfb virtual display.
Choose `windows-latest` to run Chrome directly on a standard Windows
runner, or pass `-f runner=windows-latest` to the command above. Both
runners share the saved wallet history. The workflow has a 90-minute
time limit and keeps downloadable artifacts for three days. It saves
wallet history to `wallets.sqlite` on the dedicated
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
