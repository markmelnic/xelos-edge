"""xelos CLI — pair / serve / status / doctor / logout."""

from __future__ import annotations

import asyncio
import json
import logging
import sys

import click

from . import __version__
from .api import ApiError, pair
from .capabilities import detect as detect_capabilities
from .config import Credentials, credentials_path
from .daemon import Daemon, DaemonOptions
from .fingerprint import fingerprint as host_fingerprint


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


@click.group()
@click.version_option(__version__, prog_name="xelos")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Xelos Edge daemon — pair this machine and run agents locally."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)


@main.command("pair")
@click.argument("code")
@click.option(
    "--api",
    "api_base",
    required=True,
    help="Cloud API base URL, e.g. https://api.xelos.example.com",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing credentials without prompting.",
)
def pair_cmd(code: str, api_base: str, force: bool) -> None:
    """Redeem a pair code and write credentials."""
    existing = Credentials.load()
    if existing is not None and not force:
        click.echo(
            f"Already paired with device {existing.device_id} on {existing.api_base}.\n"
            f"Re-run with --force to replace.",
            err=True,
        )
        sys.exit(2)

    fp = host_fingerprint()
    caps = detect_capabilities()
    click.echo(f"Pairing… (fingerprint={fp[:12]}…, claude_code={caps.get('claude_code_version') or 'missing'})")

    try:
        result = asyncio.run(
            pair(
                api_base=api_base,
                code=code.strip().upper(),
                fingerprint=fp,
                capabilities=caps,
            )
        )
    except ApiError as exc:
        msg = _format_api_error(exc)
        click.echo(f"Pair failed: {msg}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Pair failed: {exc}", err=True)
        sys.exit(1)

    creds = Credentials(
        api_base=api_base.rstrip("/"),
        websocket_url=result["websocket_url"],
        device_id=result["device_id"],
        organization_id=result["organization_id"],
        token=result["token"],
    )
    creds.save()
    click.echo(f"Paired. Credentials at {credentials_path()}")
    click.echo(f"  device_id       = {creds.device_id}")
    click.echo(f"  organization_id = {creds.organization_id}")
    click.echo(f"  websocket_url   = {creds.websocket_url}")
    click.echo("\nRun `xelos serve` to start the daemon.")


@main.command("serve")
@click.option(
    "--log-frames",
    is_flag=True,
    help="Log every WS frame sent/received (very chatty).",
)
def serve_cmd(log_frames: bool) -> None:
    """Run the WebSocket daemon in the foreground."""
    creds = Credentials.load()
    if creds is None:
        click.echo(
            "No credentials. Run `xelos pair <code> --api <url>` first.",
            err=True,
        )
        sys.exit(2)

    daemon = Daemon(creds, options=DaemonOptions(log_frames=log_frames))
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass


@main.command("status")
def status_cmd() -> None:
    """Print local credentials + capability report."""
    creds = Credentials.load()
    caps = detect_capabilities()
    if creds is None:
        click.echo("Not paired.")
    else:
        click.echo(f"Paired with device {creds.device_id}")
        click.echo(f"  organization_id = {creds.organization_id}")
        click.echo(f"  api_base        = {creds.api_base}")
        click.echo(f"  websocket_url   = {creds.websocket_url}")
    click.echo("Capabilities:")
    click.echo(json.dumps(caps, indent=2))


@main.command("doctor")
def doctor_cmd() -> None:
    """Sanity-check the local environment for running agents."""
    caps = detect_capabilities()
    issues: list[str] = []
    if not caps.get("claude_code_version"):
        issues.append(
            "  ✘ `claude` CLI not found on PATH — install Claude Code."
        )
    else:
        click.echo(f"  ✓ claude {caps['claude_code_version']}")
    if not caps.get("has_node"):
        issues.append("  ✘ `node` not found — Claude Code requires Node.js.")
    else:
        click.echo("  ✓ node present")
    click.echo(f"  ✓ python {caps['has_python']}")
    if issues:
        click.echo("\nProblems:", err=True)
        for line in issues:
            click.echo(line, err=True)
        sys.exit(1)
    click.echo("\nAll checks passed.")


@main.command("logout")
def logout_cmd() -> None:
    """Wipe local credentials. Server-side device is NOT revoked."""
    Credentials.clear()
    click.echo("Credentials cleared.")


def _format_api_error(exc: ApiError) -> str:
    payload = exc.payload
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return f"{exc.status} {detail}"
        if isinstance(detail, list) and detail:
            return f"{exc.status} {detail[0].get('msg', detail[0])}"
    return f"{exc.status} {exc.message}"


if __name__ == "__main__":
    main()
