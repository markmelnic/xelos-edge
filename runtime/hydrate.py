"""Manifest fetcher. Bidirectional reconcile lives in `reconcile.py`."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Credentials

log = logging.getLogger(__name__)


async def fetch_manifest(creds: Credentials) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {creds.token}"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{creds.api_base}/devices/me/fs/manifest", headers=headers
        )
        resp.raise_for_status()
        return resp.json()
