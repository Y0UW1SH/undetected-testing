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
