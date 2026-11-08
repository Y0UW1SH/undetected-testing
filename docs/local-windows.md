# Run everything on a Windows PC

All six GitHub Actions workflows have been removed: CI, SEO, product research,
subnet prices, wallet collection and account census. Collection now runs only
when you start it or explicitly install a local schedule. No runtime GitHub
login, Actions minutes, artifact retention or automatic Git commits are needed.
Test source and historical data branches are retained. Removing workflows does
**not** erase previously public data or cancel a run already executing on GitHub.
These changes take effect for hosted schedules when this branch is merged.

## Setup

Use Windows 10/11 x64, official **Python 3.13** with the Python launcher, and
Google Chrome. Keep this source checkout in a stable folder. In a console:

```bat
setup_local.cmd
run_local.cmd check
```

Setup creates `.venv-local`, installs the project and collector/RPC dependencies,
and initializes private configuration. It does not start collection, monitoring
or scheduling. Chrome must already be installed. The first browser run may
install SeleniumBase's matching driver. Python 3.13 is used for Windows crypto
wheels. This is a **source-checkout setup**, not an updated EXE installer. The
older `packaging/windows` installer bundles the viewer, not this collector.

Default data root: `%LOCALAPPDATA%\StealthWalletMonitor\LocalRunner`.
Set `TAO_LOCAL_HOME` before setup for a different root, or use:

```bat
run_local.cmd --root "D:\Private Data\Tao" init
```

Use that same root for every command and schedule. Keep it outside the public
Git checkout, OneDrive/public shared folders and disposable downloads folders.
The root contains databases, config, snapshots, reports, private preferences,
logs, backups and checkpoints. Keep credentials out of configuration JSON.

## Bring existing data before starting new collection

Stop old collectors, the old monitor and its editor first. Keep the original
ZIP/checkpoint and existing directories unchanged. A downloaded
`wallet-collection` artifact includes `wallets.sqlite` and `tao_collect.json`:

```bat
run_local.cmd import-artifact "D:\Backups\wallet-collection.zip"
```

Import checks the report and SQLite database and keeps the unmodified ZIP plus
SHA-256 under `imports`. Existing wallet databases/reports are never overwritten;
if there is a conflict, import into a **new** `--root` and compare the results.
Ordinary import failures roll back newly published live files while keeping the
original ZIP and extracted inputs. A force-killed import may leave an incomplete
set of fully written files; preserve that directory and retry in a fresh root.
ZIP and database processing is streamed, with no old 512 MiB backup limit.
Allow space for the original ZIP, extracted original, active DB and backups.

### Compare two snapshots before choosing a working state

A later artifact is not necessarily a superset of an earlier one. Saved event
IDs can disappear, and the payload for the same ID can be revised. History
checkpoints, inventory, monitor cursors and derived performance depend on the
matching rows from their own snapshot. Do not combine tables with `INSERT OR
REPLACE`, select maximum cursors, or copy the newer database over the older one.

```bat
run_local.cmd compare-artifacts "D:\Backups\earlier.zip" "D:\Backups\later.zip" --to "D:\Backups\comparison-new"
```

This command creates a **new** preservation bundle, refusing an existing target.
It keeps each original ZIP, its exact extracted database/report and SHA-256
checksums in separate `source-1` and `source-2` folders. It verifies ZIP member
reads, SQLite integrity, expected wallet-event columns and report structure
before publishing the bundle.
Nonempty WAL/journal sidecars are rejected rather than silently discarded.

`comparison.json` reports all ordinary tables: row counts, missing tables,
schema mismatches, duplicate-aware full-row differences and, where possible,
missing/changed declared primary keys. An integer primary key can be a local
sequence number, not a shared event identity. Different JSON formatting is
conservatively treated as different data. Matching rows do not prove complete
coverage or that the database and report are semantically consistent.

**No databases are merged and no active source is selected.** Both original
coverage reports remain unchanged. Inspect differences before explicitly
importing a selected ZIP into a fresh working root. Keep the other original
available for later evidence recovery. This tool does not claim a lossless merge
or reconstruct missing history. `manifest.json` checksums every preserved file
and the comparison. Keep this entire bundle private and outside the repository.

