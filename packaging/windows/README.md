# Windows wallet monitor

> This legacy installer bundles the viewer, not the new local collector.
> GitHub workflow downloads described below only apply to retained historical
> artifacts. For ongoing collection use [the Windows source setup](../../docs/local-windows.md).

Run `StealthWalletMonitor-Setup-x64.exe` on a 64-bit Windows 10 or 11 PC.
Setup installs the application for the current user. The Start menu entry
**Stealth Wallet Monitor** opens the desktop launcher. The setup bundles its
Python runtime, GitHub CLI, and local watcher dependencies.

1. To transfer saved reports, choose **Export backup** on the old PC, move the
   ZIP to the new PC, and choose **Import backup** there. An existing report ZIP
   can be imported directly. **Open report** works offline and links earlier
   scans through **Saved snapshots**.
   **Transfer explorer** opens the address-to-address evidence from all
   imported scans. If you brought a standalone `network.html` from the old
   PC, choose **Import explorer**, select that file, then choose **Transfer
   explorer** to open that saved view instead.
2. **Download latest** reads the existing GitHub Actions artifact. If GitHub
   requires authentication, use **GitHub login** to authorize the bundled CLI
   with an account that can read the repository. The downloader never starts
   a workflow.
3. **Start local monitor** opens a separate console. Supply the local provider
   key there when prompted. **Monitor status** and **Stop local monitor** operate
   on that monitor. Setup does not start it. To move its private activity and
   checkpoint, stop it, tick **Include private local activity and checkpoint**
   on both export and import, and transport the ZIP privately.
4. Choose **Edit watchlist** to add or mute coldkeys and save private labels
   and notes. The editor opens in your browser on this PC. Clicking it again
   reopens the editor for this data folder. If port 8749 belongs to an editor
   for a different data folder, close that editor first. The monitor reads
   your choices from the installed data folder on each poll.
   The **Groups** tab lets you review transfer connections and choose members
   to watch. A transfer does not prove shared ownership. Subnet context is
   assessed separately from historical profit, with explicit missing/stale data.

### Move private watch preferences

The report backup and its optional activity feed are separate from your
watch choices. To transfer your choices to a fresh installation, stop the
monitor and run `app\wallet-monitor.cmd stop-editor` in the old installation.
For a source checkout, close the editor console. Privately copy only
`try_tao_out\local_watchlist.sqlite` from its data folder. On the new PC,
stop the monitor and editor, create `try_tao_out` inside the new data folder,
and place the file there. If that file already exists on the new PC, keep it
and enter the desired choices in **Edit watchlist**. The SQLite file contains
private wallet selections, labels and notes. Configure provider and GitHub
credentials separately on each PC.

By default files live in `%LOCALAPPDATA%\Programs\StealthWalletMonitor`; saved
reports and monitoring data live in `%LOCALAPPDATA%\StealthWalletMonitor\Data`.
Uninstall from Windows Settings or the Start menu. The uninstall preserves
your data. A custom data root can be set with `STEALTH_WALLET_DATA` before
opening the launcher. The command-line entry point is `app\wallet-monitor.cmd`
inside the installation, with `--help` for commands. The setup is unsigned.

The installed CLI also supports `edit-watchlist` and `stop-editor`.
The bundled Python supports `-m seo.tao_network_view` for making a new
explorer from an existing census SQLite file, report JSONs or transfer window.
The optional `-m seo.tao_chain_census` and `-m seo.tao_chain_window` commands
have `--help` for running bounded local chain scans explicitly. Run these
from your data directory. Keep provider credentials on the local machine.

Native holder discovery, research priority and subnet context are also bundled.
From your data directory, invoke the installed `app\runtime\python.exe` by its
full path with `-m seo.tao_subnet_context --out github_out/subnet-context.json`
to refresh lifecycle and pool-reserve evidence, using your local `dwellir.txt`.
On subsequent refreshes, add
`--previous github_out/subnet-context.json` to retain recent generation history.
The context file contains public chain evidence; it contains no provider key.
The editor reloads it when it changes. It is a dated snapshot, not a live sell
quote, and this command does not start a GitHub workflow or recurring job.

## Build and verify

From a Windows x64 checkout, with Python 3.11+ and pip installed:

```powershell
python packaging/windows/build.py
python packaging/windows/smoke.py dist/windows/StealthWalletMonitor-Setup-x64.exe
Get-FileHash -Algorithm SHA256 dist/windows/StealthWalletMonitor-Setup-x64.exe
```

The builder downloads checksum-pinned CPython, GitHub CLI and the NSIS compiler
from the URLs in `sources.json`. It downloads only wheel hashes and versions in
`watch-wheels.txt` and uses the Windows .NET Framework C# compiler for the
launcher. Build inputs are cached under the ignored `try_tao_out/packager`.
Use `--offline` to rebuild entirely from that verified cache. Setup and its
SHA-256 file are written to `dist/windows`. Add
`--kit dist/windows/StealthWalletMonitor-Kit.zip` for a ZIP containing the
installer, its SHA-256 file and this guide. The smoke test installs into an
isolated temporary directory using `/S /NOINTEGRATION /D=<path>`, verifies the
bundled viewer/dependencies, round-trips two offline snapshots, starts and
stops an idle test process, opens and closes the native launcher, checks the
transfer explorer with offline data, reads and writes an isolated watchlist
over loopback, and uninstalls. Use `--backup` and
`--explorer` with the smoke test to verify an actual migration kit. It runs
offline with Start menu and uninstall registration disabled.

The payload contains the explicitly listed viewer, watcher and migration files,
bundled libraries and their licenses. It is built separately from the optional
report ZIP. No credentials, local activity, personal browser profile or
application data are inputs to the build.
