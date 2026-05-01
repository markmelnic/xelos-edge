"""xelos CLI — pair / serve / status / doctor / logout."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import click

from . import __version__
from .api import ApiError, pair
from .capabilities import detect as detect_capabilities
from .config import Credentials, credentials_path
from .daemon import Daemon, DaemonOptions
from .fingerprint import fingerprint as host_fingerprint
from .updates import maybe_warn_outdated, refresh_cache as refresh_update_cache


def _setup_logging(verbose: bool) -> None:
    """Console + persistent file logging.

    Always mirrors lines to `~/.xelos/serve.log` since the TUI grabs the
    terminal during serve and makes scrollback unselectable.
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s :: %(message)s"
    )
    root = logging.getLogger()
    # Drop pre-existing handlers so re-entry (e.g. post-update execv) doesn't double-log.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    try:
        from .config import _xelos_home

        log_path = _xelos_home() / "serve.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # 5MB × 5 = 25MB on-disk ceiling.
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            str(log_path),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception:  # pragma: no cover
        pass


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="xelos")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Xelos Edge — pair this machine, run agents locally.

    Run with no subcommand to launch the interactive menu (pair,
    serve, status, doctor, update, logout). Subcommands stay
    available for scripts and headless boxes.
    """
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)
    if ctx.invoked_subcommand != "update":
        maybe_warn_outdated(click.echo)
    if ctx.invoked_subcommand is None:
        if not sys.stdout.isatty():
            click.echo(ctx.get_help())
            return
        _run_launcher_loop()


_PROD_API_BASE = "https://xelos-api-production.up.railway.app"
_LOCAL_API_BASE = "http://localhost:8000"


def _default_api_base() -> str:
    """API base URL: $XELOS_API_BASE → localhost when $XELOS_DEV=1 → prod."""
    explicit = (os.environ.get("XELOS_API_BASE") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    if os.environ.get("XELOS_DEV") in ("1", "true", "yes"):
        return _LOCAL_API_BASE
    return _PROD_API_BASE


@main.command("pair")
@click.argument("code")
@click.option(
    "--api",
    "api_base",
    default=None,
    show_default=False,
    help=(
        "Cloud API base URL. Defaults to $XELOS_API_BASE, then "
        "localhost:8000 when $XELOS_DEV=1, then production."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing credentials without prompting.",
)
def pair_cmd(code: str, api_base: str | None, force: bool) -> None:
    """Redeem a pair code and write credentials."""
    if Credentials.load() is not None and not force:
        click.echo(
            "Already paired. Re-run with --force to replace.", err=True
        )
        sys.exit(2)
    resolved_api = api_base or _default_api_base()
    _do_pair_interactive(code=code, api_base=resolved_api, force=force)


@main.command("logs")
@click.option("-f", "--follow", is_flag=True, help="tail -f the log file")
@click.option("-n", "--lines", default=200, show_default=True)
def logs_cmd(follow: bool, lines: int) -> None:
    """Print the rolling daemon log (`~/.xelos/serve.log`)."""
    from .config import _xelos_home

    path = _xelos_home() / "serve.log"
    if not path.exists():
        click.echo(
            f"No log file yet at {path}. Run `xelos serve` first.",
            err=True,
        )
        sys.exit(2)
    if follow:
        # Defer to system `tail -F` for rotation-aware follow.
        tail = shutil.which("tail")
        if tail:
            os.execv(tail, [tail, "-n", str(lines), "-F", str(path)])
        click.echo("`tail` not found; printing last lines and exiting.", err=True)
    with path.open("r", encoding="utf-8", errors="replace") as f:
        content = f.read().splitlines()[-lines:]
        click.echo("\n".join(content))


@main.command("pair-local", hidden=True)
@click.argument("code")
@click.option(
    "--port", default=8000, show_default=True, help="Local backend port."
)
@click.option("--force", is_flag=True)
def pair_local_cmd(code: str, port: int, force: bool) -> None:
    """Hidden dev convenience — pair against http://localhost:<port>."""
    if Credentials.load() is not None and not force:
        click.echo(
            "Already paired. Re-run with --force to replace.", err=True
        )
        sys.exit(2)
    _do_pair_interactive(
        code=code, api_base=f"http://localhost:{port}", force=force
    )


