# xelos-edge

Daemon that runs agents on a paired local machine using Claude Code.
Connects to the Xelos cloud over WebSocket, mirrors every workspace
the paired user belongs to under `~/.xelos/mirror/<workspace>/`,
spawns one Claude Code process per agent run rooted in that agent's
directory.

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

- Auto-installs missing dependencies via the system package manager
  (Python 3.11+, Node 18+, Claude Code CLI). Prompts before each
  install; pass `XELOS_NONINTERACTIVE=1` to accept the defaults.
- Creates an isolated runtime in `~/.xelos/runtime` so it doesn't touch
  any other Python on your machine.
- Drops a `xelos` launcher into `~/.local/bin` (POSIX) or
  `%USERPROFILE%\.xelos\bin` (Windows) and adds it to your PATH.
- Offers to run `claude login` so the daemon can spawn agents on first
  boot. Skip with `XELOS_SKIP_CLAUDE=1` and run `claude login` later.

Re-running upgrades the runtime in place. To uninstall, delete the
`~/.xelos` directory and remove the launcher.

Env knobs:

| var | effect |
|---|---|
| `XELOS_NONINTERACTIVE=1` | accept all prompts with their defaults |
| `XELOS_SKIP_DEPS=1` | don't try to auto-install Python / Node |
| `XELOS_SKIP_CLAUDE=1` | don't install Claude Code or run `claude login` |
| `XELOS_PACKAGE_SPEC=...` | install from a different ref (default: `git+...@main`) |

---

## Quick start

Just run:

```sh
xelos
```

Bare `xelos` opens an interactive menu where you can **Pair this device**,
**Serve** (run the daemon + dashboard), check **Status** / **Doctor**, or
**Update** xelos-edge. The first time, pick "Pair this device", paste
the code from the Xelos UI (**Devices → Pair Device**), and confirm —
the menu re-renders with "Serve" enabled.

The daemon stores its long-lived token at `~/.xelos/credentials`
(chmod 600 on POSIX; ACL-protected user-scope on Windows).

### Headless / scriptable subcommands

The launcher is a thin wrapper over the existing subcommands; they
remain for systemd, launchd, Docker, CI:

```sh
xelos pair <CODE>            # one-shot pair (override API with --api)
xelos serve                  # run daemon (TUI when tty, plain logs otherwise)
xelos serve --no-tui         # force plain log streaming
xelos tui                    # always launch the dashboard
xelos status                 # show pairing state + capability report
xelos doctor                 # sanity-check tooling (claude, node, python)
xelos update                 # upgrade xelos-edge in place
xelos logout                 # wipe local credentials
```

---

## What the daemon does

- **Multi-workspace mirror.** Devices belong to a single user but span
  every workspace that user is a member of. Each workspace gets its
  own subdir under `~/.xelos/mirror/<workspace_slug>/...`.
- **Three-way file sync.** Cloud manifest ↔ local `state.db` ↔ disk;
  conflicts stash the local copy and prefer the cloud copy.
- **Run dispatch.** Cloud sends `job.start` over WebSocket; the daemon
  spawns `claude` in the agent's directory with the agent's system
  prompt, allowed tools, and an MCP bridge (`xelos-mcp`) so cloud
  tools (web search, file ops, memory, delegation) are callable.
- **Live dashboard.** `xelos` (or `xelos serve` on a tty) shows status,
  active runs, file-sync activity, and a streaming log tail in a
  Textual TUI. Switch panes with `s/r/f/l`, quit with `q`.

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
