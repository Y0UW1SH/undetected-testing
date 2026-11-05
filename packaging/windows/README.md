# Windows wallet monitor

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

By default files live in `%LOCALAPPDATA%\Programs\StealthWalletMonitor`; saved
reports and monitoring data live in `%LOCALAPPDATA%\StealthWalletMonitor\Data`.
Uninstall from Windows Settings or the Start menu. The uninstall preserves
your data. A custom data root can be set with `STEALTH_WALLET_DATA` before
opening the launcher. The command-line entry point is `app\wallet-monitor.cmd`
inside the installation, with `--help` for commands. The setup is unsigned.

The bundled Python also supports `-m seo.tao_network_view` for making a new
explorer from an existing census SQLite file, report JSONs or transfer window.
The optional `-m seo.tao_chain_census` and `-m seo.tao_chain_window` commands
have `--help` for running bounded local chain scans explicitly. Run these
from your data directory. Keep provider credentials on the local machine.

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
SHA-256 file are written to `dist/windows`. The smoke test installs into an
isolated temporary directory using `/S /NOINTEGRATION /D=<path>`, verifies the
bundled viewer/dependencies, round-trips two offline snapshots, starts and
stops an idle test process, opens and closes the native launcher, checks the
transfer explorer with offline data, and uninstalls. Use `--backup` and
`--explorer` with the smoke test to verify an actual migration kit. It runs
offline with Start menu and uninstall registration disabled.

The payload contains the explicitly listed viewer, watcher and migration files,
bundled libraries and their licenses. It is built separately from the optional
report ZIP. No credentials, local activity, personal browser profile or
application data are inputs to the build.
