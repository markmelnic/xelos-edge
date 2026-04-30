"""WS daemon — frame router for fs.*, run.*, job.* and friends.

Reconnect: exponential backoff with full jitter, capped at 60s.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import shutil
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .capabilities import detect as detect_capabilities
from .config import Credentials
from .events import EVENTS
from .fingerprint import short_host
from .fs_mirror import FsMirror
from .hydrate import fetch_manifest
from .path_resolver import resolve as resolve_path
from .reconcile import reconcile_with_cloud
from .run_supervisor import RunSupervisor
from .state_db import FileState, StateDB
from .watcher import FsEvent, FsWatcher

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 10
RECONNECT_INITIAL_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 60.0

# Files larger than this take the presigned-PUT path so we don't blow
# WS frame budgets. The cloud's `INLINE_PUSH_BYTES_MAX` is the same
# threshold for the cloud→device direction.
INLINE_PUSH_BYTES_MAX = 768 * 1024


@dataclass(slots=True)
class DaemonOptions:
    log_frames: bool = False
    # When false, the host (e.g. the Textual TUI) owns SIGINT/SIGTERM.
    install_signal_handlers: bool = True


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
        # Devices span multiple workspaces — one FsMirror per workspace_slug,
        # rooted at `~/.xelos/mirror/<workspace_slug>/`. The watcher is shared
        # across them all (rooted at `~/.xelos/mirror`).
        self._mirrors: dict[str, FsMirror] = {}
        self._watcher: FsWatcher | None = None
        self._runs: RunSupervisor | None = None
        self._hydrate_lock = asyncio.Lock()
        self._hydrated_once = False
        self._known_orgs: list[dict[str, Any]] = []

    async def run(self) -> None:
        if self.options.install_signal_handlers:
            self._install_signal_handlers()
        EVENTS.publish(
            "daemon.started",
            device_id=str(self.credentials.device_id),
            user_id=str(self.credentials.user_id),
            api_base=self.credentials.api_base,
        )

        backoff = RECONNECT_INITIAL_SECONDS
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = RECONNECT_INITIAL_SECONDS
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                log.warning("ws disconnected: %s", exc)
                EVENTS.publish("ws.disconnected", reason=str(exc))
                # Permanent close: bad/expired token (1008) or device
                # revoked / superseded (4003 / 4000). Reconnecting just
                # spins the loop forever — surface the failure instead so
                # the user can re-pair.
                code = getattr(exc, "code", None) or getattr(
                    getattr(exc, "rcvd", None), "code", None
                )
                if code in (1008, 4000, 4003):
                    log.error(
                        "credentials rejected by cloud (close code=%s). "
                        "Stopping daemon — re-run `xelos pair <code>`.",
                        code,
                    )
                    EVENTS.publish(
                        "ws.auth_rejected", code=code, reason=str(exc)
                    )
                    break
            except InvalidStatus as exc:
                # Cloud refused the WS upgrade. 401/403 → bad token.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                log.warning("ws upgrade rejected: %s (status=%s)", exc, status)
                EVENTS.publish(
                    "ws.disconnected", reason=f"upgrade:{status or 'unknown'}"
                )
                if status in (401, 403):
                    log.error(
                        "credentials rejected by cloud (HTTP %s). "
                        "Stopping daemon — re-run `xelos pair <code>`.",
                        status,
                    )
                    EVENTS.publish("ws.auth_rejected", code=status)
                    break
            except OSError as exc:
                log.warning("ws disconnected: %s", exc)
                EVENTS.publish("ws.disconnected", reason=str(exc))
            except Exception as exc:
                log.exception("ws session crashed")
                EVENTS.publish("ws.disconnected", reason=f"crash:{type(exc).__name__}")

            if self._stop.is_set():
                break

            # Full jitter — avoids reconnect storms after a server restart.
            sleep_for = random.uniform(0, backoff)
            log.info("reconnecting in %.1fs", sleep_for)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

        log.info("daemon stopped")

    async def _session(self) -> None:
        url = self._authed_ws_url()
        log.info("connecting to %s", _redact_token(url))
        EVENTS.publish("ws.connecting", url=_redact_token(url))
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
            EVENTS.publish("ws.connected", url=_redact_token(url))

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
            # Reconcile runs on every connect; kick off in the background.
            asyncio.create_task(self._on_ready())
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
        if ftype == "fs.ack":
            if self.options.log_frames:
                log.debug("fs.ack %s %s", frame.get("status"), frame.get("path"))
            return
        if ftype == "fs.conflict":
            await self._handle_fs_conflict(frame)
            return
        if ftype == "job.start":
            EVENTS.publish(
                "run.dispatch",
                run_id=frame.get("run_id"),
                agent_name=(frame.get("agent") or {}).get("name"),
                agent_slug=(frame.get("agent") or {}).get("slug"),
                department_slug=frame.get("department_slug"),
                installed_skills=frame.get("installed_skills") or [],
                installed_plugins=frame.get("installed_plugins") or [],
            )
            supervisor = await self._ensure_supervisor()
            if supervisor is not None:
                await supervisor.start(frame)
            return
        if ftype == "job.cancel":
            if self._runs is not None:
                await self._runs.cancel(frame)
            return
        log.debug("unknown frame type=%s — ignoring", ftype)

    async def _on_ready(self) -> None:
        """Reconcile every connect; runs once per known org workspace."""
        async with self._hydrate_lock:
            primer = await self._ensure_mirror()
            if primer is None:
                log.warning("reconcile: mirror init failed; skipping")
                return

            ws = self._ws
            if ws is None:
                log.warning("reconcile: ws gone before run; skipping")
                return

            async def _send_via_ws(frame: dict[str, Any]) -> None:
                await self._send(ws, frame)

            def _suppress(path: str) -> None:
                if self._watcher is not None:
                    self._watcher.suppress(path)

            # Run reconcile once per org we know about; the manifest is
            # multi-org but each mirror filters to its own slug.
            mirrors_snapshot = list(self._mirrors.values())
            for mirror in mirrors_snapshot:
                try:
                    summary = await reconcile_with_cloud(
                        credentials=self.credentials,
                        mirror=mirror,
                        state=self._state,
                        send=_send_via_ws,
                        suppress=_suppress,
                    )
                    self._hydrated_once = True
                    log.info(
                        "reconcile complete (%s): pulled=%d pushed=%d "
                        "deleted_local=%d deleted_remote=%d "
                        "conflicts=%d errors=%d in_sync=%d",
                        mirror.workspace_slug,
                        summary.pulled,
                        summary.pushed + summary.pushed_new,
                        summary.deleted_local,
                        summary.deleted_remote,
                        summary.conflicts,
                        summary.errors,
                        summary.in_sync,
                    )
                    EVENTS.publish(
                        "reconcile.completed",
                        workspace_slug=mirror.workspace_slug,
                        pulled=summary.pulled,
                        pushed=summary.pushed + summary.pushed_new,
                        deleted_local=summary.deleted_local,
                        deleted_remote=summary.deleted_remote,
                        conflicts=summary.conflicts,
                        errors=summary.errors,
                        in_sync=summary.in_sync,
                    )
                except Exception as exc:
                    log.exception(
                        "reconcile failed for %s", mirror.workspace_slug
                    )
                    EVENTS.publish(
                        "reconcile.failed",
                        workspace_slug=mirror.workspace_slug,
                        error=str(exc),
                    )

        await self._ensure_watcher()

    def _mirror_for_path(self, abs_path: Any) -> FsMirror | None:
        """Resolve an absolute path back to its owning org mirror.

        Layout is `~/.xelos/mirror/<workspace_slug>/...`. Walks every active
        mirror and returns whichever one contains `abs_path`.
        """
        from pathlib import Path

        try:
            p = Path(abs_path).resolve()
        except OSError:
            return None
        for mirror in self._mirrors.values():
            try:
                p.relative_to(mirror.root.resolve())
            except ValueError:
                continue
            return mirror
        return None

    async def _ensure_mirror(
        self, workspace_slug: str | None = None
    ) -> FsMirror | None:
        """Get or lazily create the FsMirror for `workspace_slug`.

        When called without `workspace_slug`, fetches the manifest, primes one
        mirror per org listed there, and returns the first one (legacy
        callers expect a single mirror back).
        """
        if workspace_slug:
            mirror = self._mirrors.get(workspace_slug)
            if mirror is None:
                mirror = FsMirror(workspace_slug=workspace_slug, state=self._state)
                self._mirrors[workspace_slug] = mirror
            return mirror

        try:
            manifest = await fetch_manifest(self.credentials)
        except Exception:
            log.exception("manifest fetch failed in mirror init")
            return None
        orgs = manifest.get("workspaces") or []
        if not orgs:
            # Legacy single-org manifest payload.
            single = manifest.get("workspace_slug")
            if single:
                orgs = [{"slug": single}]
        if not orgs:
            return None
        self._known_orgs = orgs
        first: FsMirror | None = None
        for o in orgs:
            slug = o.get("slug")
            if not isinstance(slug, str) or not slug:
                continue
            m = self._mirrors.get(slug)
            if m is None:
                m = FsMirror(workspace_slug=slug, state=self._state)
                self._mirrors[slug] = m
            if first is None:
                first = m
        return first

    async def _handle_fs_push(self, frame: dict[str, Any]) -> None:
        workspace_slug = frame.get("workspace_slug")
        if not isinstance(workspace_slug, str) or not workspace_slug:
            log.warning("fs.push missing workspace_slug; ignoring")
            return
        mirror = await self._ensure_mirror(workspace_slug)
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
            outcome = mirror.write_file(
                scope=scope,
                department_slug=dept,
                agent_slug=agent,
                rel_path=rel,
                content=content,
                content_hash=frame.get("content_hash"),
                origin="cloud",
            )
            if self._watcher is not None:
                self._watcher.suppress(str(outcome.abs_path))
            log.info("fs.push applied: %s", rel)
            EVENTS.publish(
                "fs.pulled",
                path=rel,
                size=len(content),
                scope=scope,
                department_slug=dept,
                agent_slug=agent,
            )
        except Exception:
            log.exception("fs.push apply failed: %s", rel)

    async def _handle_fs_delete(self, frame: dict[str, Any]) -> None:
        workspace_slug = frame.get("workspace_slug")
        if not isinstance(workspace_slug, str) or not workspace_slug:
            return
        mirror = await self._ensure_mirror(workspace_slug)
        if mirror is None:
            return
        try:
            target = mirror.delete(
                scope=frame["scope"],
                department_slug=frame.get("department_slug"),
                agent_slug=frame.get("agent_slug"),
                rel_path=frame["path"],
            )
            if target is not None and self._watcher is not None:
                self._watcher.suppress(str(target))
            log.info("fs.delete applied: %s", frame["path"])
            EVENTS.publish(
                "fs.deleted_local",
                path=frame.get("path"),
                scope=frame.get("scope"),
                department_slug=frame.get("department_slug"),
                agent_slug=frame.get("agent_slug"),
            )
        except Exception:
            log.exception("fs.delete failed: %s", frame.get("path"))

    async def _ensure_supervisor(self) -> RunSupervisor | None:
        if self._runs is not None:
            return self._runs
        ws = self._ws
        if ws is None:
            return None
        caps = detect_capabilities()
        cap = max(1, int(caps.get("max_concurrent_runs") or 4))

        TERMINAL_FRAME_TYPES = {"run.completed", "run.failed", "run.awaiting_approval"}

        async def _send(frame: dict[str, Any]) -> None:
            ftype = frame.get("type", "")
            # Before reporting the run terminal, flush any pending watcher
            # writes so CC's last file mutations land in the cloud BEFORE
            # the cloud finalises the run record. Otherwise a delegated
            # follow-up agent could read stale files.
            if ftype in TERMINAL_FRAME_TYPES and self._watcher is not None:
                try:
                    await self._watcher.flush()
                except Exception:
                    log.exception("watcher flush failed before terminal frame")

            # Mirror run lifecycle to the local event bus so the TUI can
            # render an "active runs" dashboard without round-tripping the
            # cloud.
            if ftype.startswith("run."):
                kind = ftype[len("run."):]
                EVENTS.publish(
                    "run." + kind,
                    run_id=frame.get("run_id"),
                    data=frame.get("data") or {},
                    error=frame.get("error"),
                    files_modified=frame.get("files_modified") or [],
                )

            current_ws = self._ws
            if current_ws is None:
                # Cloud times the run out on its side.
                log.debug("supervisor send dropped (ws closed): %s", frame.get("type"))
                return
            try:
                await self._send(current_ws, frame)
            except (ConnectionClosed, OSError) as exc:
                # Disconnect raced our send; reconnect+reconcile picks up the
                # diff. We don't want this to crash the run-supervising
                # coroutine — the cloud will time the run out and the
                # supervisor stays available for the next session.
                log.warning(
                    "supervisor send raced disconnect (%s): %s",
                    type(exc).__name__,
                    frame.get("type"),
                )

        self._runs = RunSupervisor(send=_send, max_concurrent_runs=cap)
        return self._runs

    async def _ensure_watcher(self) -> None:
        if self._watcher is not None:
            return
        # Multi-org devices: watch the umbrella `~/.xelos/mirror` root,
        # not just one org subdir. `_on_local_change` derives the org
        # from the changed path's first segment.
        mirror = await self._ensure_mirror()
        if mirror is None:
            return
        # mirror.root → `~/.xelos/mirror/<workspace_slug>`; one level up is
        # the multi-org root.
        from pathlib import Path

        watcher_root = Path(mirror.root).parent
        watcher = FsWatcher(root=watcher_root, on_event=self._on_local_change)
        await watcher.start()
        self._watcher = watcher

    async def _on_local_change(self, event: FsEvent) -> None:
        # Derive the changed file's org from its first path segment
        # under `~/.xelos/mirror/<workspace_slug>/...`. Multi-org devices have
        # any number of mirrors; pick the matching one.
        mirror = self._mirror_for_path(event.abs_path)
        if mirror is None:
            return

        resolved = resolve_path(root=mirror.root, abs_path=event.abs_path)
        if resolved is None:
            return

        ws = self._ws
        if ws is None:
            # Reconcile-on-reconnect picks up the diff.
            return

        if event.deleted or not event.abs_path.exists():
            # Clear state first — a follow-up write must not look synced.
            self._state.delete(str(event.abs_path))
            await self._send(
                ws,
                {
                    "type": "fs.delete",
                    "request_id": str(uuid.uuid4()),
                    "workspace_slug": mirror.workspace_slug,
                    "scope": resolved.scope,
                    "department_slug": resolved.department_slug,
                    "agent_slug": resolved.agent_slug,
                    "path": resolved.rel_path,
                },
            )
            EVENTS.publish(
                "fs.deleted_remote",
                path=resolved.rel_path,
                scope=resolved.scope,
                department_slug=resolved.department_slug,
                agent_slug=resolved.agent_slug,
            )
            return

        try:
            content = event.abs_path.read_bytes()
        except OSError:
            log.warning("could not read %s for upstream push", event.abs_path)
            return

        actual_hash = hashlib.sha256(content).hexdigest()
        prev = self._state.get(str(event.abs_path))
        if prev is not None and prev.content_hash == actual_hash:
            return

        parent_hash = prev.content_hash if prev is not None else None
        mime_type = _guess_mime(event.abs_path)

        # Pick transport: inline base64 stays under WS frame budgets,
        # large files PUT to S3 directly via a presigned URL.
        try:
            if len(content) <= INLINE_PUSH_BYTES_MAX:
                frame: dict[str, Any] = {
                    "type": "fs.write",
                    "request_id": str(uuid.uuid4()),
                    "workspace_slug": mirror.workspace_slug,
                    "scope": resolved.scope,
                    "department_slug": resolved.department_slug,
                    "agent_slug": resolved.agent_slug,
                    "path": resolved.rel_path,
                    "content_b64": base64.b64encode(content).decode("ascii"),
                    "content_hash": actual_hash,
                    "parent_hash": parent_hash,
                    "size": len(content),
                    "mime_type": mime_type,
                }
            else:
                upload = await self._presigned_put(
                    workspace_slug=mirror.workspace_slug,
                    scope=resolved.scope,
                    department_slug=resolved.department_slug,
                    agent_slug=resolved.agent_slug,
                    rel_path=resolved.rel_path,
                    mime_type=mime_type,
                )
                if upload is None:
                    log.warning(
                        "skip large file %s — presign failed",
                        event.abs_path,
                    )
                    return
                ok = await self._put_to_s3(
                    upload["presigned_url"], content, mime_type
                )
                if not ok:
                    return
                frame = {
                    "type": "fs.write",
                    "request_id": str(uuid.uuid4()),
                    "workspace_slug": mirror.workspace_slug,
                    "scope": resolved.scope,
                    "department_slug": resolved.department_slug,
                    "agent_slug": resolved.agent_slug,
                    "path": resolved.rel_path,
                    "s3_key": upload["s3_key"],
                    "content_hash": actual_hash,
                    "parent_hash": parent_hash,
                    "size": len(content),
                    "mime_type": mime_type,
                }
            await self._send(ws, frame)
        except ConnectionClosed:
            return
        EVENTS.publish(
            "fs.pushed",
            path=resolved.rel_path,
            size=len(content),
            scope=resolved.scope,
            department_slug=resolved.department_slug,
            agent_slug=resolved.agent_slug,
        )

        st = event.abs_path.stat()
        self._state.upsert(
            FileState(
                abs_path=str(event.abs_path),
                rel_path=resolved.rel_path,
                scope=resolved.scope,
                department_slug=resolved.department_slug,
                agent_slug=resolved.agent_slug,
                content_hash=actual_hash,
                size=st.st_size,
                mtime=st.st_mtime,
                last_synced_at=time.time(),
                origin="local",
            )
        )

    async def _presigned_put(
        self,
        *,
        workspace_slug: str,
        scope: str,
        department_slug: str | None,
        agent_slug: str | None,
        rel_path: str,
        mime_type: str,
    ) -> dict[str, Any] | None:
        body = {
            "workspace_slug": workspace_slug,
            "scope": scope,
            "department_slug": department_slug,
            "agent_slug": agent_slug,
            "path": rel_path,
            "mime_type": mime_type,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                resp = await http.post(
                    f"{self.credentials.api_base}/devices/me/fs/upload-url",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self.credentials.token}"
                    },
                )
        except Exception:
            log.exception("upload-url request failed")
            return None
        if resp.status_code >= 400:
            log.warning(
                "upload-url declined (%s): %s",
                resp.status_code,
                resp.text[:200],
            )
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    async def _put_to_s3(
        self, url: str, content: bytes, mime_type: str
    ) -> bool:
        try:
            async with httpx.AsyncClient(timeout=300) as http:
                resp = await http.put(
                    url,
                    content=content,
                    headers={"Content-Type": mime_type},
                )
            if resp.status_code >= 400:
                log.warning(
                    "S3 PUT failed (%s): %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
            return True
        except Exception:
            log.exception("S3 PUT crashed")
            return False

    async def _handle_fs_conflict(self, frame: dict[str, Any]) -> None:
        """Cloud diverged. Stash local copy; cloud version wins (LWW)."""
        workspace_slug = frame.get("workspace_slug")
        if not isinstance(workspace_slug, str) or not workspace_slug:
            return
        mirror = await self._ensure_mirror(workspace_slug)
        if mirror is None:
            return
        rel = frame.get("path")
        if not isinstance(rel, str):
            return
        try:
            from .fs_mirror import _resolve_target

            target = _resolve_target(
                workspace_slug=mirror.workspace_slug,
                scope=frame["scope"],
                department_slug=frame.get("department_slug"),
                agent_slug=frame.get("agent_slug"),
                rel_path=rel,
            )
        except Exception:
            log.exception("conflict resolve failed: %s", rel)
            return

        if target.exists():
            stash = target.with_name(
                f"{target.name}.{short_host()}.conflict.{int(time.time())}"
            )
            try:
                shutil.copy2(target, stash)
                log.warning(
                    "fs.conflict on %s — local stashed as %s", rel, stash
                )
            except Exception:
                log.exception("conflict stash failed: %s", rel)

        # Drop state so the next fs.push overwrites cleanly.
        self._state.delete(str(target))

    async def _send(self, ws: Any, frame: dict[str, Any]) -> None:
        if self.options.log_frames:
            log.debug("send %s", frame.get("type"))
        await ws.send(json.dumps(frame, default=str))

    def _authed_ws_url(self) -> str:
        # Derive the WS URL from `api_base` rather than trusting the
        # server-provided `websocket_url`. The cloud builds that field
        # from its own `API_BASE_URL` env, which on a local dev backend
        # is often left unset → the daemon ends up dialing production
        # forever despite `api_base` pointing at localhost. Rebuilding
        # locally keeps the two values in lockstep with whatever the
        # operator paired against.
        api_base = (self.credentials.api_base or "").rstrip("/")
        if api_base.startswith("https://"):
            ws_base = "wss://" + api_base[len("https://"):]
        elif api_base.startswith("http://"):
            ws_base = "ws://" + api_base[len("http://"):]
        else:
            # Already a ws(s) scheme or something unparseable — fall
            # back to whatever the credentials file shipped.
            ws_base = self.credentials.websocket_url.rsplit(
                "/devices/", 1
            )[0]
        url = f"{ws_base}/devices/{self.credentials.device_id}/ws"
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}token={self.credentials.token}"

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass  # Windows / non-main-thread.


def _redact_token(url: str) -> str:
    if "token=" not in url:
        return url
    head, _, tail = url.partition("token=")
    if "&" in tail:
        return f"{head}token=<redacted>&{tail.split('&', 1)[1]}"
    return f"{head}token=<redacted>"


_MIME_BY_SUFFIX = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".html": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _guess_mime(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


