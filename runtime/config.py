"""Credentials + state paths under `~/.xelos/` (override via XELOS_HOME)."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path


def _xelos_home() -> Path:
    base = os.environ.get("XELOS_HOME")
    if base:
        return Path(base).expanduser()
    return Path.home() / ".xelos"


def credentials_path() -> Path:
    return _xelos_home() / "credentials"


def state_db_path() -> Path:
    return _xelos_home() / "state.db"


@dataclass(slots=True)
class Credentials:
    api_base: str
    websocket_url: str
    device_id: str
    organization_id: str
    token: str

    @classmethod
    def load(cls) -> "Credentials | None":
        path = credentials_path()
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        try:
            return cls(**data)
        except TypeError:
            return None

    def save(self) -> None:
        home = _xelos_home()
        home.mkdir(parents=True, exist_ok=True)
        try:
            home.chmod(0o700)
        except OSError:
            pass

        path = credentials_path()
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)

    @staticmethod
    def clear() -> None:
        path = credentials_path()
        if path.exists():
            path.unlink()
