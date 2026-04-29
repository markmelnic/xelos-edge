"""Drive one Claude Code subprocess per agent run; bridges cloud tools via MCP."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .cc_process import ClaudeCodeProcess, ClaudeNotFound, JobSpec, StepEvent
from .config import _xelos_home
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

            files_modified: list[str] = []
            try:
                async for ev in proc.stream():
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
                    await self._send_terminal(
                        run_id,
                        kind="failed",
                        error=f"claude_exit_code_{exit_code}",
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
        mcp_config_path = self._write_mcp_config(run_id)

        # CC matches MCP tools via `mcp__<server>__<tool>`.
        cc_allowed = list(frame.get("allowed_tools") or [])
        for slug in frame.get("xelos_tools") or []:
            cc_allowed.append(f"mcp__xelos__{slug}")

        return JobSpec(
            run_id=run_id,
            working_directory=cwd,
            system_prompt=frame.get("system_prompt") or "",
            user_message=frame.get("user_message") or "",
            allowed_tools=cc_allowed,
            max_turns=max_turns,
            mcp_config_path=mcp_config_path,
        )

    def _write_mcp_config(self, run_id: str) -> Path | None:
        """One-off `~/.xelos/runs/<id>.mcp.json` pointing CC at xelos-mcp."""
        binary = shutil.which("xelos-mcp")
        if binary is None:
            log.warning(
                "xelos-mcp not on PATH; agent will run with native CC tools only"
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
