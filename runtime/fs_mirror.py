"""Chroot-enforced local mirror writer rooted at `~/.xelos/mirror/{org}/`.

Layout (new — matches the cloud S3 keyspace under the workspace slug):

    workspace → {org}/{rel}
    department   → {org}/{dept}/{rel}
    agent        → {org}/{dept}/{agent}/{rel}

Legacy daemons wrote `_org/`, `departments/<dept>/`, and `.../agents/<agent>/`
prefixes. `migrate_legacy_layout` upgrades an existing mirror in place on
daemon start so reconcile doesn't have to re-download every byte.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import _xelos_home
from .state_db import FileState, StateDB

log = logging.getLogger(__name__)


def org_root(workspace_slug: str) -> Path:
    return _xelos_home() / "mirror" / workspace_slug


def _resolve_target(
    *,
    workspace_slug: str,
    scope: str,
    department_slug: str | None,
    agent_slug: str | None,
    rel_path: str,
) -> Path:
    """Layout matches the cloud S3 prefix (workspace-rooted)."""
    base = org_root(workspace_slug)
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


def migrate_legacy_layout(workspace_slug: str) -> bool:
    """One-shot in-place migration from the legacy `_org/`/`departments/`/
    `agents/` layout to the workspace-rooted layout. Idempotent. Returns
    True iff something was moved.

    Strategy:
      1. Promote `{root}/_org/*` → `{root}/*`.
      2. For each `{root}/departments/<dept>/`:
         - Move `{root}/departments/<dept>/agents/<agent>/` → `{root}/<dept>/<agent>/`.
         - Promote `{root}/departments/<dept>/<rest>` → `{root}/<dept>/<rest>`
           (excluding the now-emptied `agents/` directory).
      3. Remove emptied `_org/` and `departments/` containers.

    Conflicts (target path already exists with content) are NOT silently
    overwritten — we fall back to a wipe of the legacy subtree so the next
    reconcile re-downloads. Cloud is SoT, so the worst case is bandwidth.
    """
    root = org_root(workspace_slug)
    legacy_org = root / "_org"
    legacy_depts = root / "departments"
    moved = False

    if not root.exists():
        return False

    if legacy_org.is_dir():
        moved |= _promote_children(legacy_org, root)
        _rmdir_if_empty(legacy_org)

    if legacy_depts.is_dir():
        for dept_dir in list(legacy_depts.iterdir()):
            if not dept_dir.is_dir():
                continue
            dept_target = root / dept_dir.name
            agents_dir = dept_dir / "agents"
            if agents_dir.is_dir():
                dept_target.mkdir(parents=True, exist_ok=True)
                for agent_dir in list(agents_dir.iterdir()):
                    if not agent_dir.is_dir():
                        continue
                    agent_target = dept_target / agent_dir.name
                    if agent_target.exists():
                        log.warning(
                            "legacy mirror migration: %s already exists, "
                            "wiping legacy %s",
                            agent_target,
                            agent_dir,
                        )
                        shutil.rmtree(agent_dir, ignore_errors=True)
                        continue
                    shutil.move(str(agent_dir), str(agent_target))
                    moved = True
                _rmdir_if_empty(agents_dir)
            # Promote remaining dept-scope contents to dept_target/.
            if dept_dir.is_dir():
                moved |= _promote_children(dept_dir, dept_target)
                _rmdir_if_empty(dept_dir)
        _rmdir_if_empty(legacy_depts)

    if moved:
        log.info(
            "legacy mirror layout migrated to workspace-rooted layout for %s",
            workspace_slug,
        )
    return moved


def _promote_children(src: Path, dst: Path) -> bool:
    moved = False
    if not src.is_dir():
        return False
    dst.mkdir(parents=True, exist_ok=True)
    for child in list(src.iterdir()):
        target = dst / child.name
        if target.exists():
            log.warning(
                "legacy mirror migration: %s already exists, wiping legacy %s",
                target,
                child,
            )
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
            continue
        shutil.move(str(child), str(target))
        moved = True
    return moved


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        # Non-empty or already gone — both fine.
        pass


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
        self.root = org_root(workspace_slug)
        self.root.mkdir(parents=True, exist_ok=True)
        # In-place upgrade legacy `_org/` + `departments/` layouts. Safe to
        # call on every boot: idempotent, and a no-op once everything has
        # already been promoted.
        try:
            migrate_legacy_layout(workspace_slug)
        except Exception:  # pragma: no cover
            log.exception(
                "legacy mirror layout migration failed for %s — falling back "
                "to reconcile",
                workspace_slug,
            )

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
        # Echo: incoming hash already matches local.
        prev = self.state.get(str(target))
        if prev is not None and prev.content_hash == actual_hash:
            return WriteOutcome(abs_path=target, skipped_echo=True)

        target.parent.mkdir(parents=True, exist_ok=True)
        # tmp + atomic rename so a crash can't leave a half-written file.
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
            # Only empty dirs — children may still be live nodes.
            try:
                target.rmdir()
            except OSError:
                log.warning("dir %s not empty — skipping rmdir", target)
        elif target.exists():
            target.unlink()
        self.state.delete(str(target))
        return target
