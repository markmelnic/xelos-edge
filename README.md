# xelos-edge

Daemon that runs agents on a paired local machine using Claude Code.
Connects to a Xelos cloud API over WebSocket; mirrors the org file tree
under `~/xelos/`; spawns one Claude Code process per agent run rooted
in that agent's directory.

## Install (dev)

```bash
cd xelos-edge
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

## Pair a device

In the Xelos UI, click **Devices → Pair Device**. Copy the generated code,
then on the target machine:

```bash
xelos pair <CODE> --api https://api.example.com
```

Credentials land at `~/.xelos/credentials` (chmod 600).

## Run the daemon

```bash
xelos serve
```

Foreground for now. Production launchd / systemd integration ships in
a later phase.

## Status

- **P0 (current)** — pair flow, persistent WS heartbeat, capability report.
- P1 — bidirectional file mirror with `~/xelos/{org}/...` layout.
- P2 — agentic run dispatch, Claude Code subprocess management.
- P3 — `xelos-mcp` bridging Xelos cloud tools into the local CC session.
