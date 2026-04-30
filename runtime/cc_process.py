"""Spawn `claude --output-format stream-json`, translate to Xelos StepEvents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections import deque
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
    mcp_config_path: Path | None = None
    extra_args: list[str] = field(default_factory=list)
    # Claude Code session id from the agent's prior run on this device.
    # When set we shell out with `claude --resume <id>` so the dispatcher
    # remembers the previous turn instead of treating each chat as a
    # cold start.
    resume_session_id: str | None = None


class ClaudeNotFound(Exception):
    pass


class ClaudeCodeProcess:
    """One `claude` subprocess per Run."""

    def __init__(self, spec: JobSpec) -> None:
        self.spec = spec
        self._proc: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None
        self._final_usage: dict[str, Any] = {}
        self._final_cost: float | None = None
        # Bounded stderr buffer — surfaced in the terminal frame on a
        # non-zero exit so cloud + UI see *why* claude died, not just
        # `exit_code_1`. 80 lines covers any reasonable traceback while
        # capping the WS frame size.
        self._stderr_tail: deque[str] = deque(maxlen=80)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def final_usage(self) -> dict[str, Any]:
        return self._final_usage

    @property
    def final_cost(self) -> float | None:
        return self._final_cost

    async def stream(self) -> AsyncIterator[StepEvent]:
        """Spawn + yield translated events. Caller wraps terminal frame."""
        binary = shutil.which("claude")
        if binary is None:
            raise ClaudeNotFound(
                "`claude` CLI not found on PATH; install Claude Code first"
            )

        # Hard-fail before spawn if there's nothing to send. Lets the
        # caller produce a precise terminal frame (`empty_user_message`)
        # instead of relying on claude's opaque "Input must be provided"
        # stderr line.
        user_msg = (self.spec.user_message or "").strip()
        if not user_msg:
            raise ValueError("empty_user_message")

        # Use the `--flag=value` form throughout for every option that
        # takes a value. Claude Code's CLI parser is order-sensitive in
        # subtle ways; observed failure mode is the trailing positional
        # being eaten as the value of the *previous* flag (e.g.
        # `--mcp-config <path> <user_msg>` resolves user_msg as the
        # config path). The `=` form is unambiguous: each token is
        # self-contained, the trailing token is unambiguously the
        # positional prompt.
        sys_prompt = (self.spec.system_prompt or "").strip()
        args: list[str] = [
            binary,
            "--print",
            "--output-format=stream-json",
            "--verbose",
            f"--max-turns={self.spec.max_turns}",
        ]
        if sys_prompt:
            args.append(f"--append-system-prompt={sys_prompt}")
        if self.spec.allowed_tools:
            args.append(f"--allowedTools={','.join(self.spec.allowed_tools)}")
        if self.spec.mcp_config_path is not None:
            args.append(f"--mcp-config={self.spec.mcp_config_path}")
        if self.spec.resume_session_id:
            # `--resume <id>` rehydrates the prior CC session (full
            # conversation history + tool results) from the local
            # `~/.claude/projects/...` store. No-op if claude can't find
            # the session — claude logs a warning and starts fresh.
            args.append(f"--resume={self.spec.resume_session_id}")
        args += self.spec.extra_args
        # Positional prompt last — `--print` parses everything else as
        # flags up to this point.
        args.append(user_msg)

        cwd = str(self.spec.working_directory)
        os.makedirs(cwd, exist_ok=True)

        # Truncate the user message in the spawn log so a 30KB prompt
        # doesn't smother the daemon log. Full content remains in the
        # cloud-side trigger_payload.
        log.info(
            "spawning claude code: cwd=%s tools=%s max_turns=%d "
            "user_msg_chars=%d sys_prompt_chars=%d mcp=%s",
            cwd,
            self.spec.allowed_tools,
            self.spec.max_turns,
            len(user_msg),
            len(sys_prompt),
            self.spec.mcp_config_path,
        )
        # Argv shown at INFO so a misparse like the `--mcp-config` /
        # positional collision is one log scroll away. System prompt
        # collapsed to length to keep the line short.
        redacted = [
            (
                f"--append-system-prompt=<{len(sys_prompt)} chars>"
                if a.startswith("--append-system-prompt=")
                else a
            )
            for a in args
        ]
        log.info("claude argv: %r", redacted)

        self._proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Write the same prompt to stdin and close it. Done after spawn
        # but before reading stdout so claude has the input by the time
        # it starts producing the stream-json output.
        if self._proc.stdin is not None:
            try:
                self._proc.stdin.write(user_msg.encode("utf-8"))
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                # Subprocess died before we finished writing — nothing
                # we can do; fall through and let stdout/wait surface
                # the exit code with stderr context.
                log.warning("stdin write to claude failed: %s", exc)
            finally:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass

        # Drain stderr concurrently — a full pipe buffer would deadlock stdout.
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
            if not text:
                continue
            # Always emit at INFO so file/journal logs include the line —
            # `--verbose` daemon flag bumps the parent logger anyway, but
            # plain users still need to see why claude died. Buffer for
            # the terminal frame too.
            log.info("claude stderr: %s", text)
            self._stderr_tail.append(text)

    @property
    def stderr_tail(self) -> str:
        """Last N lines of claude stderr, newline-joined. Empty when
        the subprocess exited cleanly with nothing on stderr."""
        return "\n".join(self._stderr_tail)

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

        # Forward unknown events verbatim — nothing silently dropped.
        yield StepEvent(type="cc_raw", data={"event": event})
