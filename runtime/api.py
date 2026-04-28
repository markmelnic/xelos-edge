"""Thin HTTP client against the Xelos cloud API.

The daemon only hits one endpoint pre-pair (`POST /devices/pair`) and
then stays on the WebSocket. Any other REST calls go through here so
auth handling lives in one place.
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(f"{status} {message}")
        self.status = status
        self.message = message
        self.payload = payload


async def pair(
    *,
    api_base: str,
    code: str,
    fingerprint: str,
    capabilities: dict[str, Any],
    public_key: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Redeem a pair code → returns the pair result payload."""
    body = {
        "code": code,
        "fingerprint": fingerprint,
        "capabilities": capabilities,
    }
    if public_key:
        body["public_key"] = public_key

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{api_base.rstrip('/')}/devices/pair", json=body)
    if resp.status_code >= 400:
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text
        raise ApiError(resp.status_code, "pair failed", payload)
    return resp.json()
