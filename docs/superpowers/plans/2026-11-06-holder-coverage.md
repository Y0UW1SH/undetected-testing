# Holder coverage implementation plan

**Goal:** Discover subnet holders broadly, then inspect different wallets on successive GitHub runs instead of repeatedly sampling six holders per subnet.

**Design:** Keep the established Taostats browser and persistent wallet SQLite database. In holders-only mode, capture every valid positive holder row from a bounded number of consecutive pages, checkpoint each verified page, and retain a deduplicated research queue across runs. Select new wallets fairly across the requested subnets. Current holdings identify candidates; they do not prove profit. Preserve the existing historical ledger, current unrealized P/L, transfer tracing, and exchange screening.

**Choices:** A larger fixed sample would still repeat wallets. A resumable queue advances coverage while keeping each Actions run bounded. Use the queue. Start validation on SN1–5, retain support for all subnets, and leave five-minute Dwellir monitoring local. No repository visibility or billing changes are authorized by a question about them.

**Updated source preference:** The user subsequently chose Dwellir wherever practical, with Taostats through GitHub only for remaining data needs. Keep this browser collector as a bounded fallback. Do not launch the planned GitHub holder scan before checking native holder discovery. Dwellir has now returned current stake storage metadata and historical block events locally; this proves raw access, not a completed wallet-history index.

## Work

- [x] Add browser holder-page collection and persistent page checkpoints; reject malformed, stale, wrong-subnet, or incomplete pages without advancing. Preserve deduplication across hotkeys and subnets and label moving-page coverage honestly.
- [x] Queue all captured positive holders; select round-robin across subnets, excluding wallets with already usable inspected history, and give unavailable attempts a retry delay.
- [x] Connect holders-only collection, expose a bounded holder-page budget in CLI/Actions, and report captured/pending/inspected counts separately.
- [x] Clarify historical timing labels and keep confirmed transfer destinations visible for monitoring; record where Chef's funds went without claiming shared ownership.
- [x] Test page resumption, duplicate holders, failed pages, fair queue progress, and existing collector behavior. Review also fixed empty partial histories being treated as finished and failure paths losing saved retry progress. Final local suite: 324 passed; lint clean.
- [ ] GitHub holder scan deferred by the updated Dwellir-first source preference. No new holder scan was launched. The browser path remains a tested fallback, not a claimed live collection.

## Native evidence findings

Local read-only archival RPC returned actual purchase quantities, verified three
same-subnet stake moves, and established a zero SN28 balance before an example
wallet's first incoming transfer. The ledger now accepts subnet-scoped historical
inventory anchors, preventing later balance differences from being invented as
earlier holdings. Scoped anchors do not erase other assets or imply transfers.

Another transfer intermediary's unchanged shares grew from 33,213 to
35,929.888943380 alpha before onward transfer. Dated stake-growth accounting
now preserves original monetary cost and purchase prices through transfers.
It separates growth proceeds/value from trading gains. Native whole-subnet
inventory and complete indexed movement history are required for the supported
growth proof; conflicting intermediate snapshots require splitting intervals.
Unclassified differences remain unresolved, never zero-cost purchases.

The local AlphaV2 scanner now validates raw storage keys, share epochs, pinned
block pagination, and atomic checkpoints. It adds distinct coldkey/subnet
candidates to the shared holder research queue without pretending shares are
balances. Legacy Alpha rows are explicitly outside this first scanner's scope.
A bounded live scan read 5,120 rows and found 3,799 distinct candidates. These
are unassessed candidates, not profit-ranked wallets. History-only batches can
use explicit addresses on GitHub; native discovery and credentials stay local.

Offline native replay writes a separate database and report, preserving source
artifact event membership, account observation batches and quote timestamps.
Review caught and fixed source-cutoff errors, interior snapshot cost erosion,
and verified native one-nano rounding moves falsely treated as trades.
Application validation: 398 tests passed; flake8 clean.

## Broad research and linked watchlists

- [x] Convert observed native shares with exact pinned pool totals, rank within
  each subnet, and rotate across every active network ID. Expose missing networks
  and partial discovery rather than claiming a complete census.
- [x] Use latest history to avoid repeated seed research, with inspected,
  screened and retry-wait accounts excluded. Keep discovery distinct from
  verified profit and from a bounded GitHub history batch.
- [x] Build evidence-preserving transfer groups, retain empty historical wallets,
  exclude hubs from automatic merging, and prevent stale sender holdings being
  counted twice in mixed-date group totals.
- [x] Add local group/member selection with atomic preference updates and retained
  mutes. Require explicit bounded member selection for broad networks.
- [x] Prefer validated local native replay at the same source observation time in
  directory/watchlist views; newer source observations still supersede old replay.

The first native-seeded history validation inspected 26 accounts: ten new seeds,
six followed accounts and ten linked accounts. That was an explicit batch budget,
not a source limit. Broader selection now prioritizes material observed holdings
instead of alphabetical addresses. Validation: 446 application tests passed;
lint clean. Deeper historical backfill and complete native holder traversal
remain ongoing research work, not claimed complete by these tests.

## Constraints

- Never read, publish, or use Dwellir credentials in GitHub Actions.
- No extra Actions schedule or unbounded continuation loop.
- Failed history or missing costs must remain unknown, never zero profit or loss.
- Existing private watch/mute preferences must be preserved.
- Holder rankings change while pages are fetched: a completed traversal is not a fixed-block census.