To resume the historical account-directory checkpoint, clone its state branch
**once**, then restore locally. These are migration commands, not recurring jobs:

```bat
git clone --depth 1 --single-branch --branch tao-account-registry --no-tags https://github.com/Y0UW1SH/undetected-testing.git "D:\Backups\census-state"
run_local.cmd restore-census "D:\Backups\census-state"
```

This verifies checkpoint digests, reconstructs `accounts.sqlite` and refuses to
replace an existing file. Older checkpoints containing `accounts.sqlite` also
work. The branch itself is untouched. If wallet artifact retention has expired,
the `tao-wallet-data` branch may still contain a database; retain an untouched
clone and seek a matching saved report before importing. A DB alone is not a
complete viewer snapshot.

Existing subnet history (`data/tao.sqlite`), SEO history (`data/ci.sqlite`, renamed
to `seo.sqlite`) and research history (`data/research.sqlite`) can be migrated by
copying their **closed** databases into a new root, without replacing existing
files. Keep source files unchanged. Copy private watch preferences only after
stopping the editor and watcher: `try_tao_out/local_watchlist.sqlite` belongs at
that same relative path in the new root. Legacy movement data requires the same
care; do not merge two live SQLite files by copying one over the other.

## Collect, inspect and monitor

Edit `<root>\config\local.json`. Each job has an argument list, using the existing
collector options. `collect` defaults to a small watched-wallet refresh. Import
old history first, or add a `--watch-address` with a valid coldkey. For discovery,
remove `--addresses-only` and choose bounded `--netuids`, `--subnets`, `--top`,
`--new-wallets` and history limits. Use `--holders-only` for resumable holder pages.
Do not combine the two modes. CLI options override scalar config values.

```bat
run_local.cmd collect
run_local.cmd subnets --netuids 8,19 --threshold 10
run_local.cmd census --pages 20
run_local.cmd view
run_local.cmd directory
run_local.cmd edit
run_local.cmd watch
run_local.cmd status
```

Census runs **one bounded, resumable chunk** (8 pages by default, 1–100 allowed).
Run it again to advance; a full directory can take many chunks. No unbounded
successor jobs are dispatched. A completed process does not mean the directory
or wallet history is complete: read the report's coverage, missing history and
next-page fields. The collector never fabricates missing trading costs.

`watch` reads the atomically updated local report every poll, with no GitHub
refresh. Collect or import once before starting it. It polls finalized chain
activity every five minutes, resumes its saved checkpoint, and reads private
watch preferences. It can retain last-good report data during a transient read
failure. The feed is **not** a substitute for indexed historical collection.
The HTML is a saved view; refresh the browser after a new poll.

Set the provider key locally in `DWELLIR_API_KEY`, use `DWELLIR_WS_URL`, or put
matching Dwellir Finney HTTPS/WSS endpoints in `<root>\dwellir.txt`. Never put keys
in Git, command arguments, scheduler task definitions or chat. Foreground watch
prompts with hidden input if no local credential is available. Scheduled
`watch --once` cannot prompt: provision its local credential first, then test a
one-shot poll yourself. The managed backup excludes `dwellir.txt` and environment variables. Secrets
manually placed in configuration or reports would still be copied; keep them out.
Ctrl+C stops the foreground watcher. Stop a scheduled watcher through Task
Scheduler; removing its schedule prevents later invocations but does not kill an
already-running process. The old `stop_wallet_watcher.cmd` only handles legacy
hidden monitors, not the new foreground runner.

Compatibility launchers `collect_tao.cmd`, `watch_wallets.cmd`,
`view_wallets_github.cmd`, `run_tao_github.cmd`, `edit_watchlist.cmd` and
`view_tao_directory.cmd` now point to this local root. The old GitHub-named files
are aliases only. `view_wallets_github.cmd` no longer accepts Actions run IDs.
Old `try_tao.cmd` / `view_tao.cmd` demos and the EXE launcher are separate legacy
entrypoints; use `run_local.cmd` for this migration.

## Optional local jobs and manual checks

Edit `<root>\config\keywords.json` before SEO and
`<root>\config\research.json` before product tracking. Examples are placeholders.

