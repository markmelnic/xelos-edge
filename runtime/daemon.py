"""WebSocket daemon — connects to cloud, sends heartbeats, awaits frames.

Frame router as of P1a:
  hello_ack / heartbeat_ack / ready / ignored / error  — handshake
  fs.push   — cloud→device file write (inline base64 or presigned URL)
  fs.delete — cloud→device delete
Frame protocol for job.* and device→cloud fs.* lands in later phases.
Reconnect is exponential-backoff with full-jitter; capped at 60s.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import signal
from dataclasses import dataclass
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .capabilities import detect as detect_capabilities
from .config import Credentials
from .fs_mirror import FsMirror
from .hydrate import fetch_manifest, hydrate
from .state_db import StateDB

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
        self._state = StateDB()
        self._mirror: FsMirror | None = None
        self._hydrate_lock = asyncio.Lock()
        self._hydrated_once = False

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

        if ftype == "ready":
            # First server frame after hello — kick off the initial
            # hydrate in the background so heartbeats keep flowing.
            asyncio.create_task(self._initial_hydrate())
            return
        if ftype in ("hello_ack", "heartbeat_ack", "ignored"):
            return
        if ftype == "error":
            log.error("server error: %s", frame.get("error"))
            return
        if ftype == "fs.push":
            await self._handle_fs_push(frame)
            return
        if ftype == "fs.delete":
            await self._handle_fs_delete(frame)
            return
        # Unknown frames are tolerated so future protocol versions
        # degrade gracefully against older daemons.
        log.debug("unknown frame type=%s — ignoring", ftype)

    # FS sync -------------------------------------------------------------
    async def _initial_hydrate(self) -> None:
        async with self._hydrate_lock:
            if self._hydrated_once:
                return
            try:
                written, folders, errors = await hydrate(
                    self.credentials, state=self._state
                )
                self._hydrated_once = True
                log.info(
                    "hydrate done: %d files, %d folders, %d errors",
                    written,
                    folders,
                    errors,
                )
            except Exception:
                log.exception("hydrate failed; will retry on next reconnect")

    async def _ensure_mirror(self) -> FsMirror | None:
        if self._mirror is not None:
            return self._mirror
        try:
            manifest = await fetch_manifest(self.credentials)
        except Exception:
            log.exception("manifest fetch failed in mirror init")
            return None
        org_slug = manifest.get("organization_slug")
        if not org_slug:
            return None
        self._mirror = FsMirror(org_slug=org_slug, state=self._state)
        return self._mirror

    async def _handle_fs_push(self, frame: dict[str, Any]) -> None:
        mirror = await self._ensure_mirror()
        if mirror is None:
            log.warning("fs.push received before mirror ready; ignoring")
            return
        kind = frame.get("kind", "file")
        scope = frame["scope"]
        dept = frame.get("department_slug")
        agent = frame.get("agent_slug")
        rel = frame["path"]

        if kind == "folder":
            try:
                mirror.make_folder(
                    scope=scope,
                    department_slug=dept,
                    agent_slug=agent,
                    rel_path=rel,
                )
            except Exception:
                log.exception("fs.push folder failed: %s", rel)
            return

        # Resolve content: inline base64 or presigned URL.
        content: bytes | None = None
        if frame.get("content_b64"):
            try:
                content = base64.b64decode(frame["content_b64"])
            except Exception:
                log.warning("fs.push %s: invalid base64", rel)
                return
        elif frame.get("presigned_url"):
            try:
                async with httpx.AsyncClient(timeout=120) as http:
                    resp = await http.get(frame["presigned_url"])
                    resp.raise_for_status()
                    content = resp.content
            except Exception:
                log.exception("fs.push %s: presigned download failed", rel)
                return
        else:
            log.debug("fs.push %s: no content provided", rel)
            return

        try:
            mirror.write_file(
                scope=scope,
                department_slug=dept,
                agent_slug=agent,
                rel_path=rel,
                content=content,
                content_hash=frame.get("content_hash"),
                origin="cloud",
            )
            log.info("fs.push applied: %s", rel)
        except Exception:
            log.exception("fs.push apply failed: %s", rel)

    async def _handle_fs_delete(self, frame: dict[str, Any]) -> None:
        mirror = await self._ensure_mirror()
        if mirror is None:
            return
        try:
            mirror.delete(
                scope=frame["scope"],
                department_slug=frame.get("department_slug"),
                agent_slug=frame.get("agent_slug"),
                rel_path=frame["path"],
            )
            log.info("fs.delete applied: %s", frame["path"])
        except Exception:
            log.exception("fs.delete failed: %s", frame.get("path"))

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
