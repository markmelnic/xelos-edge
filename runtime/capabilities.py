"""Detect host capabilities reported to cloud at pair / connect time."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from typing import Any

from . import __version__


def _claude_code_version() -> str | None:
    if shutil.which("claude") is None:
        return None
    try:
        out = subprocess.check_output(
            ["claude", "--version"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
        # Output looks like "1.0.49 (Claude Code)".
        return out.split()[0] if out else None
    except Exception:
        return None


def _cpu_count() -> int:
    return os.cpu_count() or 1


def detect() -> dict[str, Any]:
    return {
        "edge_version": __version__,
        "claude_code_version": _claude_code_version(),
        "os": sys.platform,  # darwin | linux | win32
        "arch": platform.machine().lower() or None,
        "has_node": shutil.which("node") is not None,
        "has_python": platform.python_version(),
        "max_concurrent_runs": min(8, _cpu_count()),
    }
