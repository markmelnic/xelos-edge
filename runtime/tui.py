"""Textual TUI for `xelos serve` — Status / Runs / Files / Workspaces / Logs."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Label,
    RichLog,
    Sparkline,
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
    overflow: hidden;
}

Header {
    background: #0d131c;
    color: #beafe7;
    height: 3;
    padding: 1 2;
    border-bottom: solid #28344a;
}

TabbedContent {
    background: #0d131c;
    margin: 0 1;
    height: 1fr;
}

TabPane {
    padding: 1 2;
    overflow: hidden;
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

/* Status page layout */
#status-root {
    height: 1fr;
    overflow: hidden;
}

#status-cards {
    grid-size: 3 1;
    grid-gutter: 1 2;
    height: auto;
    margin-bottom: 1;
}

.card {
    background: #0d131c;
    border: solid #161e2c;
    padding: 1 2;
    height: auto;
}

.card-title {
    color: #beafe7;
    text-style: bold;
    margin-bottom: 1;
}

.metric-row {
    height: 1;
}

.metric-key {
    color: #7d8a9a;
    width: 14;
}

.metric-value {
    color: #e6edf3;
    text-style: bold;
}

.dim {
    color: #7d8a9a;
}

.accent {
    color: #beafe7;
}

.good {
    color: #4ade80;
}

.warn {
    color: #fbbf24;
}

.bad {
    color: #f87171;
}

#live-log-card {
    height: 1fr;
    background: #0d131c;
    border: solid #161e2c;
    padding: 0 1;
}

#live-log-title {
    color: #beafe7;
    text-style: bold;
    padding: 1 1 0 1;
}

#live-log {
    height: 1fr;
    background: #06080c;
    border: none;
    padding: 0 1;
    margin: 1 0 0 0;
}

Sparkline {
    height: 1;
    width: 1fr;
    color: #beafe7;
    background: #06080c;
}

#workspaces-grid {
    grid-size: 2;
    grid-gutter: 1 2;
    height: auto;
}

#logs-view {
    height: 1fr;
    background: #06080c;
    border: solid #161e2c;
    padding: 0 1;
}

#runs-table, #files-table, #workspaces-table {
    height: 1fr;
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


def _fmt_uptime(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    td = timedelta(seconds=int(seconds))
    days, rem = divmod(td.total_seconds(), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{int(days)}d {int(hours):02d}h"
    if hours:
        return f"{int(hours)}h {int(minutes):02d}m"
    return f"{int(minutes)}m"


def _dir_size(path: Path) -> int:
    """Total bytes under `path`. Best-effort — silent on errors."""
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


@dataclass
class MetricsState:
    """Counters the TUI tracks across all event kinds — feeds the Status cards."""

    started_at: float = field(default_factory=time.time)
    runs_dispatched: int = 0
    runs_completed: int = 0
    runs_failed: int = 0
    runs_active: int = 0
    fs_pulled: int = 0
    fs_pushed: int = 0
    fs_deleted: int = 0
    last_reconcile_at: float | None = None
    last_reconcile_summary: dict[str, Any] = field(default_factory=dict)
    # Sparkline buckets — one int per second over the last 60 seconds.
    activity_buckets: deque[int] = field(default_factory=lambda: deque([0] * 60, maxlen=60))
    activity_last_second: int = 0
    activity_current: int = 0

    def tick(self) -> None:
        """Advance the activity sparkline by one second."""
        now_sec = int(time.time())
        if self.activity_last_second == 0:
            self.activity_last_second = now_sec
            return
        gap = now_sec - self.activity_last_second
        if gap <= 0:
            return
        for _ in range(min(gap, 60)):
            self.activity_buckets.append(self.activity_current)
            self.activity_current = 0
        self.activity_last_second = now_sec

    def bump_activity(self) -> None:
        self.activity_current += 1

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at


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


def _metric_row(key: str, value: str, value_class: str = "metric-value") -> Static:
    """One key + value line inside a card."""
    text = Text()
    text.append(f"{key:<14}", style="bold #7d8a9a")
    text.append(value, style=("#e6edf3"))
    s = Static(text, classes="metric-row")
    return s


class HealthCard(Vertical):
    """Daemon health: WS state, uptime, mirror size, capabilities."""

    DEFAULT_CSS = """
    HealthCard { layout: vertical; height: auto; }
    """

    def __init__(self, *, creds: Credentials, mirror_root: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._creds = creds
        self._mirror_root = Path(mirror_root)
        self._ws_state = "connecting"
        self._uptime = "0s"
        self._mirror_size = "—"
        caps = detect_capabilities()
        self._cc = caps.get("claude_code_version") or "—"
        self._node = caps.get("node_version") or "—"

    def compose(self) -> ComposeResult:
        yield Label("⚡ Health", classes="card-title")
        yield Static(self._body(), id="health-body")

    def _body(self) -> Text:
        ws_dot, ws_color = {
            "live": ("●", "#4ade80"),
            "connecting": ("◐", "#fbbf24"),
            "down": ("●", "#f87171"),
        }.get(self._ws_state, ("◌", "#7d8a9a"))
        text = Text()
        text.append(f"{'Cloud WS':<14}", style="bold #7d8a9a")
        text.append(f"{ws_dot} {self._ws_state}\n", style=f"bold {ws_color}")
        text.append(f"{'Uptime':<14}", style="bold #7d8a9a")
        text.append(f"{self._uptime}\n", style="#e6edf3")
        text.append(f"{'Mirror size':<14}", style="bold #7d8a9a")
        text.append(f"{self._mirror_size}\n", style="#e6edf3")
        text.append(f"{'Claude Code':<14}", style="bold #7d8a9a")
        text.append(f"{self._cc}\n", style="#e6edf3")
        text.append(f"{'Node':<14}", style="bold #7d8a9a")
        text.append(f"{self._node}", style="#e6edf3")
        return text

    def set_ws_state(self, state: str) -> None:
        self._ws_state = state
        self._refresh()

    def set_uptime(self, uptime: str) -> None:
        self._uptime = uptime
        self._refresh()

    def set_mirror_size(self, size: str) -> None:
        self._mirror_size = size
        self._refresh()

    def _refresh(self) -> None:
        try:
            self.query_one("#health-body", Static).update(self._body())
        except Exception:
            pass


class SyncCard(Vertical):
    """Cumulative file-sync counters + last reconcile summary."""

    DEFAULT_CSS = """
    SyncCard { layout: vertical; height: auto; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pulled = 0
        self._pushed = 0
        self._deleted = 0
        self._last_reconcile = "—"

    def compose(self) -> ComposeResult:
        yield Label("⇅ Sync", classes="card-title")
        yield Static(self._body(), id="sync-body")

    def _body(self) -> Text:
        text = Text()
        text.append(f"{'Pulled':<14}", style="bold #7d8a9a")
        text.append(f"{self._pulled}\n", style="bold #beafe7")
        text.append(f"{'Pushed':<14}", style="bold #7d8a9a")
        text.append(f"{self._pushed}\n", style="bold #2dd4bf")
        text.append(f"{'Deleted':<14}", style="bold #7d8a9a")
        text.append(f"{self._deleted}\n", style="bold #f87171")
        text.append(f"{'Last sync':<14}", style="bold #7d8a9a")
        text.append(f"{self._last_reconcile}", style="#a3b0bf")
        return text

    def update_counters(self, *, pulled: int, pushed: int, deleted: int) -> None:
        self._pulled = pulled
        self._pushed = pushed
        self._deleted = deleted
        self._refresh()

    def update_reconcile(self, payload: dict[str, Any]) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        in_sync = payload.get("in_sync", 0)
        errors = payload.get("errors", 0)
        flag = "✓" if not errors else "✕"
        self._last_reconcile = (
            f"{flag} {ts}  in_sync={in_sync}  errors={errors}"
        )
        self._refresh()

    def _refresh(self) -> None:
        try:
            self.query_one("#sync-body", Static).update(self._body())
        except Exception:
            pass


class ActivityCard(Vertical):
    """Run lifecycle counters + a 60s sparkline of event throughput."""

    DEFAULT_CSS = """
    ActivityCard { layout: vertical; height: auto; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._dispatched = 0
        self._completed = 0
        self._failed = 0
        self._active = 0
        self._sparkline: Sparkline | None = None

    def compose(self) -> ComposeResult:
        yield Label("◐ Runs", classes="card-title")
        yield Static(self._body(), id="activity-body")
        self._sparkline = Sparkline([0] * 60, id="activity-spark", summary_function=max)
        yield self._sparkline

    def _body(self) -> Text:
        text = Text()
        text.append(f"{'Active':<14}", style="bold #7d8a9a")
        text.append(f"{self._active}\n", style="bold #fbbf24")
        text.append(f"{'Completed':<14}", style="bold #7d8a9a")
        text.append(f"{self._completed}\n", style="bold #4ade80")
        text.append(f"{'Failed':<14}", style="bold #7d8a9a")
        text.append(f"{self._failed}\n", style="bold #f87171")
        text.append(f"{'Dispatched':<14}", style="bold #7d8a9a")
        text.append(f"{self._dispatched}", style="#e6edf3")
        return text

    def update_counters(
        self,
        *,
        dispatched: int,
        completed: int,
        failed: int,
        active: int,
    ) -> None:
        self._dispatched = dispatched
        self._completed = completed
        self._failed = failed
        self._active = active
        self._refresh()

    def update_sparkline(self, samples: list[int]) -> None:
        if self._sparkline is None:
            return
        try:
            self._sparkline.data = list(samples)
        except Exception:
            pass

    def _refresh(self) -> None:
        try:
            self.query_one("#activity-body", Static).update(self._body())
        except Exception:
            pass


class StatusPane(Vertical):
    """Daemon health + sync + activity cards stacked above a live log tail."""

    def __init__(self, *, creds: Credentials, mirror_root: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._creds = creds
        self._mirror_root = mirror_root

    def compose(self) -> ComposeResult:
        with Vertical(id="status-root"):
            with Grid(id="status-cards"):
                yield HealthCard(
                    creds=self._creds,
                    mirror_root=self._mirror_root,
                    classes="card",
                    id="health-card",
                )
                yield SyncCard(classes="card", id="sync-card")
                yield ActivityCard(classes="card", id="activity-card")
            with Vertical(id="live-log-card"):
                yield Label("◇ Live tail", id="live-log-title")
                yield RichLog(
                    id="live-log",
                    highlight=True,
                    markup=False,
                    wrap=True,
                    max_lines=200,
                )

    def append_log(self, line: str, level: str = "INFO") -> None:
        try:
            view = self.query_one("#live-log", RichLog)
        except Exception:
            return
        color = {
            "DEBUG": "#7d8a9a",
            "INFO": "#a3b0bf",
            "WARNING": "#fbbf24",
            "ERROR": "#f87171",
            "CRITICAL": "#f87171",
        }.get(level, "#a3b0bf")
        ts = datetime.now().strftime("%H:%M:%S")
        view.write(Text.assemble((f"{ts} ", "#525252"), (line, color)))


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


# --- Workspaces pane ------------------------------------------------------


class WorkspacesPane(VerticalScroll):
    """Per-workspace mirror state. Updates from reconcile + fs events."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._slots: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        yield Label("Mirrored workspaces", classes="section-title")
        table = DataTable(zebra_stripes=False, cursor_type="row", id="workspaces-table")
        table.add_column("Workspace", key="workspace")
        table.add_column("Pulled", key="pulled")
        table.add_column("Pushed", key="pushed")
        table.add_column("Conflicts", key="conflicts")
        table.add_column("Errors", key="errors")
        table.add_column("Last reconcile", key="last")
        yield table

    def _table(self) -> DataTable:
        return self.query_one("#workspaces-table", DataTable)

    def upsert(
        self,
        *,
        slug: str,
        pulled: int = 0,
        pushed: int = 0,
        conflicts: int = 0,
        errors: int = 0,
        last_reconcile_ts: float | None = None,
    ) -> None:
        if not slug:
            return
        slot = self._slots.setdefault(
            slug,
            {"pulled": 0, "pushed": 0, "conflicts": 0, "errors": 0, "last": None},
        )
        slot["pulled"] = pulled or slot["pulled"]
        slot["pushed"] = pushed or slot["pushed"]
        slot["conflicts"] = conflicts or slot["conflicts"]
        slot["errors"] = errors or slot["errors"]
        if last_reconcile_ts:
            slot["last"] = last_reconcile_ts

        last_str = (
            _fmt_clock(slot["last"]) if slot["last"] else "—"
        )
        table = self._table()
        if slug in table.rows:
            table.update_cell(slug, "pulled", str(slot["pulled"]))
            table.update_cell(slug, "pushed", str(slot["pushed"]))
            table.update_cell(slug, "conflicts", str(slot["conflicts"]))
            table.update_cell(slug, "errors", str(slot["errors"]))
            table.update_cell(slug, "last", last_str)
        else:
            table.add_row(
                Text(slug, style="bold #beafe7"),
                str(slot["pulled"]),
                str(slot["pushed"]),
                Text(str(slot["conflicts"]), style="#fbbf24" if slot["conflicts"] else None),
                Text(str(slot["errors"]), style="#f87171" if slot["errors"] else None),
                last_str,
                key=slug,
            )

    def bump_fs(self, *, slug: str | None, kind: str) -> None:
        """Increment the relevant counter on a workspace from a fs.* event."""
        if not slug:
            return
        slot = self._slots.setdefault(
            slug,
            {"pulled": 0, "pushed": 0, "conflicts": 0, "errors": 0, "last": None},
        )
        if kind == "pulled":
            slot["pulled"] += 1
        elif kind == "pushed":
            slot["pushed"] += 1
        self.upsert(slug=slug)


# --- Logs pane ------------------------------------------------------------


class LogsPane(Vertical):
    """Daemon log tail. Uses Vertical (not VerticalScroll) so RichLog's own
    scroll behaviour isn't doubled by an outer scroll container."""

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
        Binding("w", "switch_tab('workspaces')", "Workspaces"),
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
        self._tick_task: asyncio.Task | None = None
        self._log_buffer: deque[tuple[str, str]] = deque(maxlen=2000)
        self._mirror_root = str(_xelos_home() / "mirror")
        self._metrics = MetricsState()

    def compose(self) -> ComposeResult:
        yield HeaderBar(
            device_id=str(self._creds.device_id),
            user_id=str(self._creds.user_id),
        )
        with TabbedContent(initial="tab-status", id="tabs"):
            with TabPane("Status", id="tab-status"):
                yield StatusPane(
                    creds=self._creds,
                    mirror_root=self._mirror_root,
                    id="status-pane",
                )
            with TabPane("Runs", id="tab-runs"):
                yield RunsPane(id="runs-pane")
            with TabPane("Files", id="tab-files"):
                yield FilesPane(id="files-pane")
            with TabPane("Workspaces", id="tab-workspaces"):
                yield WorkspacesPane(id="workspaces-pane")
            with TabPane("Logs", id="tab-logs"):
                yield LogsPane(id="logs-pane")
        yield Footer()

    # --- lifecycle ---

    async def on_mount(self) -> None:
        self._install_log_bridge()
        # Hydrate from local store BEFORE daemon starts streaming, so
        # restart-after-restart shows continuous history rather than a
        # blank canvas.
        self._hydrate_from_state()
        self._daemon_task = asyncio.create_task(self._daemon.run())
        self._event_task = asyncio.create_task(self._drain_events())
        self._log_drain_task = asyncio.create_task(self._drain_log_buffer())
        self._tick_task = asyncio.create_task(self._tick_metrics())

    def _hydrate_from_state(self) -> None:
        state = getattr(self._daemon, "_state", None)
        if state is None:
            return
        try:
            recent = state.recent_runs(limit=100)
        except Exception:  # pragma: no cover
            log.exception("hydrate runs from state_db failed")
            recent = []
        if recent:
            try:
                runs_pane = self.query_one(RunsPane)
                ws_pane = self.query_one(WorkspacesPane)
            except Exception:
                runs_pane = None
                ws_pane = None
            for r in recent:
                if runs_pane is not None:
                    runs_pane.upsert_run(
                        run_id=r.run_id,
                        agent_name=r.agent_name,
                        department_slug=r.department_slug,
                        status=r.status,
                    )
                if ws_pane is not None and r.workspace_slug:
                    ws_pane.upsert(slug=r.workspace_slug)
                # Bucket lifecycle counters from history so the
                # ActivityCard isn't all zeros after a restart.
                self._metrics.runs_dispatched += 1
                if r.status == "completed":
                    self._metrics.runs_completed += 1
                elif r.status == "failed":
                    self._metrics.runs_failed += 1
                elif r.status in ("running", "queued"):
                    # Mark as orphaned-running — daemon's reconnect will
                    # re-emit terminal events when state is reconciled.
                    self._metrics.runs_active += 1
            try:
                activity = self.query_one("#activity-card", ActivityCard)
                activity.update_counters(
                    dispatched=self._metrics.runs_dispatched,
                    completed=self._metrics.runs_completed,
                    failed=self._metrics.runs_failed,
                    active=max(self._metrics.runs_active, 0),
                )
            except Exception:
                pass

        try:
            recent_logs = state.recent_logs(limit=200)
        except Exception:  # pragma: no cover
            recent_logs = []
        if recent_logs:
            try:
                logs_pane = self.query_one("#logs-pane", LogsPane)
                status_pane = self.query_one("#status-pane", StatusPane)
            except Exception:
                logs_pane = None
                status_pane = None
            for rec in recent_logs:
                if logs_pane is not None:
                    logs_pane.append(rec.msg, level=rec.level)
                if status_pane is not None:
                    status_pane.append_log(rec.msg, level=rec.level)

    async def on_unmount(self) -> None:
        for task in (self._event_task, self._log_drain_task, self._tick_task):
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
        state = getattr(self._daemon, "_state", None)
        try:
            while True:
                await asyncio.sleep(0.25)
                if not self._log_buffer:
                    continue
                pane = self.query_one("#logs-pane", LogsPane)
                try:
                    status = self.query_one("#status-pane", StatusPane)
                except Exception:
                    status = None
                while self._log_buffer:
                    level, msg = self._log_buffer.popleft()
                    pane.append(msg, level=level)
                    if status is not None:
                        status.append_log(msg, level=level)
                    # Mirror to local store so a restart shows continuity.
                    if state is not None:
                        try:
                            state.append_log(level, msg)
                        except Exception:  # pragma: no cover
                            pass
        except asyncio.CancelledError:
            return

    async def _tick_metrics(self) -> None:
        """1Hz tick: refresh uptime + sparkline + occasional mirror size."""
        last_size_check = 0.0
        try:
            while True:
                await asyncio.sleep(1.0)
                self._metrics.tick()
                try:
                    health = self.query_one("#health-card", HealthCard)
                    health.set_uptime(_fmt_uptime(self._metrics.uptime_seconds))
                except Exception:
                    pass
                try:
                    activity = self.query_one("#activity-card", ActivityCard)
                    activity.update_sparkline(list(self._metrics.activity_buckets))
                except Exception:
                    pass
                # Mirror dir scan is expensive; do it every 30s.
                if time.time() - last_size_check > 30:
                    last_size_check = time.time()
                    try:
                        size = _dir_size(Path(self._mirror_root))
                        health = self.query_one("#health-card", HealthCard)
                        health.set_mirror_size(_fmt_size(size))
                    except Exception:
                        pass
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
        self._metrics.bump_activity()

        # Card refresh helper.
        def _refresh_activity() -> None:
            try:
                self.query_one("#activity-card", ActivityCard).update_counters(
                    dispatched=self._metrics.runs_dispatched,
                    completed=self._metrics.runs_completed,
                    failed=self._metrics.runs_failed,
                    active=max(self._metrics.runs_active, 0),
                )
            except Exception:
                pass

        def _refresh_sync() -> None:
            try:
                self.query_one("#sync-card", SyncCard).update_counters(
                    pulled=self._metrics.fs_pulled,
                    pushed=self._metrics.fs_pushed,
                    deleted=self._metrics.fs_deleted,
                )
            except Exception:
                pass

        if kind == "ws.connecting":
            header.ws_state = "connecting"
            try:
                self.query_one("#health-card", HealthCard).set_ws_state("connecting")
            except Exception:
                pass
            return
        if kind == "ws.connected":
            header.ws_state = "live"
            try:
                self.query_one("#health-card", HealthCard).set_ws_state("live")
            except Exception:
                pass
            return
        if kind == "ws.disconnected":
            header.ws_state = "down"
            try:
                self.query_one("#health-card", HealthCard).set_ws_state("down")
            except Exception:
                pass
            return

        if kind == "reconcile.completed":
            self._metrics.last_reconcile_at = time.time()
            self._metrics.last_reconcile_summary = dict(payload)
            try:
                self.query_one("#sync-card", SyncCard).update_reconcile(payload)
            except Exception:
                pass
            try:
                self.query_one("#workspaces-pane", WorkspacesPane).upsert(
                    slug=str(payload.get("workspace_slug") or "default"),
                    pulled=int(payload.get("pulled") or 0),
                    pushed=int(payload.get("pushed") or 0),
                    conflicts=int(payload.get("conflicts") or 0),
                    errors=int(payload.get("errors") or 0),
                    last_reconcile_ts=time.time(),
                )
            except Exception:
                pass
            return
        if kind == "reconcile.failed":
            try:
                self.query_one("#sync-card", SyncCard).update_reconcile(
                    {"errors": 1, "_reason": payload.get("error", "")}
                )
            except Exception:
                pass
            return

        if kind == "run.dispatch":
            self._metrics.runs_dispatched += 1
            self._metrics.runs_active += 1
            self.query_one(RunsPane).upsert_run(
                run_id=str(payload.get("run_id") or ""),
                agent_name=payload.get("agent_name"),
                department_slug=payload.get("department_slug"),
                status="queued",
            )
            _refresh_activity()
            return
        if kind.startswith("run."):
            sub = kind[len("run."):]
            if sub in ("completed",):
                self._metrics.runs_completed += 1
                self._metrics.runs_active = max(0, self._metrics.runs_active - 1)
            elif sub in ("failed",):
                self._metrics.runs_failed += 1
                self._metrics.runs_active = max(0, self._metrics.runs_active - 1)
            self.query_one(RunsPane).upsert_run(
                run_id=str(payload.get("run_id") or ""),
                status=sub,
                bump_step=sub
                in ("step", "thinking", "tool_call", "tool_result", "assistant_message"),
            )
            _refresh_activity()
            return

        if kind == "fs.pulled":
            self._metrics.fs_pulled += 1
            self.query_one(FilesPane).add_event(
                direction="↓ pulled",
                path=str(payload.get("path") or ""),
                size=payload.get("size"),
                scope=payload.get("scope"),
                department_slug=payload.get("department_slug"),
                agent_slug=payload.get("agent_slug"),
            )
            try:
                self.query_one("#workspaces-pane", WorkspacesPane).bump_fs(
                    slug=payload.get("workspace_slug"), kind="pulled"
                )
            except Exception:
                pass
            _refresh_sync()
            return
        if kind == "fs.pushed":
            self._metrics.fs_pushed += 1
            self.query_one(FilesPane).add_event(
                direction="↑ pushed",
                path=str(payload.get("path") or ""),
                size=payload.get("size"),
                scope=payload.get("scope"),
                department_slug=payload.get("department_slug"),
                agent_slug=payload.get("agent_slug"),
            )
            try:
                self.query_one("#workspaces-pane", WorkspacesPane).bump_fs(
                    slug=payload.get("workspace_slug"), kind="pushed"
                )
            except Exception:
                pass
            _refresh_sync()
            return
        if kind in ("fs.deleted_local", "fs.deleted_remote"):
            self._metrics.fs_deleted += 1
            tag = "✕ " + kind.split(".", 1)[1]
            self.query_one(FilesPane).add_event(
                direction=tag,
                path=str(payload.get("path") or ""),
                size=None,
                scope=payload.get("scope"),
                department_slug=payload.get("department_slug"),
                agent_slug=payload.get("agent_slug"),
            )
            _refresh_sync()
            return


def run_tui(*, credentials: Credentials, log_frames: bool = False) -> None:
    app = XelosTUI(credentials=credentials, log_frames=log_frames)
    app.run()


__all__ = ["XelosTUI", "run_tui"]
