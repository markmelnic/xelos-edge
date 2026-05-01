"""Textual TUI for `xelos serve` — Status / Runs / Files / Logs panes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from . import __version__
from .capabilities import detect as detect_capabilities
from .config import Credentials, _xelos_home
from .daemon import Daemon, DaemonOptions
from .events import EVENTS, Event


# --- Brand palette (mirrors xelos-www CSS tokens) -------------------------

PALETTE_CSS = """
Screen {
    background: #06080c;
    color: #e6edf3;
}

Header {
    background: #0d131c;
    color: #beafe7;
    height: 3;
    padding: 1 2;
    border-bottom: solid #28344a;
}

#brand {
    color: #beafe7;
    text-style: bold;
}

#meta {
    color: #7d8a9a;
}

.status-pill-live {
    background: #4ade80 10%;
    color: #4ade80;
    padding: 0 1;
}

.status-pill-down {
    background: #f87171 10%;
    color: #f87171;
    padding: 0 1;
}

.status-pill-pending {
    background: #fbbf24 10%;
    color: #fbbf24;
    padding: 0 1;
}

TabbedContent {
    background: #0d131c;
    margin: 0 1;
}

TabPane {
    padding: 1 2;
}

Tabs {
    background: #0d131c;
    color: #a3b0bf;
}

Tab {
    color: #7d8a9a;
}

Tab:hover {
    color: #d8ccf0;
}

Tab.-active {
    color: #beafe7;
    text-style: bold;
}

DataTable {
    background: #0d131c;
    border: solid #161e2c;
}

DataTable > .datatable--header {
    background: #121925;
    color: #a3b0bf;
    text-style: bold;
}

DataTable > .datatable--cursor {
    background: #1a2333;
}

#status-grid {
    grid-size: 2 4;
    grid-gutter: 1 2;
    height: auto;
}

.kv-key {
    color: #7d8a9a;
    text-style: bold;
    width: 22;
}

.kv-value {
    color: #e6edf3;
}

.section-title {
    color: #beafe7;
    text-style: bold;
    border-bottom: solid #28344a;
    padding: 0 0 1 0;
    margin: 0 0 1 0;
}

RichLog {
    background: #06080c;
    border: solid #161e2c;
    padding: 0 1;
}