@main.command("serve")
@click.option(
    "--log-frames",
    is_flag=True,
    help="Log every WS frame sent/received (very chatty).",
)
@click.option(
    "--no-tui",
    is_flag=True,
    help="Disable the interactive TUI (forces line-oriented logging).",
)
def serve_cmd(log_frames: bool, no_tui: bool) -> None:
    """Run the WebSocket daemon in the foreground."""
    creds = Credentials.load()
    if creds is None:
        click.echo(
            "No credentials. Run `xelos pair <code>` first.",
            err=True,
        )
        sys.exit(2)

    use_tui = (not no_tui) and sys.stdout.isatty()
    if use_tui:
        from .tui import run_tui

        run_tui(credentials=creds, log_frames=log_frames)
        return

    daemon = Daemon(creds, options=DaemonOptions(log_frames=log_frames))
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass


@main.command("tui")
@click.option(
    "--log-frames",
    is_flag=True,
    help="Log every WS frame sent/received (very chatty).",
)
def tui_cmd(log_frames: bool) -> None:
    """Always launch the interactive dashboard (no auto-fallback)."""
    creds = Credentials.load()
    if creds is None:
        click.echo(
            "No credentials. Run `xelos pair <code>` first.",
            err=True,
        )
        sys.exit(2)
    from .tui import run_tui

    run_tui(credentials=creds, log_frames=log_frames)


@main.command("status")
def status_cmd() -> None:
    """Print local credentials + capability report."""
    creds = Credentials.load()
    caps = detect_capabilities()
    if creds is None:
        click.echo("Not paired.")
    else:
        click.echo(f"Paired with device {creds.device_id}")
        click.echo(f"  user_id      = {creds.user_id}")
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


@main.command("update")
@click.option(
    "--ref",
    default=None,
    help="Git ref (branch/tag/sha) to install. Defaults to main, or "
    "$XELOS_PACKAGE_SPEC if set.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the upgrade command without running it.",
)
def update_cmd(ref: str | None, dry_run: bool) -> None:
    """Upgrade xelos-edge in place to the latest release."""
    install = _detect_install()
    click.echo(f"Current xelos {__version__} ({install['kind']} @ {install['location']})")

    if install["kind"] == "editable":
        click.echo(
            "Editable install detected. Update with:\n"
            f"  git -C {install['location']} pull\n"
            f"  {sys.executable} -m pip install -e \"{install['location']}[dev]\"",
            err=True,
        )
        sys.exit(2)

    if os.name == "nt":
        click.echo(
            "Windows in-place upgrade is unsafe (xelos.exe is locked while running).\n"
            "Re-run the installer instead:\n"
            "  iwr -useb https://raw.githubusercontent.com/markmelnic/xelos-edge/main/install.ps1 | iex",
            err=True,
        )
        sys.exit(2)

    spec = os.environ.get("XELOS_PACKAGE_SPEC")
    if ref:
        spec = f"git+https://github.com/markmelnic/xelos-edge.git@{ref}"
    if not spec:
        spec = "git+https://github.com/markmelnic/xelos-edge.git@main"

    if spec.startswith("git+") and shutil.which("git") is None:
        click.echo(
            "git not found on PATH. Either install git, or set $XELOS_PACKAGE_SPEC\n"
            "to a tarball URL, e.g.:\n"
            "  export XELOS_PACKAGE_SPEC="
            "https://github.com/markmelnic/xelos-edge/archive/refs/heads/main.tar.gz",
            err=True,
        )
        sys.exit(2)

    purelib = sysconfig.get_path("purelib")
    if purelib and not os.access(purelib, os.W_OK):
        click.echo(
            f"! site-packages is not writable: {purelib}\n"
            "  Likely a system-wide install. Try one of:\n"
            "    - re-run the installer (recommended): "
            "curl -fsSL https://raw.githubusercontent.com/markmelnic/xelos-edge/main/install.sh | sh\n"
            "    - sudo xelos update\n"
            "    - pip install --user --upgrade ...  (may not match your launcher)",
            err=True,
        )

    # `--force-reinstall --no-deps` because we ship from `@main`: the pyproject
    # version doesn't bump per SHA, so plain `--upgrade` is a no-op.
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--force-reinstall",
        "--no-deps",
        "--no-cache-dir",
        spec,
    ]
    click.echo(f"$ {' '.join(cmd)}")
    if dry_run:
        return

    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as exc:
        click.echo(f"Upgrade failed (exit {exc.returncode}).", err=True)
        sys.exit(exc.returncode)
    refresh_update_cache()
    click.echo("Upgrade complete. Re-run `xelos --version` to confirm.")


