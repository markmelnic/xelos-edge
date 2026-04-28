"""Reverse path resolver — local absolute path → cloud (scope, slugs, rel).

Cloud and daemon agree on a 1:1 mirror layout under `~/xelos/{org}/...`:

    {org}/_org/{rel}                              -> organization scope
    {org}/departments/{dept}/{rel}                -> department scope
    {org}/departments/{dept}/agents/{agent}/{rel} -> agent scope

This module turns a local absolute path back into the tuple the cloud
needs (`scope`, `department_slug`, `agent_slug`, `rel_path`). Returns
None for paths the daemon shouldn't sync (suffix files, stuff outside
the org root).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Files the watcher should never push upstream — write tempfiles, OS
# metadata, editor backups, etc. Conflict stashes are also skipped so
# they don't cycle back through the cloud.
IGNORE_SUFFIXES = (
    ".xelos-tmp",
    ".swp",
    ".swo",
    ".tmp",
    ".bak",
    "~",
)
IGNORE_NAMES = (
    ".DS_Store",
    "Thumbs.db",
    ".gitkeep",
)


@dataclass(slots=True, frozen=True)
class ResolvedPath:
    scope: str
    department_slug: str | None
    agent_slug: str | None
    rel_path: str


def should_ignore(path: Path) -> bool:
    name = path.name
    if name in IGNORE_NAMES:
        return True
    for s in IGNORE_SUFFIXES:
        if name.endswith(s):
            return True
    if ".conflict." in name:
        return True
    return False


def resolve(*, root: Path, abs_path: Path) -> ResolvedPath | None:
    """Map an absolute path inside the org root back to its cloud coords.

    `root` is the org root (`~/xelos/{org_slug}`). Returns None for
    paths outside the root or for known noise files.
    """
    if should_ignore(abs_path):
        return None
    try:
        rel = abs_path.resolve().relative_to(root.resolve())
    except ValueError:
        return None

    parts = rel.parts
    if not parts:
        return None

    # Org-scope: {org_root}/_org/...
    if parts[0] == "_org":
        if len(parts) < 2:
            return None
        return ResolvedPath(
            scope="organization",
            department_slug=None,
            agent_slug=None,
            rel_path="/".join(parts[1:]),
        )

    # Dept / Agent scope: {org_root}/departments/{dept}/...
    if parts[0] != "departments" or len(parts) < 3:
        return None
    dept = parts[1]
    inner = parts[2:]

    # Agent scope: .../agents/{agent}/...
    if len(inner) >= 2 and inner[0] == "agents":
        agent = inner[1]
        rel_inner = inner[2:]
        if not rel_inner:
            return None
        return ResolvedPath(
            scope="agent",
            department_slug=dept,
            agent_slug=agent,
            rel_path="/".join(rel_inner),
        )

    # Plain dept scope.
    return ResolvedPath(
        scope="department",
        department_slug=dept,
        agent_slug=None,
        rel_path="/".join(inner),
    )
