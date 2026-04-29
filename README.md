# xelos-edge

Daemon that runs agents on a paired local machine using Claude Code.
Connects to a Xelos cloud API over WebSocket; mirrors the org file
tree under `~/xelos/`; spawns one Claude Code process per agent run
rooted in that agent's directory.

Supported on **macOS**, **Linux**, and **Windows**.

---

## Install (one-liner)

### macOS / Linux

```sh
curl -fsSL https://raw.githubusercontent.com/markmelnic/xelos-edge/main/install.sh | sh
```

### Windows (PowerShell)

```powershell
iwr -useb https://raw.githubusercontent.com/markmelnic/xelos-edge/main/install.ps1 | iex
```

The installer:

- Verifies you have **Python 3.11+** (offers to install via `winget` on
  Windows; tells you the right `brew` / `apt` / `dnf` / `pacman` command
  on macOS / Linux).
- Creates an isolated runtime in `~/.xelos/runtime` so it doesn't touch
  any other Python on your machine.
- Drops a `xelos` launcher into `~/.local/bin` (POSIX) or
  `%USERPROFILE%\.xelos\bin` (Windows) and adds that to your PATH.

Re-running upgrades to the latest version. To uninstall, delete the
`~/.xelos` directory and remove the launcher.

---

## Pair a device

In the Xelos UI, open **Devices → Pair Device**. Copy the pair code,
then on the target machine:

```sh
xelos pair <CODE>
```

Defaults to the production API at `https://xelos-api-production.up.railway.app`.
Override with `--api <url>` for staging / self-hosted deployments.

The daemon stores a long-lived token at `~/.xelos/credentials`
(chmod 600 on POSIX; ACL-protected user-scope on Windows).

## Run the daemon

```sh
xelos serve
```

Foreground for now. Production launchd / systemd / Windows Service
integration ships in a later phase.

## Other commands

```sh
xelos status     # show pairing state + capability report
xelos doctor     # sanity-check tooling (claude, node, python)
xelos logout     # wipe local credentials (server-side device is NOT revoked)
```

---

## Install from source (for contributors)

```sh
git clone https://github.com/markmelnic/xelos-edge.git
cd xelos-edge
python3 -m venv .venv
. .venv/bin/activate          # or `.venv\Scripts\Activate.ps1` on Windows
pip install -e ".[dev]"
xelos --help
```

## Roadmap

- **P0 (current)** — pair flow, persistent WS heartbeat, capability report.
- P1 — bidirectional file mirror with `~/xelos/{org}/...` layout.
- P2 — agentic run dispatch, Claude Code subprocess management.
- P3 — `xelos-mcp` bridging Xelos cloud tools into the local CC session.
- P4 — file sync conflict resolution.
- P5 — device pinning + library install to local FS.