Footer {
    background: #0d131c;
    color: #7d8a9a;
}
"""


# --- Helpers --------------------------------------------------------------


def _short(uid: Any, n: int = 8) -> str:
    s = str(uid or "")
    return s[:n] + "…" if len(s) > n else s


def _fmt_size(n: int | None) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


# --- Header widget --------------------------------------------------------


class HeaderBar(Static):
    """Shows brand + device id + workspace + ws state."""

    ws_state: reactive[str] = reactive("connecting")

    def __init__(self, *, device_id: str, user_id: str) -> None:
        super().__init__(id="header")
        self._device_id = device_id
        self._user_id = user_id

    def render(self) -> Text:
        ws_dot, ws_color = {
            "live": ("●", "#4ade80"),
            "connecting": ("◐", "#fbbf24"),
            "down": ("●", "#f87171"),
        }.get(self.ws_state, ("◌", "#7d8a9a"))

        text = Text()
        text.append("◆ ", style="bold #beafe7")
        text.append("XELOS EDGE", style="bold #beafe7")
        text.append(f"   v{__version__}", style="#7d8a9a")
        text.append("    device ", style="#7d8a9a")
        text.append(_short(self._device_id, 12), style="#d8ccf0")
        text.append("    user ", style="#7d8a9a")
        text.append(_short(self._user_id, 12), style="#d8ccf0")
        text.append("    WS ", style="#7d8a9a")
        text.append(f"{ws_dot} {self.ws_state}", style=f"bold {ws_color}")
        return text


# --- Status pane ----------------------------------------------------------


class StatusPane(VerticalScroll):
    """Static device + capability info plus current sync stats."""

    def __init__(self, *, creds: Credentials, mirror_root: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._creds = creds
        self._mirror_root = mirror_root
        self._reconcile_label: Label | None = None

    def _kv_text(self, key: str, value: Any) -> Text:
        text = Text()
        text.append(f"{key:<22}", style="bold #7d8a9a")
        text.append(str(value or "—"), style="#e6edf3")
        return text

    def compose(self) -> ComposeResult:
        yield Label("Device", classes="section-title")
        yield Static(self._kv_text("Device id", self._creds.device_id))
        yield Static(self._kv_text("User", self._creds.user_id))
        yield Static(self._kv_text("API base", self._creds.api_base))
        yield Static(self._kv_text("Mirror root", self._mirror_root))

        yield Label("Capabilities", classes="section-title")
        caps = detect_capabilities()
        yield Static(
            self._kv_text("Claude Code", caps.get("claude_code_version") or "—")
        )
        yield Static(self._kv_text("Node", caps.get("node_version") or "—"))
        yield Static(self._kv_text("Python", caps.get("python_version") or "—"))
        yield Static(
            self._kv_text(
                "Max concurrent runs", caps.get("max_concurrent_runs") or "—"
            )
        )

        yield Label("Last reconcile", classes="section-title")
        self._reconcile_label = Label("(waiting…)", classes="kv-value")
        yield self._reconcile_label

    def update_reconcile(self, payload: dict[str, Any]) -> None:
        if self._reconcile_label is None:
            return
        msg = (
            f"pulled={payload.get('pulled', 0)}  "
            f"pushed={payload.get('pushed', 0)}  "
            f"deleted_local={payload.get('deleted_local', 0)}  "
            f"conflicts={payload.get('conflicts', 0)}  "
            f"errors={payload.get('errors', 0)}  "
            f"in_sync={payload.get('in_sync', 0)}"
        )
        self._reconcile_label.update(msg)


# --- Runs pane ------------------------------------------------------------


class RunsPane(Vertical):
    """Live table of dispatched runs + their lifecycle state."""

    def compose(self) -> ComposeResult:
        yield Label("Active and recent runs", classes="section-title")
        table = DataTable(zebra_stripes=False, cursor_type="row", id="runs-table")
        table.add_column("Run", key="run")
        table.add_column("Agent", key="agent")
        table.add_column("Department", key="department")
        table.add_column("Status", key="status")
        table.add_column("Steps", key="steps")
        table.add_column("Updated", key="updated")
        yield table

    def _table(self) -> DataTable:
        return self.query_one("#runs-table", DataTable)

    def upsert_run(
        self,
        *,
        run_id: str,
        agent_name: str | None = None,
        department_slug: str | None = None,
        status: str | None = None,
        bump_step: bool = False,
    ) -> None:
        if not run_id:
            return
        table = self._table()
        key = run_id
        if key in table.rows:
            row = table.get_row(key)
            try:
                steps = int(str(row[4]) or 0)
            except (ValueError, IndexError):
                steps = 0
            if bump_step:
                steps += 1
            current_status = row[3].plain if isinstance(row[3], Text) else str(row[3])
            new_status = status or current_status
            new_agent = agent_name or row[1]
            new_dept = department_slug or row[2]
            table.update_cell(key, "agent", new_agent)
            table.update_cell(key, "department", new_dept)
            table.update_cell(key, "status", _format_status(new_status))
            table.update_cell(key, "steps", str(steps))
            table.update_cell(key, "updated", _fmt_clock(time.time()))
        else:
            table.add_row(
                _short(run_id, 8),
                agent_name or "—",
                department_slug or "—",
                _format_status(status or "queued"),
                "1" if bump_step else "0",
                _fmt_clock(time.time()),
                key=key,
            )


def _format_status(status: str) -> Text:
    color = {
        "queued": "#7d8a9a",
        "started": "#fbbf24",
        "running": "#fbbf24",
        "step": "#fbbf24",
        "completed": "#4ade80",
        "failed": "#f87171",
        "awaiting_approval": "#c084fc",
        "device_quiet": "#fbbf24",
    }.get(status, "#a3b0bf")
    return Text(status, style=f"bold {color}")


# --- Files pane -----------------------------------------------------------


class FilesPane(Vertical):
    """Recent fs.* events in chronological order."""

    MAX_ROWS = 200

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._row_keys: deque[str] = deque(maxlen=self.MAX_ROWS)
        self._counter = 0

    def compose(self) -> ComposeResult:
        yield Label("Recent file sync activity", classes="section-title")
        table = DataTable(zebra_stripes=False, cursor_type="row", id="files-table")
        table.add_columns("Time", "Direction", "Scope", "Path", "Size")
        yield table

    def add_event(
        self,
        *,
        direction: str,
        path: str,
        size: int | None,
        scope: str | None,
        agent_slug: str | None,
        department_slug: str | None,
    ) -> None:
        table = self.query_one("#files-table", DataTable)
        scope_label = (
            f"agent/{agent_slug}"
            if scope == "agent" and agent_slug
            else f"dept/{department_slug}"
            if scope == "department" and department_slug
            else scope or "—"
        )
        arrow_color = {
            "↓ pulled": "#beafe7",
            "↑ pushed": "#2dd4bf",
            "✕ deleted_local": "#f87171",
            "✕ deleted_remote": "#f87171",
        }.get(direction, "#a3b0bf")
        self._counter += 1
        key = f"fs-{self._counter}"
        table.add_row(
            _fmt_clock(time.time()),
            Text(direction, style=arrow_color),
            scope_label,
            path,
            _fmt_size(size) if size is not None else "—",
            key=key,
        )
        if len(self._row_keys) == self.MAX_ROWS:
            evict = self._row_keys.popleft()
            try:
                table.remove_row(evict)
            except Exception:
                pass
        self._row_keys.append(key)


# --- Logs pane ------------------------------------------------------------


class LogsPane(VerticalScroll):
    def compose(self) -> ComposeResult:
        yield Label("Daemon logs (tail)", classes="section-title")
        yield RichLog(
            id="logs-view",
            highlight=True,
            markup=False,
            wrap=True,
            max_lines=2000,
        )

    def append(self, line: str, level: str = "INFO") -> None:
        view = self.query_one("#logs-view", RichLog)
        color = {
            "DEBUG": "#7d8a9a",
            "INFO": "#a3b0bf",
            "WARNING": "#fbbf24",
            "ERROR": "#f87171",
            "CRITICAL": "#f87171",
        }.get(level, "#a3b0bf")
        ts = datetime.now().strftime("%H:%M:%S")
        view.write(Text.assemble((f"{ts} ", "#525252"), (line, color)))


# --- Logging bridge -------------------------------------------------------


class _LogBridge(logging.Handler):
    """Forwards log records into a deque the TUI drains on a timer."""

    def __init__(self, buffer: deque) -> None:
        super().__init__(level=logging.INFO)
        self._buf = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        self._buf.append((record.levelname, msg))


# --- App ------------------------------------------------------------------


class XelosTUI(App):
    """Textual entrypoint."""

    TITLE = "XELOS EDGE"
    CSS = PALETTE_CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("s", "switch_tab('status')", "Status"),
        Binding("r", "switch_tab('runs')", "Runs"),
        Binding("f", "switch_tab('files')", "Files"),
        Binding("l", "switch_tab('logs')", "Logs"),
    ]

    def __init__(
        self,
        *,
        credentials: Credentials,
        log_frames: bool = False,
    ) -> None:
        super().__init__()
        self._creds = credentials
        self._log_frames = log_frames
        self._daemon = Daemon(
            credentials,
            options=DaemonOptions(
                log_frames=log_frames,
                install_signal_handlers=False,
            ),
        )
        self._daemon_task: asyncio.Task | None = None
        self._event_task: asyncio.Task | None = None
        self._log_drain_task: asyncio.Task | None = None
        self._log_buffer: deque[tuple[str, str]] = deque(maxlen=2000)
        self._mirror_root = str(_xelos_home() / "mirror")

    def compose(self) -> ComposeResult:
        yield HeaderBar(
            device_id=str(self._creds.device_id),
            user_id=str(self._creds.user_id),
        )
        with TabbedContent(initial="tab-status", id="tabs"):
            with TabPane("Status", id="tab-status"):
                yield StatusPane(creds=self._creds, mirror_root=self._mirror_root)
            with TabPane("Runs", id="tab-runs"):
                yield RunsPane(id="runs-pane")
            with TabPane("Files", id="tab-files"):
                yield FilesPane(id="files-pane")
            with TabPane("Logs", id="tab-logs"):
                yield LogsPane(id="logs-pane")
        yield Footer()

    # --- lifecycle ---

    async def on_mount(self) -> None:
        self._install_log_bridge()
        self._daemon_task = asyncio.create_task(self._daemon.run())
        self._event_task = asyncio.create_task(self._drain_events())
        self._log_drain_task = asyncio.create_task(self._drain_log_buffer())

    async def on_unmount(self) -> None:
        for task in (self._event_task, self._log_drain_task):
            if task is not None:
                task.cancel()
        if self._daemon_task is not None:
            self._daemon_task.cancel()
            try:
                await self._daemon_task
            except (asyncio.CancelledError, Exception):
                pass

    # --- actions ---

    def action_switch_tab(self, key: str) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = f"tab-{key}"

    # --- internals ---

    def _install_log_bridge(self) -> None:
        bridge = _LogBridge(self._log_buffer)
        bridge.setFormatter(
            logging.Formatter("%(name)s :: %(message)s")
        )
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(bridge)
        root.setLevel(logging.INFO)
        for noisy in ("httpx", "httpcore", "websockets"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    async def _drain_log_buffer(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.25)
                if not self._log_buffer:
                    continue
                pane = self.query_one("#logs-pane", LogsPane)
                while self._log_buffer:
                    level, msg = self._log_buffer.popleft()
                    pane.append(msg, level=level)
        except asyncio.CancelledError:
            return

    async def _drain_events(self) -> None:
        queue = EVENTS.subscribe()
        try:
            while True:
                ev: Event = await queue.get()
                self._handle_event(ev)
        except asyncio.CancelledError:
            EVENTS.unsubscribe(queue)
            return

    def _handle_event(self, ev: Event) -> None:
        kind = ev.kind
        payload = ev.payload
        header = self.query_one(HeaderBar)

        if kind == "ws.connecting":
            header.ws_state = "connecting"
            return
        if kind == "ws.connected":
            header.ws_state = "live"
            return
        if kind == "ws.disconnected":
            header.ws_state = "down"
            return

        if kind == "reconcile.completed":
            try:
                self.query_one(StatusPane).update_reconcile(payload)
            except Exception:
                pass
            return
        if kind == "reconcile.failed":
            try:
                self.query_one(StatusPane).update_reconcile(
                    {"errors": 1, "_reason": payload.get("error", "")}
                )
            except Exception:
                pass
            return

        if kind == "run.dispatch":
            self.query_one(RunsPane).upsert_run(
                run_id=str(payload.get("run_id") or ""),
                agent_name=payload.get("agent_name"),
                department_slug=payload.get("department_slug"),
                status="queued",
            )
            return
        if kind.startswith("run."):
            sub = kind[len("run."):]
            self.query_one(RunsPane).upsert_run(
                run_id=str(payload.get("run_id") or ""),
                status=sub,
                bump_step=sub
                in ("step", "thinking", "tool_call", "tool_result", "assistant_message"),
            )
            return

        if kind == "fs.pulled":
            self.query_one(FilesPane).add_event(
                direction="↓ pulled",
                path=str(payload.get("path") or ""),
                size=payload.get("size"),
                scope=payload.get("scope"),
                department_slug=payload.get("department_slug"),
                agent_slug=payload.get("agent_slug"),
            )
            return
        if kind == "fs.pushed":
            self.query_one(FilesPane).add_event(
                direction="↑ pushed",
                path=str(payload.get("path") or ""),
                size=payload.get("size"),
                scope=payload.get("scope"),
                department_slug=payload.get("department_slug"),
                agent_slug=payload.get("agent_slug"),
            )
            return
        if kind in ("fs.deleted_local", "fs.deleted_remote"):
            tag = "✕ " + kind.split(".", 1)[1]
            self.query_one(FilesPane).add_event(
                direction=tag,
                path=str(payload.get("path") or ""),
                size=None,
                scope=payload.get("scope"),
                department_slug=payload.get("department_slug"),
                agent_slug=payload.get("agent_slug"),
            )
            return


def run_tui(*, credentials: Credentials, log_frames: bool = False) -> None:
    app = XelosTUI(credentials=credentials, log_frames=log_frames)
    app.run()


__all__ = ["XelosTUI", "run_tui"]
