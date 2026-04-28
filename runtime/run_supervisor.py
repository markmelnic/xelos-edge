"""Run supervisor — drives one Claude Code subprocess per agent run.

The supervisor accepts `job.start` frames from the cloud, spawns a
`ClaudeCodeProcess` rooted in the agent's mirror directory, and
forwards each translated `StepEvent` upstream as a `run.<type>` frame
the cloud's `agent_runner_device` knows how to ingest.

Concurrency cap defends the host against runaway delegation chains
that would otherwise spawn unbounded CC processes.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .cc_process import ClaudeCodeProcess, ClaudeNotFound, JobSpec, StepEvent
from .fs_mirror import org_root

log = logging.getLogger(__name__)


SendFn = Callable[[dict[str, Any]], Awaitable[None]]


class RunSupervisor:
    def __init__(
        self,
        *,
        send: SendFn,
        max_concurrent_runs: int = 4,
    ) -> None:
        self._send = send
        self._sem = asyncio.Semaphore(max_concurrent_runs)
        self._active: dict[str, ClaudeCodeProcess] = {}
        self._lock = asyncio.Lock()

    # Frame entry points -------------------------------------------------
    async def start(self, frame: dict[str, Any]) -> None:
        run_id = str(frame.get("run_id") or "")
        if not run_id:
            log.warning("job.start frame missing run_id")
            return
        asyncio.create_task(self._drive_run(run_id, frame))

    async def cancel(self, frame: dict[str, Any]) -> None:
        run_id = str(frame.get("run_id") or "")
        proc = self._active.get(run_id)
        if proc is None:
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

    # Run lifecycle ------------------------------------------------------
    async def _drive_run(self, run_id: str, frame: dict[str, Any]) -> None:
        async with self._sem:
            spec = self._build_spec(run_id, frame)
            if spec is None:
                await self._send_terminal(
                    run_id, kind="failed", error="invalid_job_spec"
                )
                return

            proc = ClaudeCodeProcess(spec)
            async with self._lock:
                self._active[run_id] = proc

            await self._send_event(
                run_id,
                StepEvent(
                    type="run.started",
                    data={
                        "agent_id": frame.get("agent", {}).get("id"),
                        "agent_name": frame.get("agent", {}).get("name"),
                        "executor": "device",
                        "working_directory": str(spec.working_directory),
                    },
                ),
            )

            try:
                async for ev in proc.stream():
                    await self._send_event(run_id, ev)
                # Stream ended cleanly. If proc never emitted `agent_done`,
                # synthesise a terminal so the cloud doesn't wait forever.
                exit_code = (
                    proc._proc.returncode  # noqa: SLF001
                    if proc._proc is not None
                    else None
                )
                if exit_code not in (0, None):
                    await self._send_terminal(
                        run_id,
                        kind="failed",
                        error=f"claude_exit_code_{exit_code}",
                        usage=proc.final_usage,
                        external_session_id=proc.session_id,
                    )
                else:
                    await self._send_terminal(
                        run_id,
                        kind="completed",
                        usage=proc.final_usage,
                        external_session_id=proc.session_id,
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
                async with self._lock:
                    self._active.pop(run_id, None)

    # Helpers ------------------------------------------------------------
    def _build_spec(
        self, run_id: str, frame: dict[str, Any]
    ) -> JobSpec | None:
        org_slug = frame.get("organization_slug")
        dept_slug = frame.get("department_slug")
        agent = frame.get("agent") or {}
        agent_slug = agent.get("slug")
        if not (org_slug and dept_slug and agent_slug):
            return None

        cwd = (
            org_root(org_slug)
            / "departments"
            / dept_slug
            / "agents"
            / agent_slug
        )
        max_turns = max(1, int(agent.get("max_steps") or 20))
        return JobSpec(
            run_id=run_id,
            working_directory=cwd,
            system_prompt=frame.get("system_prompt") or "",
            user_message=frame.get("user_message") or "",
            allowed_tools=list(frame.get("allowed_tools") or []),
            max_turns=max_turns,
        )

    async def _send_event(self, run_id: str, ev: StepEvent) -> None:
        """Wrap a StepEvent into the cloud's run.* frame envelope."""
        await self._send(
            {
                "type": f"run.{ev.type}" if not ev.type.startswith("run.") else ev.type,
                "run_id": run_id,
                "data": ev.data,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def _send_terminal(
        self,
        run_id: str,
        *,
        kind: str,
        error: str | None = None,
        trace: str | None = None,
        usage: dict[str, Any] | None = None,
        external_session_id: str | None = None,
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
        await self._send(frame)
