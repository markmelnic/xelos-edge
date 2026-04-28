"""Stable per-install fingerprint.

Hash of (machine-id ∥ user) so multiple users on one box still get
distinct fingerprints. Falls back to a generated UUID stored at
`~/.xelos/fingerprint` when no platform machine id is readable.
"""

from __future__ import annotations

import hashlib
import platform
import uuid
from pathlib import Path

from .config import _xelos_home


def _read_machine_id() -> str | None:
    candidates = (
        Path("/etc/machine-id"),  # systemd
        Path("/var/lib/dbus/machine-id"),
    )
    for p in candidates:
        try:
            if p.is_file():
                v = p.read_text(encoding="utf-8").strip()
                if v:
                    return v
        except OSError:
            continue
    # macOS — IOPlatformUUID via ioreg
    if platform.system() == "Darwin":
        try:
            import subprocess

            out = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).decode()
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    parts = line.split('"')
                    if len(parts) >= 4:
                        return parts[-2]
        except Exception:
            pass
    return None


def _generate_persistent() -> str:
    home = _xelos_home()
    home.mkdir(parents=True, exist_ok=True)
    fp_file = home / "fingerprint"
    if fp_file.exists():
        return fp_file.read_text(encoding="utf-8").strip()
    new_id = str(uuid.uuid4())
    fp_file.write_text(new_id, encoding="utf-8")
    fp_file.chmod(0o600)
    return new_id


def fingerprint() -> str:
    base = _read_machine_id() or _generate_persistent()
    user = (
        Path.home().name
        or platform.node()
        or "unknown"
    )
    return hashlib.sha256(f"{base}::{user}".encode("utf-8")).hexdigest()
