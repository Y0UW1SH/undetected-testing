# Windows local runner implementation plan

Goal: replace all hosted workflows with explicitly started local jobs, retaining
collectors, test source, historical data and migration tools. Windows 10/11 x64
and Python 3.13 are the first supported deployment; core Python stays portable.

Architecture: a CLI orchestrates existing modules in a private data directory.
SQLite leases prevent competing collectors. Before writing existing databases,
use SQLite's online backup API. Child jobs write reports to unique staging
folders, then validated files replace latest reports atomically. Keep immutable
run snapshots and backups. Windows scripts create a local virtual environment
and opt-in Task Scheduler entries under the logged-in user's account.

- [x] Test and implement private state paths, lock release, WAL-safe streamed
  backups, verified restore to empty destinations and artifact import.
- [x] Test and implement bounded collection, census, subnet, SEO/research jobs;
  preserve last-good reports on failure, expose honest status and local logs.
- [x] Update local watcher launchers and tolerate transient report updates.
- [x] Add Windows setup and explicit scheduling commands with no credentials,
  elevation, startup side effects or changes to system execution policy.
- [x] Document migration, legacy installer limitations, pause/sleep/reboot,
  local manual checks and optional jobs. Remove every workflow YAML only after
  replacements are implemented, keep source tests and historical data.
- [x] Run offline tests, lint, import/recovery integration and a fresh review.

Review focus: concurrent runs; interrupted/partial reports; large databases and
WAL contents; Windows quoting and logged-off sessions; secrets/private labels
never appearing in Git or backup manifests. No live collection or scheduling
is started during implementation; native Windows verification is a deployment
check, not something a Linux test run can establish.

Verification: offline tests and lint run locally; artifact import, backup,
restore and render exercised with the recovered original held untouched.
Native Windows scheduling/Chrome remains an explicit deployment verification.
