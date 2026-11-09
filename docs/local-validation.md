# Local validation and remaining deployment work

Validated on 2026-11-09. This is the Windows-first source project in
[PR #2](https://github.com/Y0UW1SH/undetected-testing/pull/2), branch
`feat/windows-local-runner`. It is not an installed service or a new EXE.
No scheduled jobs were installed, no recovered databases were merged, and no
private observations or credentials are part of the code change.

## Executed on the available desktop

- Debian GNU/Linux 13 x86_64, Python 3.12.14, SeleniumBase 4.57.1.
- Fresh virtual environment; editable project and `requirements-local.txt`
  installed successfully. `pip check` passed.
- `local_runner init` and `check` passed required checks. No Xvfb/display was
  available; the doctor reported that as a warning.
- Actual `api-collect --netuid 1 --pages 2 --page-size 3` with an explicitly
  supplied public sample account succeeded. Seven requests produced price,
  subnet detail, six subnet-trade observations, an account balance and six
  account-transaction observations. Counts are not unique-event guarantees.
- Backed up that disposable state and restored to an absent destination.
  Every one of the 16 evidence-file checksums matched after restore; the current
  JSON report was byte-identical. No pre-existing wallet ledger was touched.
- The generated HTML was checked by tests, including escaping of provider text.
  Browser rendering was not observed: shell Chromium fails at startup with a
  local-socket permission error. The shared browser also does not support opening
  these local file URLs. No browser security settings were changed.
- Independent review found and fixed mixed-generation publication, non-finite
  JSON numeric overflow, and truncated-response error handling. The current JSON
  is now the single atomic publication point, identifying immutable evidence.

## Test results

- Offline suite with the available PowerShell parser: **655 passed, 1 skipped**.
  The skip is native `cmd.exe` execution, not a passing Windows test.
- `flake8` reported zero findings; `git diff --check` passed.
- Bare `pytest` also includes the existing root
  `test_user_agent.py::OfflineTests::test_get_user_agent`. That browser test
  fails at Chrome session startup in this desktop environment. Writable caches
  and explicit headless Chromium did not resolve the local-socket restriction.
  Do not interpret the offline suite as a successful browser run.

## Windows-specific evidence

- Full dependencies resolved for Windows x64 / Python 3.13, and all 87 selected
  binary wheels were downloadable. This verifies packaging availability, not an
  actual Windows install.
- Microsoft PowerShell 7.5.4 on Linux parsed both PowerShell scripts. Tests ran
  their syntax-only path, malformed-script rejection, non-Windows guard and the
  actual scheduler argument-quoting function. They did not register tasks.
- `tools/test_windows_local.ps1` is the offline native smoke test to run after
  `setup_local.cmd`. It isolates its state, runs the tests and checks CMD
  init/status/backup/restore. It does not start Chrome, collect live data or
  install scheduled tasks.
- Native Windows CMD, Python installation, Chrome and Task Scheduler execution
  remain unverified because no Windows host was available. The native CMD test
  is explicitly skipped off Windows.

## Source findings and implementation choice

TAO.com's public price, subnet, canonical trade, account balance and transaction
endpoints responded without an API key. An independent probe checked one account
balance at a fixed block and one stake-removal event against archive chain data.
That validates those samples, not the entire service or historical coverage.

Account transaction and transaction-detail responses omitted a leg of a multi-leg
extrinsic present in the subnet trade feed and chain data. Account offsets are
capped at 10,000, and an exited position's holdings history can be empty. The new
command therefore stores provider-specific observations separately; it does not
rebuild cost basis, infer profitability, or silently replace the legacy ledger.

Hosted OpenTaoAPI served market/subnet data, but trader rankings and stake-event
routes returned 404. A subnet history request returned only the oldest 500 rows
until a larger limit was supplied. Sample wallet history also lacked recent
observations. No automatic fallback or profit ranking is implemented from it.

## Remaining work before unattended production

1. Run the offline Windows smoke test on the target PC.
2. Run a bounded public-data API observation there and open its printed HTML.
3. Preserve and compare the two recovery archives before explicitly choosing a
   working state. Neither archive is automatically selected or merged.
4. Validate the existing Taostats browser/RPC collection and watcher on that PC.
5. Before any future lossless history adapter, define canonical event identity,
   multi-leg/transfer reconciliation, gaps and pagination checkpoints. Keep
   coverage unknown until these have evidence-backed tests.
6. Install schedules only deliberately after those checks, with the intended
   private root. This draft does not merge itself or alter repository visibility.
