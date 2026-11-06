# Taostats account census

The census reads Taostats' public **Accounts** directory, ordered by total
balance across networks. Each directory row records an address, network,
observed balance, rank and block when available. The JSON reports distinct
coldkeys separately from address/network pairs. Wallet histories, transfer
ties, and profitable trades require the separate wallet collector.

## Run on GitHub

Start one chunk from the code checkout. The default is eight sequential pages
of 100 rows each. The scanner also samples the last page when needed.

```console
gh workflow run tao-census.yml --ref master -f pages=8 -f delay=2
```

To traverse the rest of the public directory, start one finite series.

```console
gh workflow run tao-census.yml --ref master -f mode=full -f delay=2 -f full_chunks=80
```

Full mode saves each chunk of up to 100 pages to the `tao-account-registry`
branch before starting another. One runner handles up to four chunks, then
queues one successor if the directory still has an unverified page. The
chunk budget decreases across the series. A failed read, failed save, stale
continuation, stalled cursor, or exhausted budget stops the series. To resume
after inspecting the error, start a fresh dispatch. Only one census workflow
can run at a time. Avoid direct edits to the checkpoint branch.

Each scan runs on a standard GitHub-hosted Ubuntu runner. Standard hosted
compute is free for public repositories. Private repositories draw from their
plan's included compute. Artifact storage has a separate allowance. [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

The job summary shows the latest checkpoint generation and the next page.
Intermediate full runs upload a small JSON report for one day. Manual runs
and terminal full runs offer the SQLite result and JSON for three days.
The checkpoint branch keeps the cumulative data after artifacts expire.

On this PC, double-click `view_tao_directory.cmd` to download the latest saved
checkpoint and open `github_out/accounts.html`. Use `-Offline` to reuse your
last download, or `-Follow -NoOpen` to update the view as new checkpoints
arrive (up to 12 hours). This command doesn't start GitHub jobs. The directory
shows holders and their balances; only the smaller group joined to wallet
history has evidence for trading performance.

## Restore a local SQLite database

From a normal code checkout with project requirements installed, clone the
checkpoint branch once. Restore from its latest generation.

```console
git clone --depth 1 --single-branch --branch tao-account-registry --no-tags https://github.com/Y0UW1SH/undetected-testing.git ../census-state
python -m tools.tao_census_workflow restore --state ../census-state --db data/census.sqlite
```

On later downloads, use `git -C ../census-state pull --ff-only`. Add
`--replace` when refreshing an existing local database, or give it a new name.
To reproduce an earlier checkpoint, choose its generation from a run summary
or the ordered `generations` list in `census-checkpoint.json`.

```console
python -m tools.tao_census_workflow restore --state ../census-state --db data/census-generation-3.sqlite --through 3
```

Generation 1 contains a full baseline. Later generations contain changed
pages and accounts, the current directory metadata and a SHA-256 digest.
Restoration validates the parts and the resulting SQLite database before
installing the output. Older branch revisions containing `accounts.sqlite`
are also readable by the same command.

## Interpret coverage

`partial` gives the next unverified page. `traversed_live` means all observed
page numbers were covered while the directory could move between reads.
Rank changes can cause accounts to repeat or move over a page boundary.
The registry keeps previously observed accounts. `complete_single_block`
requires a concordant total and unique accounts on verified pages from one
network and the same anchor block. The directory can change after that block.
