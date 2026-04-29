"""Interactive launcher — the default screen `xelos` shows.

Runs before any subcommand. The user picks an action (Pair, Serve,
Status, Doctor, Update, Logout, Quit); the launcher exits with the
chosen action name and the CLI dispatches the corresponding flow.

Cross-platform: Textual works on macOS, Linux, Windows. No native
toolkit assumptions; everything is rendered in the terminal.
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, Static

from . import __version__
from .capabilities import detect as detect_capabilities
from .config import Credentials, credentials_path


PALETTE_CSS = """
Screen {
    background: #06080c;
    color: #e6edf3;
    align: center middle;
}

#brand-card {
    width: 64;
    border: solid #28344a;
    background: #0d131c;
    padding: 1 3;
}

#brand-title {
    color: #beafe7;
    text-style: bold;
    content-align: center middle;
    width: 100%;
}

#brand-subtitle {
    color: #7d8a9a;
    content-align: center middle;
    width: 100%;
    margin-bottom: 1;
}

.pair-state {
    color: #4ade80;
    content-align: center middle;
    width: 100%;
    margin-bottom: 1;
}

.pair-state-missing {
    color: #fbbf24;
    content-align: center middle;
    width: 100%;
    margin-bottom: 1;
}

#menu Button {
    width: 100%;
    margin: 0 0 1 0;
    background: #121925;
    color: #e6edf3;
    border: solid #161e2c;
}

#menu Button:hover {
    background: #1a2333;
    color: #beafe7;
    border: solid #28344a;
}

#menu Button.-active {
    background: #beafe7 10%;
    color: #beafe7;
}

.danger Button {
    color: #f87171;
}

#status-pane {
    width: 80;
    border: solid #28344a;
    background: #0d131c;
    padding: 1 2;
}

.kv-key {
    color: #7d8a9a;
    width: 22;
}

.kv-value {
    color: #e6edf3;
}

.section-title {
    color: #beafe7;
    text-style: bold;
    margin: 0 0 1 0;
    border-bottom: solid #28344a;
}

#pair-form {
    width: 64;
    border: solid #28344a;
    background: #0d131c;
    padding: 1 3;
}

