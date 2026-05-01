"""Outdated-version notifier. Compares installed vs latest `main` SHA."""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import httpx

from . import __version__
from .config import update_check_path

CACHE_TTL_SECONDS = 24 * 60 * 60
COMMIT_URL = "https://api.github.com/repos/markmelnic/xelos-edge/commits/main"


def _read_cache() -> Optional[dict]:
    path = update_check_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_cache(data: dict) -> None:
    path = update_check_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except Exception:
        pass


def _fetch_head_sha() -> Optional[str]:
    """Return the short SHA of `main`'s HEAD commit, or None on any failure."""
    try:
        resp = httpx.get(
            COMMIT_URL,
            timeout=2.0,
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 200:
            return None
        sha = resp.json().get("sha")
        if not isinstance(sha, str) or len(sha) < 7:
            return None
        return sha[:12]
    except Exception:
        return None


def maybe_warn_outdated(echo) -> None:
    """Print stderr banner if main has commits beyond the installed SHA."""
    if os.environ.get("XELOS_NO_UPDATE_CHECK") == "1":
        return
    if __version__ == "0.0.0+local":
        return

    cache = _read_cache() or {}
    installed_sha = cache.get("installed_sha")
    latest_sha = cache.get("latest_sha")
    fresh = (time.time() - cache.get("checked_at", 0)) < CACHE_TTL_SECONDS

    if not fresh or not latest_sha:
        latest_sha = _fetch_head_sha()
        if latest_sha is None:
            return
        # First-ever check: pin installed=latest so we don't nag on initial run.
        if not installed_sha:
            installed_sha = latest_sha
        _write_cache(
            {
                "installed_sha": installed_sha,
                "latest_sha": latest_sha,
                "checked_at": int(time.time()),
            }
        )

    if not installed_sha or not latest_sha or installed_sha == latest_sha:
        return
    echo(
        f"! xelos has updates on main (installed {installed_sha[:7]}, "
        f"latest {latest_sha[:7]}). Run: xelos update",
        err=True,
    )


def refresh_cache() -> None:
    """Pin cache to current HEAD. Call after a successful `xelos update`."""
    sha = _fetch_head_sha()
    if not sha:
        return
    _write_cache(
        {
            "installed_sha": sha,
            "latest_sha": sha,
            "checked_at": int(time.time()),
        }
    )
