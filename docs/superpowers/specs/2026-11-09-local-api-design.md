# Browser-free local source validation

The Windows project remains the source checkout on `feat/windows-local-runner`
(PR #2), not an installed Windows service or a new EXE. Validate its core on the
available Debian desktop and provide a safe Windows smoke-test entry point.

TAO.com is a candidate public HTTP source. Live read-only probes returned price,
subnet detail and trade data without authentication. Hosted OpenTaoAPI currently
has subnet endpoints but returns 404 for trader rankings and stake events.

Use a separate, explicit `api-collect` command for bounded TAO.com observations.
This is an implementation choice: preserve source-specific raw receipts and
provenance rather than silently replace the Taostats-backed wallet ledger. A
full backend switch would need event identity, transfer semantics, historical
coverage and cost-basis reconciliation that a few successful reads do not prove.

Do not load the user's watchlist implicitly, send credentials, follow arbitrary
pagination links, merge databases, schedule work, publish private observations,
or treat API ranking/performance figures as verified trading profit. Preserve
both recovery ZIPs unchanged. Failed reads must not replace the last good report.

Use fixed HTTPS endpoints, bounded request sizes/timeouts/page counts, retained
raw units and timestamps, explicit incomplete-coverage labels and checksums.
Save observations outside the checkout and render a separate read-only summary.
Native Windows validation stays pending until a Windows host executes it.
