# Local native discovery and accounting evidence

The native tools use Dwellir on this PC only. Keep matching HTTPS/WSS URLs in
the ignored `dwellir.txt` file. Neither tool uploads that file or uses it in
GitHub Actions. Use the installed local watcher Python environment for discovery:

```powershell
try_tao_out/watch-env-313/Scripts/python.exe -m seo.tao_native_holders --db try_tao_out/native_holder_candidates.sqlite --page-size 256 --max-pages 20 --rpc-limit 90
```

Repeat with the same database to resume the same finalized block. A different
database starts a new observation. Each verified page commits its checkpoint
and candidate wallets together. This scans current-epoch positive `AlphaV2`
shares; it excludes legacy `Alpha` rows and does not claim a complete holder
census. Shares are not alpha quantities, portfolio values or profit rankings.
The `holder_registry` rows work with `seo.tao_holder_queue`; native rows have
unranked priority and unknown balances. Import attributed history results before
selecting another batch so already researched wallets are not repeatedly chosen.

Taostats remains useful as an indexed source of historical buys, sells and
transfers. A bounded GitHub collection can inspect explicitly selected native
candidates using `addresses_only`; `holders_only` would fetch browser holder
pages again. Exchange and activity screening occurs when those accounts are
inspected. A positive native stake row is not evidence of trading skill.

## Prioritize research across all active subnets

Use the pinned native pool totals to convert shares to exact observed alpha
subtotals, then select wallets in rounds across the chain's active subnet IDs:

```powershell
try_tao_out/watch-env-313/Scripts/python.exe -m seo.tao_native_priority --db try_tao_out/native_holder_candidates.sqlite --history-db path/to/downloaded/wallets.sqlite --out try_tao_out/native-priority.json --limit 64 --rpc-limit 120
```

Both input databases are opened read-only. The output distinguishes the complete
active-network list from the partial holder traversal, and lists networks with
no captured candidates. It excludes inspected or screened accounts and respects
retry delays. Larger observed holdings receive priority within a subnet; alpha
quantities cannot be compared between subnets. These are partial position totals,
not whole-wallet values. The tool makes no TAO-price or minimum-TAO claim.

Send only the selected public addresses to an `addresses_only` GitHub collection.
The number of seeds, existing followed accounts, and permitted extra linked
accounts determines that run's size. Increasing discovery does not automatically
increase the history budget. Partial history still needs deeper research before
cost coverage and trading skill can be established.

## Original cost and stake growth

An increase between two native balances is not automatically a purchase or
reward. Supported growth evidence requires unchanged shares, a compatible pool
generation, complete native staking-hotkey inventory, and complete attributed
indexed history with no intervening movement. It is interval reconciliation,
not an exhaustive native-event index or a valuation of each reward at receipt.
Multiple active hotkeys on the same subnet need a broader proof before this
isolated-position rule can apply.

The ledger preserves the original purchase price and monetary investment,
tracks verified growth units separately, and carries both through transfers.
Effective cost per current alpha differs from the original entry price.
Economic P/L includes growth value/proceeds; trading P/L excludes them, and
growth-containing sales do not count as trading wins. Estimated or unresolved
origin costs remain estimated or unresolved. No synthetic zero-price buy is
created. This is economic analysis, not tax cost-basis accounting.

## Replay a saved artifact without altering it

Use the application's Python environment and explicitly selected, previously
verified local evidence files:

```powershell
.venv/Scripts/python.exe -m seo.tao_local_replay --report path/to/tao_collect.json --db path/to/wallets.sqlite --states path/to/native-states.json --moves path/to/native-move-receipts.json --anchor path/to/native-inventory-anchor.json --out github_out/local-native
```

`--moves` and `--anchor` are optional; repeat `--anchor` for multiple scoped
checkpoints. The command performs no network requests. It writes its own
SQLite database, JSON and HTML report and retains the original inventory and
quote timestamps. The banner distinguishes verification time from observation
time. Newer prices or inventories require a new source snapshot, not changing
the old report's date. Conflicting or incomplete growth evidence fails closed.

Reports under `github_out/local-native*/` can supplement the local directory and
watchlist. Their source hash and observation time must match a downloaded source
report. At the same observation time, the verified replay takes precedence;
a genuinely newer observation takes precedence over the older replay.

## Linked wallet groups

The local watchlist's Groups tab builds connections from saved evidence. Confirmed
stake transfers and documented coldkey swaps can connect wallets. Native TAO
funding requires repeated distinct transactions before it joins a group; a single
funding transfer remains a lead. Inferred snapshot matches never merge groups.
Screened wallets and high-degree funding hubs are excluded from automatic groups,
while their connections remain inspectable.

Connections do not prove common ownership, friendship or coordinated trading.
Empty historical wallets remain visible so their onward transfers can be followed.
Mixed-date groups show partial subtotals from the freshest member observations,
not a combined exact P/L. An older sender's holding must not be counted again at
its newer recipient. Missing original costs remain unresolved, never zero.

Select a group or individual members to watch locally. Broad groups require an
explicit subset of at most 24 members. Group saves are atomic and retain existing
labels, notes and mutes; this action does not fetch missing historical prices.

## Subnet lifecycle and liquidity context

Refresh a separate finalized snapshot locally:

```powershell
try_tao_out/watch-env-313/Scripts/python.exe -m seo.tao_subnet_context --db try_tao_out/native_holder_candidates.sqlite --out github_out/subnet-context.json --rpc-limit 120
```

On subsequent refreshes add `--previous github_out/subnet-context.json` to retain
up to 32 earlier snapshots. The editor notices file changes; no recurring job is
created. The context's chain time and the wallet's observation time remain distinct.
Active and retained IDs come from native maps, augmented by prior observed IDs.
Fully purged historical subnets still require archival evidence.

Generation identity uses the registration block; the registration counter is
additional evidence of ID reuse. A missing newer counter on an older subnet does
not erase its verified registration block. Subnet immunity comes from the live
network parameter, separately from neuron immunity. Dissolving, inactive and
explicitly trading-disabled subnets invalidate current exit valuation claims.

Local risk assessment defaults to a 216,000-block early-stage window, a minimum
1,000-TAO reserve and a 5% marked-position/reserve exposure screen, with 24-hour
freshness limits. These adjustable research thresholds are not protocol rules.
The marked-position/reserve ratio is not slippage. Weighted pool metadata and
reserves alone are not an executable sale quote; no cash-out value is invented.

New and immune subnets remain visible as provisional opportunities. Generation
conflicts with original purchase evidence suppress current open/combined headline
values in the editor, while the saved report and dated realized evidence remain
auditable. Passing context checks does not establish trading skill or safety.
