"""Local absolute path → cloud (scope, slugs, rel). Inverse of fs_mirror."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Editor tempfiles, OS metadata, conflict stashes — never push upstream.
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
    """Returns None for paths outside the org root or known noise files."""
    if should_ignore(abs_path):
        return None
    try:
        rel = abs_path.resolve().relative_to(root.resolve())
    except ValueError:
        return None

    parts = rel.parts
    if not parts:
        return None

    if parts[0] == "_org":
        if len(parts) < 2:
            return None
        return ResolvedPath(
            scope="workspace",
            department_slug=None,
            agent_slug=None,
            rel_path="/".join(parts[1:]),
        )

    if parts[0] != "departments" or len(parts) < 3:
        return None
    dept = parts[1]
    inner = parts[2:]

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

    return ResolvedPath(
        scope="department",
        department_slug=dept,
        agent_slug=None,
        rel_path="/".join(inner),
    )
