"""Outdated-version notifier. Best-effort, silent on failure."""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import httpx

from . import __version__
from .config import update_check_path

CACHE_TTL_SECONDS = 24 * 60 * 60
RELEASE_URL = "https://api.github.com/repos/markmelnic/xelos-edge/releases/latest"


def _read_cache() -> Optional[dict]:
    path = update_check_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_cache(latest: str) -> None:
    path = update_check_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"latest": latest, "checked_at": int(time.time())}))
    except Exception:
        pass


def _fetch_latest() -> Optional[str]:
    try:
        resp = httpx.get(RELEASE_URL, timeout=2.0, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            return None
        tag = resp.json().get("tag_name")
        if not isinstance(tag, str):
            return None
        return tag.lstrip("v")
    except Exception:
        return None


def _is_newer(latest: str, current: str) -> bool:
    def parse(v: str) -> tuple:
        v = v.split("+", 1)[0].split("-", 1)[0]
        parts = []
        for p in v.split("."):
            try:
                parts.append(int(p))
            except ValueError:
                return ()
        return tuple(parts)

    lp, cp = parse(latest), parse(current)
    if not lp or not cp:
        return False
    return lp > cp


def maybe_warn_outdated(echo) -> None:
    """Print stderr banner if a newer release exists. Never raises."""
    if os.environ.get("XELOS_NO_UPDATE_CHECK") == "1":
        return
    if __version__ == "0.0.0+local":
        return

    cache = _read_cache()
    latest: Optional[str] = None
    if cache and (time.time() - cache.get("checked_at", 0)) < CACHE_TTL_SECONDS:
        latest = cache.get("latest")
    else:
        latest = _fetch_latest()
        if latest:
            _write_cache(latest)

    if not latest:
        return
    if _is_newer(latest, __version__):
        echo(
            f"! xelos v{latest} available (current v{__version__}). Run: xelos update",
            err=True,
        )


def refresh_cache() -> None:
    """Force a cache refresh. Call after a successful update."""
    latest = _fetch_latest()
    if latest:
        _write_cache(latest)
