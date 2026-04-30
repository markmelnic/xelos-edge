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


def update_check_path() -> Path:
    return _xelos_home() / "update-check.json"


@dataclass(slots=True)
class Credentials:
    api_base: str
    websocket_url: str
    device_id: str
    user_id: str
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
        # Migration: pre-user-scoped builds wrote a `workspace_id` key. The
        # field is now `user_id` (devices belong to a user, not an org). Map
        # the old key forward so already-paired daemons keep running until
        # the next pair refreshes the file.
        if "user_id" not in data and "workspace_id" in data:
            data["user_id"] = data.pop("workspace_id")
        # Drop unknown keys so a future-format credentials file doesn't break
        # `cls(**data)`.
        allowed = {"api_base", "websocket_url", "device_id", "user_id", "token"}
        data = {k: v for k, v in data.items() if k in allowed}
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