```bat
run_local.cmd seo
run_local.cmd research
.venv-local\Scripts\python.exe -m pytest tests\ -q
.venv-local\Scripts\python.exe -m flake8 . --exclude=.venv-local,temp
```

SEO/research exports and reports are local. The former SEO canary and optional
capture operations remain explicit CLI commands rather than automatic scheduled
traffic; run `.venv-local\Scripts\python.exe -m seo --help` for details and provide
absolute `--db`/`--out` paths under your private root. Raw browser examples and
Playwright source also remain available for manually chosen live tests. They are
not automatically run or scheduled. Do not solve a site challenge automatically;
stop and inspect its response.

## Optional scheduling

From PowerShell in the checkout, after a successful manual run:

```powershell
.\tools\schedule_local.ps1 install
.\tools\schedule_local.ps1 status
.\tools\schedule_local.ps1 remove
```

Default jobs: watched-wallet collection daily at **09:00 local Windows time**,
subnet collection every four hours. Opt in to other jobs explicitly:

```powershell
.\tools\schedule_local.ps1 install -Root 'D:\Private Data\Tao' -Jobs collect,subnets,census,watch -DailyAt '09:30' -Replace
```

Existing task definitions require `-Replace`. Trailing directory separators
are normalized when identifying tasks. If an earlier version installed tasks
using a root ending with a separator, remove those exact old names in Task
Scheduler before reinstalling; the normalized name may differ.
Names identify the checkout,
private root and current user. Status/remove must use the same `-Root`.
Recurring five-minute watch tasks run `--once`. Different collector jobs wait
up to six hours for the shared writer lock; each task ignores overlapping
instances. A failure remains visible in `LastTaskResult` and local status.

Tasks use the current user's **logged-on interactive session**, limited
privileges, no stored Windows password and no administrator requirement. Keep
the PC on, awake and signed in. A lock screen is not the same as a working,
visible browser desktop, so test your machine's behavior before relying on it.
The scheduler keeps Windows' default AC-power policy: jobs do not start on
battery and may be stopped when switching to battery. Its 24-hour execution
ceiling includes time spent waiting for another job. Forced termination is not
a graceful cancellation. SQLite transactions and per-file atomic publication
limit damage, but the latest report/status can lag saved database progress.
After a missed trigger, Task Scheduler can start the job when available; there
are no guarantees while asleep, logged off or powered down. Check status after
reboot or resume. The script does not change execution policy. If Windows blocks
the script, inspect it and follow your organization's approved execution policy.
Native Windows Scheduler/Chrome execution must be verified on the destination PC;
Linux offline tests cannot establish that it works there.

## Backups and recovery

Before each collection job, SQLite's online backup API copies existing databases,
including committed WAL data, into a new checksummed backup generation. Do not
copy a live `.sqlite` file alone by hand. Latest reports are individually
validated and atomically replaced; a failed/invalid new report does not truncate
the previous report. Partial but readable reports are retained and marked partial.
Raw run logs, partial outputs and failed diagnostics remain under `runs`.
Treat logs, wallet preferences and movement history as private.

```bat
run_local.cmd backup
run_local.cmd restore "D:\Private Data\Tao\backups\GENERATION" --to "D:\Recovered Tao"
```

Restore verifies hashes and database integrity before publishing a new directory.
The `--to` directory must not already exist. Backups include current reports,
configuration, local snapshots and private preferences/activity, but not
credentials, browser profiles, run logs or original imported ZIPs. Preserve
`imports` separately if you need the original archives. Stop the watcher and
editor for a fully coordinated transfer: online SQLite backups are consistent
per database, not a simultaneous transaction across every file.

There is no automatic deletion/retention policy. Backups and snapshots grow.
Keep enough free disk, copy verified backups to another private disk, and review
old generations before removing any manually. A process killed abruptly may
leave status saying `running`; the OS releases the SQLite lease and the next
run resumes saved database progress. No automatic upload or Git push occurs.

## Linux later

The core entry point is `python -m seo.local_runner`; default state on non-Windows
systems is `~/.local/share/stealth-wallet-monitor` or `TAO_LOCAL_HOME`. The Python
modules avoid Windows-only process commands. Linux browser/display dependency
setup and a systemd/cron deployment are not provided or claimed tested yet.
