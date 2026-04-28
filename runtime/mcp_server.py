"""xelos-mcp — local stdio MCP server proxying Xelos cloud tools.

Spawned by Claude Code via `--mcp-config` once per agent run. Reads
the daemon's stored credentials, then forwards every `tools/list` and
`tools/call` MCP request to the cloud via HTTPS using the device's
long-lived bearer token.

The cloud authorises each call against the run's agent allowlist and
dispatches it through the same `TOOL_REGISTRY` cloud agents use, so
behaviour is identical regardless of where the LLM session is running.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

import httpx
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .config import Credentials

log = logging.getLogger("xelos_mcp")


def _setup_logging() -> None:
    # Logs go to stderr — stdout is reserved for MCP JSON-RPC.
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("XELOS_MCP_LOG", "INFO"),
        format="%(asctime)s %(levelname)s xelos-mcp :: %(message)s",
    )


def _to_text(payload: Any) -> str:
    """Coerce a tool result into a plain-text MCP content block.

    Tool handlers return arbitrary JSON-serialisable structures; CC
    expects each block to be string content. We pretty-print structs
    so the model gets human-readable output.
    """
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    except Exception:
        return str(payload)


def build_server(*, run_id: str, creds: Credentials) -> Server:
    """Instantiate an MCP Server pre-wired to a specific run + creds."""
    server: Server = Server(
        name="xelos",
        version="0.1.0",
        instructions=(
            "Bridge to the Xelos cloud. Tools listed here let you delegate "
            "to other agents, read shared/department files, escalate to a "
            "council, query data sources, persist memory, and more."
        ),
    )

    base_url = creds.api_base.rstrip("/")
    headers = {"Authorization": f"Bearer {creds.token}"}

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{base_url}/devices/me/tools",
                params={"run_id": run_id},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        out: list[Tool] = []
        for entry in data.get("tools", []):
            try:
                out.append(
                    Tool(
                        name=entry["name"],
                        description=entry.get("description") or "",
                        inputSchema=entry.get("inputSchema") or {"type": "object"},
                    )
                )
            except Exception:
                log.exception("malformed tool entry: %s", entry)
        return out

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        body = {
            "run_id": run_id,
            "tool": name,
            "arguments": arguments or {},
        }
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{base_url}/devices/me/tools/call",
                    json=body,
                    headers=headers,
                )
        except Exception as exc:
            log.exception("tool transport failed: %s", name)
            return [
                TextContent(
                    type="text",
                    text=f"transport error calling {name}: {exc}",
                )
            ]

        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except Exception:
                detail = resp.text
            return [
                TextContent(
                    type="text",
                    text=f"tool error ({resp.status_code}): {detail}",
                )
            ]

        try:
            payload = resp.json()
        except ValueError:
            return [TextContent(type="text", text=resp.text)]

        text = _to_text(payload.get("output"))
        if payload.get("is_error"):
            text = f"[tool reported error]\n{text}"
        return [TextContent(type="text", text=text)]

    return server


async def _run(server: Server) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="xelos",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main(argv: list[str] | None = None) -> int:
    """CLI entry — spawned by Claude Code via `--mcp-config`."""
    _setup_logging()

    parser = argparse.ArgumentParser(prog="xelos-mcp")
    parser.add_argument(
        "--run-id",
        required=True,
        help="UUID of the Xelos Run this MCP session is bound to.",
    )
    args = parser.parse_args(argv)

    creds = Credentials.load()
    if creds is None:
        log.error("no credentials at ~/.xelos/credentials — refusing to start")
        return 2

    server = build_server(run_id=args.run_id, creds=creds)
    try:
        asyncio.run(_run(server))
    except KeyboardInterrupt:
        return 0
    except Exception:
        log.exception("xelos-mcp crashed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
