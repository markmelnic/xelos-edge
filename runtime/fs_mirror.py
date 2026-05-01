"""Chroot-enforced local mirror writer rooted at `~/.xelos/mirror/{workspace}/`.

Layout matches the cloud S3 keyspace:

    workspace  → {workspace}/{rel}
    department → {workspace}/{dept}/{rel}
    agent      → {workspace}/{dept}/{agent}/{rel}
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .config import _xelos_home
from .state_db import FileState, StateDB

log = logging.getLogger(__name__)


def workspace_root(workspace_slug: str) -> Path:
    return _xelos_home() / "mirror" / workspace_slug


def _resolve_target(
    *,
    workspace_slug: str,
    scope: str,
    department_slug: str | None,
    agent_slug: str | None,
    rel_path: str,
) -> Path:
    base = workspace_root(workspace_slug)
    if scope == "workspace":
        prefix = base
    elif scope == "department":
        if not department_slug:
            raise ValueError("department scope without department_slug")
        prefix = base / department_slug
    elif scope == "agent":
        if not department_slug or not agent_slug:
            raise ValueError("agent scope without dept/agent slug")
        prefix = base / department_slug / agent_slug
    else:
        raise ValueError(f"unknown scope: {scope}")

    rel = _normalise_rel(rel_path)
    target = (prefix / rel).resolve()
    base_resolved = base.resolve()
    if not _is_within(base_resolved, target):
        raise ValueError(f"refused path escape: {target}")
    return target


def _normalise_rel(p: str) -> str:
    parts = [seg for seg in p.replace("\\", "/").split("/") if seg and seg != "."]
    if any(seg == ".." for seg in parts):
        raise ValueError("path traversal with '..' is not allowed")
    return "/".join(parts)


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


@dataclass(slots=True)
class WriteOutcome:
    abs_path: Path
    skipped_echo: bool = False
    written_bytes: int = 0


class FsMirror:
    def __init__(self, *, workspace_slug: str, state: StateDB) -> None:
        self.workspace_slug = workspace_slug
        self.state = state
        self.root = workspace_root(workspace_slug)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_file(
        self,
        *,
        scope: str,
        department_slug: str | None,
        agent_slug: str | None,
        rel_path: str,
        content: bytes,
        content_hash: str | None = None,
        origin: str = "cloud",
    ) -> WriteOutcome:
        target = _resolve_target(
            workspace_slug=self.workspace_slug,
            scope=scope,
            department_slug=department_slug,
            agent_slug=agent_slug,
            rel_path=rel_path,
        )

        actual_hash = content_hash or hashlib.sha256(content).hexdigest()
        prev = self.state.get(str(target))
        if prev is not None and prev.content_hash == actual_hash:
            return WriteOutcome(abs_path=target, skipped_echo=True)

        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic rename so a crash can't leave a half-written file.
        tmp = target.with_suffix(target.suffix + ".xelos-tmp")
        with tmp.open("wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)

        st = target.stat()
        self.state.upsert(
            FileState(
                abs_path=str(target),
                rel_path=rel_path,
                scope=scope,
                department_slug=department_slug,
                agent_slug=agent_slug,
                content_hash=actual_hash,
                size=st.st_size,
                mtime=st.st_mtime,
                last_synced_at=time.time(),
                origin=origin,
            )
        )
        return WriteOutcome(abs_path=target, written_bytes=len(content))

    def make_folder(
        self,
        *,
        scope: str,
        department_slug: str | None,
        agent_slug: str | None,
        rel_path: str,
    ) -> Path:
        target = _resolve_target(
            workspace_slug=self.workspace_slug,
            scope=scope,
            department_slug=department_slug,
            agent_slug=agent_slug,
            rel_path=rel_path,
        )
        target.mkdir(parents=True, exist_ok=True)
        return target

    def delete(
        self,
        *,
        scope: str,
        department_slug: str | None,
        agent_slug: str | None,
        rel_path: str,
    ) -> Path | None:
        target = _resolve_target(
            workspace_slug=self.workspace_slug,
            scope=scope,
            department_slug=department_slug,
            agent_slug=agent_slug,
            rel_path=rel_path,
        )
        if target.is_dir():
            try:
                target.rmdir()
            except OSError:
                log.warning("dir %s not empty — skipping rmdir", target)
        elif target.exists():
            target.unlink()
        self.state.delete(str(target))
        return target