#pair-form Input {
    margin: 0 0 1 0;
    border: solid #28344a;
}
"""


# --- Screens --------------------------------------------------------------


class _StatusModal(ModalScreen[None]):
    """Read-only Status / Doctor screen."""

    BINDINGS = [
        Binding("escape", "dismiss", "Back"),
        Binding("q", "dismiss", "Back"),
    ]

    def __init__(self, *, title: str, body: list[tuple[str, str]]) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="status-pane"):
            yield Label(self._title, classes="section-title")
            for key, value in self._body:
                with Horizontal():
                    yield Label(key, classes="kv-key")
                    yield Label(value or "—", classes="kv-value")
            yield Label("[Esc] back", classes="kv-key")


class _PairScreen(ModalScreen[dict[str, str] | None]):
    """Form for entering a pair code (and optional API base)."""

    BINDINGS = [Binding("escape", "dismiss", "Back")]

    DEFAULT_API = "https://xelos-api-production.up.railway.app"

    def compose(self) -> ComposeResult:
        with Vertical(id="pair-form"):
            yield Label("Pair this device", classes="section-title")
            yield Label("Pair code", classes="kv-key")
            yield Input(placeholder="ABCD2345", id="pair-code")
            yield Label("API base (optional)", classes="kv-key")
            yield Input(placeholder=self.DEFAULT_API, id="pair-api")
            with Horizontal():
                yield Button("Cancel", id="pair-cancel")
                yield Button("Pair", id="pair-submit", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pair-cancel":
            self.dismiss(None)
            return
        if event.button.id == "pair-submit":
            code = self.query_one("#pair-code", Input).value.strip().upper()
            api = (
                self.query_one("#pair-api", Input).value.strip()
                or self.DEFAULT_API
            )
            if not code:
                return
            self.dismiss({"code": code, "api": api})


class _ConfirmModal(ModalScreen[bool]):
    """Yes/no prompt — used for logout + update."""

    BINDINGS = [Binding("escape", "_cancel", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="status-pane"):
            yield Label(self._message, classes="section-title")
            with Horizontal():
                yield Button("Cancel", id="confirm-cancel")
                yield Button("Confirm", id="confirm-ok", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-ok")

    def action__cancel(self) -> None:
        self.dismiss(False)


# --- Launcher app ---------------------------------------------------------


class XelosLauncher(App[str | None]):
    """Bare `xelos` entrypoint. Returns the action the user picked."""

    TITLE = "XELOS EDGE"
    CSS = PALETTE_CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._creds = Credentials.load()

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="brand-card"):
                yield Static(self._brand_title(), id="brand-title")
                yield Static(
                    f"v{__version__}    Edge daemon for the Xelos workspace OS",
                    id="brand-subtitle",
                )
                if self._creds is not None:
                    yield Static(
                        f"● paired as {str(self._creds.device_id)[:8]}…",
                        classes="pair-state",
                    )
                else:
                    yield Static(
                        "○ not paired — pick \"Pair this device\" to get started",
                        classes="pair-state-missing",
                    )
                with Vertical(id="menu"):
                    yield Button(
                        "▸ Pair this device" if self._creds is None else "▸ Re-pair this device",
                        id="btn-pair",
                    )
                    yield Button(
                        "▸ Serve (start daemon + dashboard)",
                        id="btn-serve",
                        disabled=self._creds is None,
                    )
                    yield Button("▸ Status", id="btn-status")
                    yield Button("▸ Doctor (check tooling)", id="btn-doctor")
                    yield Button("▸ Update xelos-edge", id="btn-update")
                    if self._creds is not None:
                        yield Button("▸ Logout (forget credentials)", id="btn-logout")
                    yield Button("▸ Quit", id="btn-quit")
        yield Footer()

    def _brand_title(self) -> Text:
        text = Text()
        text.append("◆ ", style="bold #beafe7")
        text.append("XELOS EDGE", style="bold #beafe7")
        return text

    # --- actions ---

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "btn-quit":
            self.exit(None)
            return
        if bid == "btn-pair":
            result = await self.push_screen_wait(_PairScreen())
            if result is not None:
                self.exit({"action": "pair", **result})
            return
        if bid == "btn-serve":
            self.exit("serve")
            return
        if bid == "btn-update":
            ok = await self.push_screen_wait(
                _ConfirmModal("Run `xelos update`? (xelos will exit and pip-upgrade.)")
            )
            if ok:
                self.exit("update")
            return
        if bid == "btn-logout":
            ok = await self.push_screen_wait(
                _ConfirmModal("Wipe local credentials? Server-side device is NOT revoked.")
            )
            if ok:
                self.exit("logout")
            return
        if bid == "btn-status":
            await self._show_status()
            return
        if bid == "btn-doctor":
            await self._show_doctor()
            return

    async def _show_status(self) -> None:
        if self._creds is None:
            body = [
                ("State", "Not paired"),
                ("Credentials", str(credentials_path())),
            ]
        else:
            body = [
                ("Device id", str(self._creds.device_id)),
                ("Workspace id", str(self._creds.workspace_id)),
                ("API base", self._creds.api_base),
                ("WebSocket", self._creds.websocket_url),
                ("Credentials", str(credentials_path())),
            ]
        await self.push_screen_wait(
            _StatusModal(title="Status", body=body)
        )

    async def _show_doctor(self) -> None:
        caps = detect_capabilities()
        body = [
            ("Claude Code", caps.get("claude_code_version") or "missing — install Claude Code"),
            ("Node", caps.get("node_version") or "missing — install Node.js 18+"),
            ("Python", caps.get("python_version") or "—"),
            ("Max concurrent runs", str(caps.get("max_concurrent_runs") or "—")),
        ]
        await self.push_screen_wait(
            _StatusModal(title="Doctor — local capability report", body=body)
        )


def run_launcher() -> Any:
    """Launch the menu. Returns the chosen action (or None on quit)."""
    return XelosLauncher().run()


__all__ = ["XelosLauncher", "run_launcher"]
