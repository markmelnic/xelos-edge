"""Drive one Claude Code subprocess per agent run; bridges cloud tools via MCP."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .cc_process import ClaudeCodeProcess, ClaudeNotFound, JobSpec, StepEvent
from .config import _xelos_home
from .fs_mirror import workspace_root
from .state_db import RunRecord, StateDB

log = logging.getLogger(__name__)


SendFn = Callable[[dict[str, Any]], Awaitable[None]]


class RunSupervisor:
    def __init__(
        self,
        *,
        send: SendFn,
        state: StateDB | None = None,
        max_concurrent_runs: int = 4,
    ) -> None:
        self._send = send
        self._state = state
        self._sem = asyncio.Semaphore(max_concurrent_runs)
        self._active: dict[str, ClaudeCodeProcess] = {}
        self._lock = asyncio.Lock()
        self._pending_cancel: set[str] = set()
        # Strong refs prevent GC of in-flight _drive_run tasks.
        self._drive_tasks: set[asyncio.Task] = set()

    async def start(self, frame: dict[str, Any]) -> None:
        run_id = str(frame.get("run_id") or "")
        if not run_id:
            log.warning("job.start frame missing run_id")
            return
        task = asyncio.create_task(self._drive_run(run_id, frame))
        self._drive_tasks.add(task)
        task.add_done_callback(self._drive_tasks.discard)

    async def cancel(self, frame: dict[str, Any]) -> None:
        run_id = str(frame.get("run_id") or "")
        async with self._lock:
            proc = self._active.get(run_id)
        if proc is None:
            # Run hasn't started CC yet (waiting on semaphore). Pre-cancel so
            # _drive_run aborts immediately after acquiring the slot instead of
            # starting a CC subprocess for a job the cloud already cancelled.
            self._pending_cancel.add(run_id)
            return
        try:
            await proc.cancel()
        except Exception:
            log.exception("cancel failed run=%s", run_id)

    async def cancel_all(self) -> None:
        async with self._lock:
            procs = list(self._active.values())
        for p in procs:
            try:
                await p.cancel()
            except Exception:  # pragma: no cover
                log.exception("cancel_all failed for one proc")

    async def _drive_run(self, run_id: str, frame: dict[str, Any]) -> None:
        async with self._sem:
            # A job.cancel frame may have arrived while we were queued behind
            # the semaphore. Abort early so we don't spin up CC for a dead job.
            if run_id in self._pending_cancel:
                self._pending_cancel.discard(run_id)
                log.info("run %s pre-cancelled before start; skipping CC", run_id)
                await self._send_terminal(
                    run_id, kind="failed", error="cancelled_before_start"
                )
                return

            spec = self._build_spec(run_id, frame)
            if spec is None:
                await self._send_terminal(
                    run_id, kind="failed", error="invalid_job_spec"
                )
                return

            proc = ClaudeCodeProcess(spec)
            async with self._lock:
                self._active[run_id] = proc

            agent = frame.get("agent") or {}
            self._persist_dispatch(run_id, frame, agent)

            await self._send_event(
                run_id,
                StepEvent(
                    type="run.started",
                    data={
                        "agent_id": agent.get("id"),
                        "agent_name": agent.get("name"),
                        "executor": "device",
                        "working_directory": str(spec.working_directory),
                    },
                ),
            )

            # Drive a periodic `run.heartbeat` while CC is working so the cloud's
            # device-quiet timer doesn't trip during long LLM calls or
            # rate-limit backoffs (CC can stall multiple minutes silently).
            hb_task = asyncio.create_task(self._heartbeat_loop(run_id))

            files_modified: list[str] = []
            try:
                async for ev in proc.stream():
                    self._persist_event(run_id, ev)
                    # Track file mutations so we can emit a summary step
                    # before terminal — gives the cloud audit timeline a
                    # canonical "what files changed" without parsing every
                    # tool_call.
                    if ev.type == "tool_call":
                        tool_name = (ev.data or {}).get("name")
                        if tool_name in ("Write", "Edit", "MultiEdit"):
                            args = (ev.data or {}).get("arguments") or {}
                            fp = args.get("file_path") or args.get("path")
                            if isinstance(fp, str) and fp not in files_modified:
                                files_modified.append(fp)
                    await self._send_event(run_id, ev)

                if files_modified:
                    await self._send_event(
                        run_id,
                        StepEvent(
                            type="files_modified",
                            data={"paths": files_modified},
                        ),
                    )

                # Synthesise a terminal frame if proc never emitted one.
                exit_code = (
                    proc._proc.returncode  # noqa: SLF001
                    if proc._proc is not None
                    else None
                )
                if exit_code not in (0, None):
                    # Surface stderr tail in the failure message so cloud
                    # + UI know *why* claude died — opaque exit codes are
                    # useless when debugging missing auth, bad MCP config,
                    # rate limits, etc.
                    stderr_tail = proc.stderr_tail.strip()
                    err = f"claude_exit_code_{exit_code}"
                    if stderr_tail:
                        # Trim the joined tail so the WS frame stays
                        # under the 8KB-ish soft cap that downstream log
                        # rendering assumes.
                        if len(stderr_tail) > 4000:
                            stderr_tail = stderr_tail[-4000:]
                        err = f"{err}: {stderr_tail}"
                    log.warning(
                        "claude exited %s for run %s. stderr tail:\n%s",
                        exit_code,
                        run_id,
                        stderr_tail or "(empty)",
                    )
                    await self._send_terminal(
                        run_id,
                        kind="failed",
                        error=err,
                        usage=proc.final_usage,
                        external_session_id=proc.session_id,
                        files_modified=files_modified,
                    )
                else:
                    await self._send_terminal(
                        run_id,
                        kind="completed",
                        usage=proc.final_usage,
                        external_session_id=proc.session_id,
                        files_modified=files_modified,
                    )
            except ClaudeNotFound as exc:
                await self._send_terminal(
                    run_id,
                    kind="failed",
                    error=str(exc),
                )
            except Exception as exc:
                log.exception("run %s crashed", run_id)
                await self._send_terminal(
                    run_id,
                    kind="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    trace=traceback.format_exc(),
                )
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except (asyncio.CancelledError, Exception):  # pragma: no cover
                    pass
                async with self._lock:
                    self._active.pop(run_id, None)
                if spec.mcp_config_path is not None:
                    try:
                        os.unlink(spec.mcp_config_path)
                    except OSError:
                        pass

    def _build_spec(
        self, run_id: str, frame: dict[str, Any]
    ) -> JobSpec | None:
        workspace_slug = frame.get("workspace_slug")
        dept_slug = frame.get("department_slug")
        agent = frame.get("agent") or {}
        agent_slug = agent.get("slug")
        if not (workspace_slug and dept_slug and agent_slug):
            return None

        # Layout matches `fs_mirror._resolve_target`: `{ws}/{dept}/{agent}`.
        cwd = workspace_root(workspace_slug) / dept_slug / agent_slug
        max_turns = max(1, int(agent.get("max_steps") or 20))
        mcp_config_path = self._write_mcp_config(run_id)

        # CC matches MCP tools via `mcp__<server>__<tool>`.
        cc_allowed = list(frame.get("allowed_tools") or [])
        for slug in frame.get("xelos_tools") or []:
            cc_allowed.append(f"mcp__xelos__{slug}")

        # Claude rejects an empty positional arg with `--print` so
        # always ship a non-empty user_message. Cloud should already do
        # this (`_build_user_message` synthesises a placeholder), but
        # belt-and-suspenders here keeps a malformed frame from
        # crashing the subprocess with the unhelpful "Input must be
        # provided…" stderr line.
        user_msg = (frame.get("user_message") or "").strip()
        if not user_msg:
            user_msg = "(no instruction provided — continue from prior context)"
            log.warning(
                "job.start frame for run %s carried empty user_message; "
                "substituting placeholder",
                run_id,
            )

        # Cloud passes the prior run's CC session id so we can `--resume`.
        # If absent (cold cloud, lost frame, etc.) fall back to the local
        # agent_session cache so multi-turn continuity survives a daemon
        # restart even when the cloud doesn't carry the id.
        resume_session_id = frame.get("resume_session_id")
        if not isinstance(resume_session_id, str) or not resume_session_id:
            resume_session_id = None
        if resume_session_id is None and self._state is not None:
            agent_id = (frame.get("agent") or {}).get("id")
            if agent_id:
                resume_session_id = self._state.get_agent_session(agent_id)
                if resume_session_id:
                    log.info(
                        "run %s: resuming agent session %s from local cache",
                        run_id,
                        resume_session_id,
                    )

        # Bash-enabled agents run in an automated daemon context — there is no
        # human to click "approve" on each command. Skip CC's interactive
        # approval prompts so git, node, npm, multi-operation commands, etc.
        # execute without error. The cc_bash_allowed gate on the API side is
        # still the user-consent boundary.
        extra_args: list[str] = []
        if "Bash" in cc_allowed:
            extra_args.append("--dangerously-skip-permissions")

        return JobSpec(
            run_id=run_id,
            working_directory=cwd,
            system_prompt=frame.get("system_prompt") or "",
            user_message=user_msg,
            allowed_tools=cc_allowed,
            max_turns=max_turns,
            mcp_config_path=mcp_config_path,
            resume_session_id=resume_session_id,
            extra_args=extra_args,
        )

    def _write_mcp_config(self, run_id: str) -> Path | None:
        """One-off `~/.xelos/runs/<id>.mcp.json` pointing CC at xelos-mcp.

        `xelos-mcp` ships from the same wheel as `xelos`, so it lives in
        the daemon's own venv `bin/`. Production installers expose a
        PATH shim for `xelos` but not always for `xelos-mcp`, so we
        check `shutil.which` first and fall back to the sibling of
        `sys.executable`. Without this fallback the Dispatcher loses
        every cloud tool (`create_department`, `delegate_to_agent`,
        etc.) and surfaces as "tools missing".
        """
        binary = shutil.which("xelos-mcp")
        if binary is None:
            sibling = Path(sys.executable).parent / "xelos-mcp"
            if sibling.exists() and os.access(sibling, os.X_OK):
                binary = str(sibling)
        if binary is None:
            log.warning(
                "xelos-mcp not found on PATH or alongside %s; "
                "agent will run with native CC tools only",
                sys.executable,
            )
            return None

        home = _xelos_home() / "runs"
        home.mkdir(parents=True, exist_ok=True)
        try:
            home.chmod(0o700)
        except OSError:
            pass

        cfg_path = home / f"{run_id}.mcp.json"
        cfg = {
            "mcpServers": {
                "xelos": {
                    "type": "stdio",
                    "command": binary,
                    "args": ["--run-id", run_id],
                    "env": {},
                }
            }
        }
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        try:
            cfg_path.chmod(0o600)
        except OSError:
            pass
        return cfg_path

    async def _send_event(self, run_id: str, ev: StepEvent) -> None:
        """Wrap StepEvent in the run.* frame envelope."""
        await self._send(
            {
                "type": f"run.{ev.type}" if not ev.type.startswith("run.") else ev.type,
                "run_id": run_id,
                "data": ev.data,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )

    # Cadence: under cloud's WARN window so quiet stretches reset the timer.
    HEARTBEAT_INTERVAL_SECONDS = 30

    async def _heartbeat_loop(self, run_id: str) -> None:
        """Tick `run.heartbeat` at HEARTBEAT_INTERVAL while CC is active."""
        try:
            while True:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL_SECONDS)
                await self._send(
                    {
                        "type": "run.heartbeat",
                        "run_id": run_id,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                )
        except asyncio.CancelledError:
            return
        except Exception:  # pragma: no cover
            log.exception("heartbeat loop crashed run=%s", run_id)

    async def _send_terminal(
        self,
        run_id: str,
        *,
        kind: str,
        error: str | None = None,
        trace: str | None = None,
        usage: dict[str, Any] | None = None,
        external_session_id: str | None = None,
        files_modified: list[str] | None = None,
    ) -> None:
        frame: dict[str, Any] = {
            "type": f"run.{kind}",
            "run_id": run_id,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if error is not None:
            frame["error"] = error
        if trace is not None:
            frame["trace"] = trace
        if usage:
            frame["usage"] = usage
        if external_session_id:
            frame["external_session_id"] = external_session_id
        if files_modified:
            frame["files_modified"] = files_modified
        await self._send(frame)
        self._persist_terminal(
            run_id,
            kind=kind,
            error=error,
            external_session_id=external_session_id,
            files_modified=files_modified or [],
        )

    # --- local persistence -------------------------------------------------

    def _persist_dispatch(
        self,
        run_id: str,
        frame: dict[str, Any],
        agent: dict[str, Any],
    ) -> None:
        if self._state is None:
            return
        try:
            now = time.time()
            self._state.upsert_run(
                RunRecord(
                    run_id=run_id,
                    agent_id=agent.get("id"),
                    agent_slug=agent.get("slug"),
                    agent_name=agent.get("name"),
                    department_slug=frame.get("department_slug"),
                    workspace_slug=frame.get("workspace_slug"),
                    status="running",
                    started_at=now,
                    completed_at=None,
                    last_event_at=now,
                    step_count=0,
                    error=None,
                    external_session_id=None,
                    files_modified=[],
                )
            )
        except Exception:  # pragma: no cover
            log.exception("persist dispatch failed run=%s", run_id)

    def _persist_event(self, run_id: str, ev: StepEvent) -> None:
        if self._state is None:
            return
        # Bumps the step count on substantive lifecycle frames so the TUI
        # post-restart can show non-zero progress.
        bump = ev.type in (
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "assistant_message",
        )
        try:
            self._state.update_run_status(
                run_id,
                last_event_at=time.time(),
                bump_step=bump,
            )
        except Exception:  # pragma: no cover
            pass

    def _persist_terminal(
        self,
        run_id: str,
        *,
        kind: str,
        error: str | None,
        external_session_id: str | None,
        files_modified: list[str],
    ) -> None:
        if self._state is None:
            return
        status_map = {
            "completed": "completed",
            "failed": "failed",
            "awaiting_approval": "awaiting_approval",
        }
        status = status_map.get(kind, kind)
        try:
            self._state.update_run_status(
                run_id,
                status=status,
                completed_at=time.time(),
                error=error,
                external_session_id=external_session_id,
                files_modified=files_modified,
            )
            # Refresh the agent's session-id cache so the next run can
            # `--resume` even if the cloud frame loses it.
            if external_session_id:
                rec = self._state.get_run(run_id)
                if rec and rec.agent_id:
                    self._state.update_agent_session(
                        agent_id=rec.agent_id,
                        session_id=external_session_id,
                        run_id=run_id,
                    )
        except Exception:  # pragma: no cover
            log.exception("persist terminal failed run=%s", run_id)
