# Taostats account census

The census reads Taostats' public **Accounts** directory, ordered by total
balance across networks. Each directory row records an address, network,
observed balance, rank and block when available. The JSON reports distinct
coldkeys separately from address/network pairs. Wallet histories, transfer
ties, and profitable trades require the separate wallet collector.

## Run on your PC

Use [the Windows migration guide](local-windows.md). Restore the historical
checkpoint once before starting. `run_local.cmd census` scans one resumable
chunk (eight sequential pages of 100 rows by default), then stops.
`run_local.cmd census --pages 100` advances a larger bounded chunk. Repeated
runs resume the saved database. The local scheduler can run one chunk daily.
There are no Actions runs, auto-dispatched successors or expiring artifacts.

`view_tao_directory.cmd` now builds the view from local `accounts.sqlite` and
saved reports. It does not download or follow GitHub checkpoints. The directory
shows holders and balances; only wallets joined to history have performance
coverage. See the report for next-page and completeness information.

## Restore a local SQLite database

From a normal code checkout with project requirements installed, clone the
checkpoint branch once. Restore from its latest generation.

```console
git clone --depth 1 --single-branch --branch tao-account-registry --no-tags https://github.com/Y0UW1SH/undetected-testing.git ../census-state
python -m tools.tao_census_workflow restore --state ../census-state --db data/census.sqlite
```

Keep the migration clone unchanged as an original backup. Restore to a new
filename to compare generations; do not replace an active local database.
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