def _detect_install() -> dict[str, str]:
    """Best-effort classify how the package was installed."""
    pkg_root = Path(__file__).resolve().parent
    repo_root = pkg_root.parent
    if (repo_root / "pyproject.toml").exists() and (repo_root / ".git").exists():
        return {"kind": "editable", "location": str(repo_root)}
    try:
        from importlib.metadata import distribution

        dist = distribution("xelos-edge")
        direct_url = dist.read_text("direct_url.json")
        if direct_url and '"editable": true' in direct_url:
            return {"kind": "editable", "location": str(repo_root)}
    except Exception:
        pass
    return {"kind": "installed", "location": str(pkg_root)}


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


def _run_launcher_loop() -> None:
    """Drive the interactive menu until the user quits."""
    from .launcher import run_launcher

    while True:
        result = run_launcher()
        if result is None:
            return

        # Pair flow: launcher returns a dict with the form values.
        if isinstance(result, dict) and result.get("action") == "pair":
            _do_pair_interactive(
                code=result["code"], api_base=result["api"], force=True
            )
            click.pause("\nPress any key to return to the menu… ")
            continue

        if result == "serve":
            creds = Credentials.load()
            if creds is None:
                click.echo("Not paired yet — run Pair first.", err=True)
                click.pause()
                continue
            from .tui import run_tui

            run_tui(credentials=creds)
            continue

        if result == "logout":
            Credentials.clear()
            click.echo("Credentials cleared.")
            click.pause("\nPress any key to return to the menu… ")
            continue

        if result == "update":
            # Defer to update_cmd via subprocess to avoid tangling Click context with self-upgrade.
            try:
                subprocess.check_call([sys.executable, "-m", "runtime.cli", "update"])
            except subprocess.CalledProcessError as exc:
                click.echo(f"Update failed (exit {exc.returncode}).", err=True)
                click.pause("\nPress any key to return to the menu… ")
                continue
            # Re-exec so the user lands on the freshly installed code.
            click.echo("\nUpdate applied. Restarting xelos with the new build…")
            os.execv(sys.executable, [sys.executable, "-m", "runtime.cli"])
            return  # unreachable


def _do_pair_interactive(*, code: str, api_base: str, force: bool) -> None:
    """Pair flow shared between the `pair` subcommand + the launcher."""
    existing = Credentials.load()
    if existing is not None and not force:
        click.echo(
            f"Already paired with device {existing.device_id} on {existing.api_base}.",
            err=True,
        )
        return

    fp = host_fingerprint()
    caps = detect_capabilities()
    click.echo(
        f"Pairing… (fingerprint={fp[:12]}…, "
        f"claude_code={caps.get('claude_code_version') or 'missing'})"
    )
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
        click.echo(f"Pair failed: {_format_api_error(exc)}", err=True)
        return
    except Exception as exc:
        click.echo(f"Pair failed: {exc}", err=True)
        return

    # Derive the WS URL from the api base the operator paired against —
    # the server-provided `websocket_url` may point at the wrong host
    # when a local dev backend has `API_BASE_URL` set to production.
    api_base_clean = api_base.rstrip("/")
    if api_base_clean.startswith("https://"):
        ws_scheme_url = "wss://" + api_base_clean[len("https://"):]
    elif api_base_clean.startswith("http://"):
        ws_scheme_url = "ws://" + api_base_clean[len("http://"):]
    else:
        ws_scheme_url = result["websocket_url"].rsplit("/devices/", 1)[0]
    derived_ws = f"{ws_scheme_url}/devices/{result['device_id']}/ws"

    creds = Credentials(
        api_base=api_base_clean,
        websocket_url=derived_ws,
        device_id=result["device_id"],
        user_id=result["user_id"],
        token=result["token"],
    )
    creds.save()
    click.echo(f"Paired. Credentials at {credentials_path()}")


if __name__ == "__main__":
    main()
