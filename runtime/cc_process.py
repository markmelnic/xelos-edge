"""Claude Code subprocess wrapper.

Spawns `claude --print --output-format stream-json` non-interactively
and parses each line into typed events. The wrapper is intentionally
thin: it knows how to start, parse, and stop a CC session, and it
maps CC's stream-json shape into Xelos `StepEvent` shapes that the
cloud runner already understands.

Stream-json line shapes (from Anthropic's public docs):

    {"type":"system","subtype":"init","cwd":"...","tools":[...],"session_id":"..."}
    {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
    {"type":"assistant","message":{"content":[{"type":"tool_use","id":"...","name":"Read","input":{...}}]}}
    {"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"...","content":"..."}]}}
    {"type":"result","subtype":"success","total_cost_usd":0.012,"usage":{"input_tokens":1234,"output_tokens":456}}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)


@dataclass(slots=True)
class StepEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class JobSpec:
    run_id: str
    working_directory: Path
    system_prompt: str
    user_message: str
    allowed_tools: list[str]
    max_turns: int = 20
    extra_args: list[str] = field(default_factory=list)


class ClaudeNotFound(Exception):
    pass


class ClaudeCodeProcess:
    """Owns one `claude` subprocess for the lifetime of a single run."""

    def __init__(self, spec: JobSpec) -> None:
        self.spec = spec
        self._proc: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None
        self._final_usage: dict[str, Any] = {}
        self._final_cost: float | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def final_usage(self) -> dict[str, Any]:
        return self._final_usage

    @property
    def final_cost(self) -> float | None:
        return self._final_cost

    # Lifecycle -----------------------------------------------------------
    async def stream(self) -> AsyncIterator[StepEvent]:
        """Spawn CC and yield translated step events.

        On the final `result` event the process is allowed to exit; the
        caller is responsible for wrapping the result in a terminal
        `agent_done` / `failed` frame and notifying the cloud.
        """
        binary = shutil.which("claude")
        if binary is None:
            raise ClaudeNotFound(
                "`claude` CLI not found on PATH; install Claude Code first"
            )

        args = [
            binary,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(self.spec.max_turns),
            "--append-system-prompt",
            self.spec.system_prompt,
        ]
        if self.spec.allowed_tools:
            args += ["--allowedTools", ",".join(self.spec.allowed_tools)]
        args += self.spec.extra_args
        args.append(self.spec.user_message)

        cwd = str(self.spec.working_directory)
        os.makedirs(cwd, exist_ok=True)

        log.info(
            "spawning claude code: cwd=%s tools=%s max_turns=%d",
            cwd,
            self.spec.allowed_tools,
            self.spec.max_turns,
        )

        self._proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Drain stderr in the background so a noisy CC process can't
        # deadlock by filling the pipe buffer while we wait on stdout.
        stderr_task = asyncio.create_task(self._drain_stderr())

        assert self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("non-json line from claude: %s", line[:200])
                    continue
                async for translated in self._translate(event):
                    yield translated
        finally:
            await self._proc.wait()
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass

    async def cancel(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            self._proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            try:
                self._proc.kill()
            except ProcessLookupError:
                return
            await self._proc.wait()

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        async for raw in self._proc.stderr:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                log.debug("claude stderr: %s", text)

    # CC stream-json → Xelos StepEvent translator -------------------------
    async def _translate(
        self, event: dict[str, Any]
    ) -> AsyncIterator[StepEvent]:
        etype = event.get("type")

        if etype == "system" and event.get("subtype") == "init":
            self._session_id = event.get("session_id")
            yield StepEvent(
                type="thinking",
                data={
                    "session_id": self._session_id,
                    "tools": event.get("tools", []),
                    "cwd": event.get("cwd"),
                },
            )
            return

        if etype == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    if text.strip():
                        yield StepEvent(
                            type="assistant_message",
                            data={"content": text},
                        )
                elif btype == "tool_use":
                    yield StepEvent(
                        type="tool_call",
                        data={
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "arguments": block.get("input") or {},
                        },
                    )
            return

        if etype == "user":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") == "tool_result":
                    yield StepEvent(
                        type="tool_result",
                        data={
                            "id": block.get("tool_use_id"),
                            "is_error": bool(block.get("is_error", False)),
                            "output": block.get("content"),
                        },
                    )
            return

        if etype == "result":
            usage = event.get("usage") or {}
            self._final_usage = {
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or 0),
            }
            cost = event.get("total_cost_usd")
            if cost is not None:
                try:
                    self._final_cost = float(cost)
                    self._final_usage["cost_usd"] = self._final_cost
                except (TypeError, ValueError):
                    pass

            subtype = event.get("subtype")
            if subtype == "success":
                yield StepEvent(
                    type="agent_done",
                    data={
                        "content": event.get("result"),
                        "tokens": self._final_usage.get("prompt_tokens", 0)
                        + self._final_usage.get("completion_tokens", 0),
                        "cost_usd": self._final_cost,
                    },
                )
            else:
                yield StepEvent(
                    type="agent_done",
                    data={
                        "warning": subtype or "result_non_success",
                        "tokens": self._final_usage.get("prompt_tokens", 0)
                        + self._final_usage.get("completion_tokens", 0),
                    },
                )
            return

        # Unknown CC events are forwarded verbatim under a debug type so
        # nothing is silently dropped while we iterate on the parser.
        yield StepEvent(type="cc_raw", data={"event": event})
