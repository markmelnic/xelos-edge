"""WebSocket daemon — connects to cloud, sends heartbeats, awaits frames.

P0 scope: hello + heartbeat. Frame protocol (job.start, fs.*) lands in P2.
Reconnect is exponential-backoff with full-jitter; capped at 60s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import signal
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .capabilities import detect as detect_capabilities
from .config import Credentials

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 30
RECONNECT_INITIAL_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 60.0


@dataclass(slots=True)
class DaemonOptions:
    log_frames: bool = False


class Daemon:
    def __init__(
        self,
        credentials: Credentials,
        *,
        options: DaemonOptions | None = None,
    ) -> None:
        self.credentials = credentials
        self.options = options or DaemonOptions()
        self._stop = asyncio.Event()
        self._ws: Any = None

    async def run(self) -> None:
        self._install_signal_handlers()

        backoff = RECONNECT_INITIAL_SECONDS
        while not self._stop.is_set():
            try:
                await self._session()
                # Clean exit (server closed) → reset backoff and loop.
                backoff = RECONNECT_INITIAL_SECONDS
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError, InvalidStatus) as exc:
                log.warning("ws disconnected: %s", exc)
            except Exception:
                log.exception("ws session crashed")

            if self._stop.is_set():
                break

            # Full-jitter backoff so multiple devices don't reconnect in
            # lockstep after a server restart.
            sleep_for = random.uniform(0, backoff)
            log.info("reconnecting in %.1fs", sleep_for)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                # _stop fired → exit loop.
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

        log.info("daemon stopped")

    async def _session(self) -> None:
        url = self._authed_ws_url()
        log.info("connecting to %s", _redact_token(url))
        async with websockets.connect(
            url,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            log.info("ws connected")

            await self._send(
                ws,
                {
                    "type": "hello",
                    "edge_version_supported": [1],
                    "capabilities": detect_capabilities(),
                },
            )

            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                async for raw in ws:
                    if isinstance(raw, bytes):
                        # Binary frames reserved for fs.chunk in P1+.
                        if self.options.log_frames:
                            log.debug("binary frame %d bytes", len(raw))
                        continue
                    try:
                        frame = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("non-JSON text frame")
                        continue
                    await self._handle_frame(ws, frame)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                self._ws = None

    async def _heartbeat_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            try:
                await self._send(ws, {"type": "heartbeat"})
            except ConnectionClosed:
                return

    async def _handle_frame(self, ws: Any, frame: dict[str, Any]) -> None:
        ftype = frame.get("type")
        if self.options.log_frames:
            log.debug("recv %s", ftype)

        if ftype in ("hello_ack", "heartbeat_ack", "ready", "ignored"):
            return
        if ftype == "error":
            log.error("server error: %s", frame.get("error"))
            return
        # P0 ignores unknown frames so future protocol versions degrade
        # gracefully against old daemons.
        log.debug("unknown frame type=%s — ignoring", ftype)

    async def _send(self, ws: Any, frame: dict[str, Any]) -> None:
        if self.options.log_frames:
            log.debug("send %s", frame.get("type"))
        await ws.send(json.dumps(frame, default=str))

    def _authed_ws_url(self) -> str:
        sep = "&" if "?" in self.credentials.websocket_url else "?"
        return f"{self.credentials.websocket_url}{sep}token={self.credentials.token}"

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                # Windows / non-main-thread.
                pass


def _redact_token(url: str) -> str:
    if "token=" not in url:
        return url
    head, _, tail = url.partition("token=")
    if "&" in tail:
        return f"{head}token=<redacted>&{tail.split('&', 1)[1]}"
    return f"{head}token=<redacted>"
