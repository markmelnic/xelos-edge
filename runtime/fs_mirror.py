"""Local mirror writer.

Lays files out under `~/xelos/{org_slug}/...` matching the cloud S3
prefix. Refuses any path that escapes the org root (chroot-enforced).

For P1a only cloud→device direction is wired; fsnotify upstream lands
in P1b.
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


def org_root(org_slug: str) -> Path:
    """Local mirror root for an organization."""
    return _xelos_home() / "mirror" / org_slug


def _resolve_target(
    *,
    org_slug: str,
    scope: str,
    department_slug: str | None,
    agent_slug: str | None,
    rel_path: str,
) -> Path:
    """Compute the absolute path for a file_node.

    Mirrors the cloud S3 prefix shape:
        organization → {org}/_org/{rel}
        department   → {org}/departments/{dept}/{rel}
        agent        → {org}/departments/{dept}/agents/{agent}/{rel}
    """
    base = org_root(org_slug)
    if scope == "organization":
        prefix = base / "_org"
    elif scope == "department":
        if not department_slug:
            raise ValueError("department scope without department_slug")
        prefix = base / "departments" / department_slug
    elif scope == "agent":
        if not department_slug or not agent_slug:
            raise ValueError("agent scope without dept/agent slug")
        prefix = base / "departments" / department_slug / "agents" / agent_slug
    else:
        raise ValueError(f"unknown scope: {scope}")

    rel = _normalise_rel(rel_path)
    target = (prefix / rel).resolve()
    # Chroot enforcement — refuse anything outside the org root.
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
    def __init__(self, *, org_slug: str, state: StateDB) -> None:
        self.org_slug = org_slug
        self.state = state
        self.root = org_root(org_slug)
        self.root.mkdir(parents=True, exist_ok=True)

    # File ops -------------------------------------------------------------
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
            org_slug=self.org_slug,
            scope=scope,
            department_slug=department_slug,
            agent_slug=agent_slug,
            rel_path=rel_path,
        )

        actual_hash = content_hash or hashlib.sha256(content).hexdigest()
        # Echo guard — incoming push matches what's already on disk.
        prev = self.state.get(str(target))
        if prev is not None and prev.content_hash == actual_hash:
            return WriteOutcome(abs_path=target, skipped_echo=True)

        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish write: tmp + rename so a crash mid-write doesn't
        # leave a half-written file matching the wrong hash.
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
            org_slug=self.org_slug,
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
            org_slug=self.org_slug,
            scope=scope,
            department_slug=department_slug,
            agent_slug=agent_slug,
            rel_path=rel_path,
        )
        if target.is_dir():
            # Only delete an empty dir; remaining children belong to
            # other still-live nodes that haven't been deleted yet.
            try:
                target.rmdir()
            except OSError:
                log.warning("dir %s not empty — skipping rmdir", target)
        elif target.exists():
            target.unlink()
        self.state.delete(str(target))
        return target
