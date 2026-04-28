"""Initial sync — fetch the org-tree manifest and download every file.

Downloads run concurrently against the presigned S3 URLs supplied by
the cloud manifest. The manifest also acts as the periodic reconcile
mechanism: on connect, we re-fetch and apply any drift detected
against the local mirror state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import Credentials
from .fs_mirror import FsMirror
from .state_db import StateDB

log = logging.getLogger(__name__)

# How many files to download in parallel. S3 / R2 / MinIO all handle
# burst concurrency well; bottleneck is usually the daemon's disk.
HYDRATE_CONCURRENCY = 8


async def fetch_manifest(creds: Credentials) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {creds.token}"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{creds.api_base}/devices/me/fs/manifest", headers=headers
        )
        resp.raise_for_status()
        return resp.json()


async def hydrate(
    creds: Credentials,
    *,
    state: StateDB,
) -> tuple[int, int, int]:
    """Pull the manifest and reconcile the local mirror.

    Returns (files_written, folders_created, errors).
    """
    manifest = await fetch_manifest(creds)
    org_slug = manifest.get("organization_slug")
    if not org_slug:
        raise ValueError("manifest missing organization_slug")

    mirror = FsMirror(org_slug=org_slug, state=state)
    entries = manifest.get("entries", [])
    log.info("hydrate: %d entries", len(entries))

    # Folders first so file writes don't race their parents.
    folders = [e for e in entries if e.get("kind") == "folder"]
    files = [e for e in entries if e.get("kind") == "file"]

    folders_created = 0
    for entry in folders:
        try:
            mirror.make_folder(
                scope=entry["scope"],
                department_slug=entry.get("department_slug"),
                agent_slug=entry.get("agent_slug"),
                rel_path=entry["path"],
            )
            folders_created += 1
        except Exception:
            log.exception("hydrate folder failed: %s", entry.get("path"))

    sem = asyncio.Semaphore(HYDRATE_CONCURRENCY)
    written = 0
    errors = 0

    async with httpx.AsyncClient(timeout=120) as http:

        async def _one(entry: dict[str, Any]) -> bool:
            url = entry.get("presigned_url")
            if not url:
                # No presigned URL = bucket not configured cloud-side.
                # Skip silently; the daemon can hydrate later.
                return False

            # Skip if we already have the right content locally.
            try:
                target = _resolve(mirror, entry)
            except Exception:
                log.exception("resolve failed: %s", entry)
                return False
            existing = state.get(str(target))
            if existing and existing.content_hash == entry.get("content_hash"):
                return False

            async with sem:
                try:
                    resp = await http.get(url)
                    resp.raise_for_status()
                    content = resp.content
                except Exception as exc:
                    log.warning(
                        "download failed %s: %s", entry.get("path"), exc
                    )
                    return None

            try:
                mirror.write_file(
                    scope=entry["scope"],
                    department_slug=entry.get("department_slug"),
                    agent_slug=entry.get("agent_slug"),
                    rel_path=entry["path"],
                    content=content,
                    content_hash=entry.get("content_hash"),
                    origin="cloud",
                )
                return True
            except Exception:
                log.exception("local write failed: %s", entry.get("path"))
                return None

        results = await asyncio.gather(*[_one(e) for e in files])
        for r in results:
            if r is True:
                written += 1
            elif r is None:
                errors += 1

    log.info(
        "hydrate complete: %d folders, %d files written, %d errors",
        folders_created,
        written,
        errors,
    )
    return written, folders_created, errors


def _resolve(mirror: FsMirror, entry: dict[str, Any]):
    from .fs_mirror import _resolve_target

    return _resolve_target(
        org_slug=mirror.org_slug,
        scope=entry["scope"],
        department_slug=entry.get("department_slug"),
        agent_slug=entry.get("agent_slug"),
        rel_path=entry["path"],
    )
