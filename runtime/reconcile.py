"""Three-way reconcile (manifest vs state.db vs disk) on each connect.

Outcomes per path: in_sync / pull_cloud / push_local / push_local_new /
push_delete / pull_delete / conflict (cloud wins, local stashed).

`state.content_hash` doubles as the synced-state hash both directions —
both fs.push apply and post-send local→cloud keep it current.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .config import Credentials
from .fingerprint import short_host
from .fs_mirror import FsMirror, _resolve_target
from .hydrate import fetch_manifest
from .path_resolver import resolve as resolve_local_path
from .path_resolver import should_ignore
from .state_db import FileState, StateDB

log = logging.getLogger(__name__)

DOWNLOAD_CONCURRENCY = 8


@dataclass(slots=True)
class ReconcileSummary:
    pulled: int = 0
    pushed: int = 0
    pushed_new: int = 0
    deleted_local: int = 0
    deleted_remote: int = 0
    conflicts: int = 0
    errors: int = 0
    skipped: int = 0
    in_sync: int = 0
    actions: list[str] = field(default_factory=list)

    def total(self) -> int:
        return (
            self.pulled
            + self.pushed
            + self.pushed_new
            + self.deleted_local
            + self.deleted_remote
            + self.conflicts
        )


# Send hook: daemon supplies a coroutine that JSON-encodes + ws.sends a frame.
SendFn = Callable[[dict[str, Any]], Awaitable[None]]


async def reconcile_with_cloud(
    *,
    credentials: Credentials,
    mirror: FsMirror,
    state: StateDB,
    send: SendFn,
    suppress: Callable[[str], None] | None = None,
) -> ReconcileSummary:
    """Reconcile the local mirror with the cloud manifest.

    `send` emits a frame upstream over the daemon's WS. `suppress` is
    invoked per locally-written path so the watcher skips the echo.
    """
    summary = ReconcileSummary()
    try:
        manifest = await fetch_manifest(credentials)
    except Exception:
        log.exception("manifest fetch failed; reconcile aborted")
        summary.errors += 1
        return summary

    if manifest.get("organization_slug") != mirror.org_slug:
        log.error(
            "org_slug mismatch (manifest=%s, mirror=%s) — aborting",
            manifest.get("organization_slug"),
            mirror.org_slug,
        )
        summary.errors += 1
        return summary

    manifest_by_path: dict[str, dict[str, Any]] = {}
    folders: list[dict[str, Any]] = []
    for entry in manifest.get("entries", []):
        kind = entry.get("kind")
        if kind == "folder":
            folders.append(entry)
            continue
        if kind != "file":
            continue
        try:
            target = _resolve_target(
                org_slug=mirror.org_slug,
                scope=entry["scope"],
                department_slug=entry.get("department_slug"),
                agent_slug=entry.get("agent_slug"),
                rel_path=entry["path"],
            )
        except Exception:
            log.warning("resolve failed: %s", entry.get("path"))
            continue
        manifest_by_path[str(target)] = entry

    # Folders first — file writes can't race their parents.
    for entry in folders:
        try:
            mirror.make_folder(
                scope=entry["scope"],
                department_slug=entry.get("department_slug"),
                agent_slug=entry.get("agent_slug"),
                rel_path=entry["path"],
            )
        except Exception:
            log.warning("folder reconcile skipped: %s", entry.get("path"))

    pulls: list[dict[str, Any]] = []
    pushes_existing: list[tuple[str, FileState | None]] = []
    pushes_new: list[str] = []
    deletes_local: list[str] = []
    conflicts: list[tuple[str, dict[str, Any], str]] = []

    known_paths = set(state.all_paths())
    walked: set[str] = set()

    for abs_path in known_paths:
        walked.add(abs_path)
        path_obj = Path(abs_path)
        prev = state.get(abs_path)
        cloud_entry = manifest_by_path.pop(abs_path, None)
        local_exists = path_obj.exists() and path_obj.is_file()

        if not local_exists:
            if cloud_entry is not None:
                summary.deleted_remote += 1
                resolved = resolve_local_path(
                    root=mirror.root, abs_path=path_obj
                )
                if resolved is None:
                    state.delete(abs_path)
                    continue
                await send(
                    {
                        "type": "fs.delete",
                        "request_id": str(uuid.uuid4()),
                        "scope": resolved.scope,
                        "department_slug": resolved.department_slug,
                        "agent_slug": resolved.agent_slug,
                        "path": resolved.rel_path,
                    }
                )
                state.delete(abs_path)
                summary.actions.append(f"push_delete {abs_path}")
            else:
                # Both gone — drop the stale state row.
                state.delete(abs_path)
                summary.skipped += 1
            continue

        try:
            local_bytes = path_obj.read_bytes()
        except OSError:
            summary.errors += 1
            continue
        local_hash = hashlib.sha256(local_bytes).hexdigest()
        synced_hash = prev.content_hash if prev else None

        if cloud_entry is None:
            if synced_hash == local_hash:
                # Cloud delete wins — local was untouched since last sync.
                deletes_local.append(abs_path)
            else:
                # Local edited after sync — re-create on cloud.
                pushes_existing.append((abs_path, prev))
            continue

        cloud_hash = cloud_entry.get("content_hash")
        if local_hash == cloud_hash:
            summary.in_sync += 1
            # Refresh state.last_synced_at so reconcile churn is visible.
            if prev is None or prev.content_hash != cloud_hash:
                _record_state(
                    state,
                    abs_path,
                    cloud_entry,
                    cloud_hash,
                    Path(abs_path).stat(),
                    "cloud",
                )
            continue

        if synced_hash == cloud_hash and synced_hash != local_hash:
            pushes_existing.append((abs_path, prev))
        elif synced_hash == local_hash and synced_hash != cloud_hash:
            pulls.append(cloud_entry)
        else:
            conflicts.append((abs_path, cloud_entry, local_hash))

    for abs_path, cloud_entry in manifest_by_path.items():
        path_obj = Path(abs_path)
        if path_obj.exists() and path_obj.is_file():
            try:
                local_hash = hashlib.sha256(path_obj.read_bytes()).hexdigest()
            except OSError:
                summary.errors += 1
                continue
            if local_hash == cloud_entry.get("content_hash"):
                _record_state(
                    state,
                    abs_path,
                    cloud_entry,
                    local_hash,
                    path_obj.stat(),
                    "cloud",
                )
                summary.in_sync += 1
                continue
            conflicts.append((abs_path, cloud_entry, local_hash))
        else:
            pulls.append(cloud_entry)

    for f in _walk_mirror_files(mirror.root):
        s = str(f)
        if s in walked:
            continue
        if state.get(s) is not None:
            continue
        if s in manifest_by_path:
            continue
        resolved = resolve_local_path(root=mirror.root, abs_path=f)
        if resolved is None:
            continue
        pushes_new.append(s)

    if pulls:
        async with httpx.AsyncClient(timeout=120) as http:
            sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

            async def _pull(entry: dict[str, Any]) -> bool:
                url = entry.get("presigned_url")
                if not url:
                    return False
                async with sem:
                    try:
                        resp = await http.get(url)
                        resp.raise_for_status()
                        content = resp.content
                    except Exception:
                        log.exception(
                            "pull download failed: %s", entry.get("path")
                        )
                        return False
                try:
                    outcome = mirror.write_file(
                        scope=entry["scope"],
                        department_slug=entry.get("department_slug"),
                        agent_slug=entry.get("agent_slug"),
                        rel_path=entry["path"],
                        content=content,
                        content_hash=entry.get("content_hash"),
                        origin="cloud",
                    )
                    if suppress is not None:
                        suppress(str(outcome.abs_path))
                    return True
                except Exception:
                    log.exception("pull write failed: %s", entry.get("path"))
                    return False

            results = await asyncio.gather(*(_pull(e) for e in pulls))
            for r in results:
                if r:
                    summary.pulled += 1
                    summary.actions.append("pull_cloud")
                else:
                    summary.errors += 1

    for abs_path in deletes_local:
        try:
            resolved = resolve_local_path(
                root=mirror.root, abs_path=Path(abs_path)
            )
            if resolved is None:
                state.delete(abs_path)
                continue
            target = mirror.delete(
                scope=resolved.scope,
                department_slug=resolved.department_slug,
                agent_slug=resolved.agent_slug,
                rel_path=resolved.rel_path,
            )
            if target is not None and suppress is not None:
                suppress(str(target))
            summary.deleted_local += 1
            summary.actions.append(f"pull_delete {abs_path}")
        except Exception:
            log.exception("local delete failed: %s", abs_path)
            summary.errors += 1

    for abs_path, prev in pushes_existing:
        try:
            await _push_file(
                abs_path=abs_path,
                mirror=mirror,
                state=state,
                send=send,
                parent_hash=prev.content_hash if prev else None,
            )
            summary.pushed += 1
            summary.actions.append(f"push_local {abs_path}")
        except Exception:
            log.exception("push existing failed: %s", abs_path)
            summary.errors += 1

    for abs_path in pushes_new:
        try:
            await _push_file(
                abs_path=abs_path,
                mirror=mirror,
                state=state,
                send=send,
                parent_hash=None,
            )
            summary.pushed_new += 1
            summary.actions.append(f"push_local_new {abs_path}")
        except Exception:
            log.exception("push new failed: %s", abs_path)
            summary.errors += 1

    for abs_path, cloud_entry, _local_hash in conflicts:
        try:
            await _resolve_conflict(
                abs_path=abs_path,
                cloud_entry=cloud_entry,
                mirror=mirror,
                state=state,
                suppress=suppress,
            )
            summary.conflicts += 1
            summary.actions.append(f"conflict {abs_path}")
        except Exception:
            log.exception("conflict resolve failed: %s", abs_path)
            summary.errors += 1

    log.info(
        "reconcile: pulled=%d pushed=%d pushed_new=%d "
        "deleted_local=%d deleted_remote=%d conflicts=%d errors=%d in_sync=%d",
        summary.pulled,
        summary.pushed,
        summary.pushed_new,
        summary.deleted_local,
        summary.deleted_remote,
        summary.conflicts,
        summary.errors,
        summary.in_sync,
    )
    return summary


def _walk_mirror_files(root: Path):
    if not root.exists():
        return
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if should_ignore(f):
            continue
        yield f


def _record_state(
    state: StateDB,
    abs_path: str,
    cloud_entry: dict[str, Any],
    content_hash: str,
    stat_result,
    origin: str,
) -> None:
    state.upsert(
        FileState(
            abs_path=abs_path,
            rel_path=cloud_entry["path"],
            scope=cloud_entry["scope"],
            department_slug=cloud_entry.get("department_slug"),
            agent_slug=cloud_entry.get("agent_slug"),
            content_hash=content_hash,
            size=stat_result.st_size,
            mtime=stat_result.st_mtime,
            last_synced_at=time.time(),
            origin=origin,
        )
    )


async def _push_file(
    *,
    abs_path: str,
    mirror: FsMirror,
    state: StateDB,
    send: SendFn,
    parent_hash: str | None,
) -> None:
    path_obj = Path(abs_path)
    resolved = resolve_local_path(root=mirror.root, abs_path=path_obj)
    if resolved is None:
        return
    content = path_obj.read_bytes()
    actual_hash = hashlib.sha256(content).hexdigest()
    await send(
        {
            "type": "fs.write",
            "request_id": str(uuid.uuid4()),
            "scope": resolved.scope,
            "department_slug": resolved.department_slug,
            "agent_slug": resolved.agent_slug,
            "path": resolved.rel_path,
            "content_b64": base64.b64encode(content).decode("ascii"),
            "content_hash": actual_hash,
            "parent_hash": parent_hash,
            "size": len(content),
            "mime_type": _guess_mime_simple(path_obj),
        }
    )
    st = path_obj.stat()
    state.upsert(
        FileState(
            abs_path=abs_path,
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


async def _resolve_conflict(
    *,
    abs_path: str,
    cloud_entry: dict[str, Any],
    mirror: FsMirror,
    state: StateDB,
    suppress: Callable[[str], None] | None,
) -> None:
    """Cloud version wins. Stash local copy then download cloud bytes."""
    import shutil

    path_obj = Path(abs_path)
    if path_obj.exists():
        stash = path_obj.with_name(
            f"{path_obj.name}.{short_host()}.conflict.{int(time.time())}"
        )
        try:
            shutil.copy2(path_obj, stash)
        except Exception:
            log.warning("conflict stash failed: %s", abs_path)

    url = cloud_entry.get("presigned_url")
    if not url:
        return
    async with httpx.AsyncClient(timeout=120) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        content = resp.content

    outcome = mirror.write_file(
        scope=cloud_entry["scope"],
        department_slug=cloud_entry.get("department_slug"),
        agent_slug=cloud_entry.get("agent_slug"),
        rel_path=cloud_entry["path"],
        content=content,
        content_hash=cloud_entry.get("content_hash"),
        origin="cloud",
    )
    if suppress is not None:
        suppress(str(outcome.abs_path))




def _guess_mime_simple(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        return "text/markdown" if suffix == ".md" else "text/plain"
    if suffix in (".json",):
        return "application/json"
    if suffix in (".yaml", ".yml"):
        return "application/yaml"
    if suffix in (".html",):
        return "text/html"
    return "application/octet-stream"
