"""SQLite-backed per-path sync state. Survives daemon restarts."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import state_db_path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_state (
    abs_path        TEXT PRIMARY KEY,
    rel_path        TEXT NOT NULL,
    scope           TEXT NOT NULL,
    department_slug TEXT,
    agent_slug      TEXT,
    content_hash    TEXT,
    size            INTEGER,
    mtime           REAL,
    last_synced_at  REAL,
    origin          TEXT      -- 'cloud' | 'local'
);

CREATE INDEX IF NOT EXISTS ix_file_state_scope
    ON file_state (scope, department_slug, agent_slug);
"""


@dataclass(slots=True)
class FileState:
    abs_path: str
    rel_path: str
    scope: str
    department_slug: str | None
    agent_slug: str | None
    content_hash: str | None
    size: int | None
    mtime: float | None
    last_synced_at: float | None
    origin: str | None


class StateDB:

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or state_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        # `executescript` implicitly commits, so it can't run inside our
        # manual BEGIN/COMMIT wrapper (Python 3.14 raises otherwise).
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
            finally:
                conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def get(self, abs_path: str) -> FileState | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM file_state WHERE abs_path = ?", (abs_path,)
            ).fetchone()
        if row is None:
            return None
        return FileState(**dict(row))

    def upsert(self, state: FileState) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO file_state(
                    abs_path, rel_path, scope, department_slug, agent_slug,
                    content_hash, size, mtime, last_synced_at, origin
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(abs_path) DO UPDATE SET
                    rel_path        = excluded.rel_path,
                    scope           = excluded.scope,
                    department_slug = excluded.department_slug,
                    agent_slug      = excluded.agent_slug,
                    content_hash    = excluded.content_hash,
                    size            = excluded.size,
                    mtime           = excluded.mtime,
                    last_synced_at  = excluded.last_synced_at,
                    origin          = excluded.origin
                """,
                (
                    state.abs_path,
                    state.rel_path,
                    state.scope,
                    state.department_slug,
                    state.agent_slug,
                    state.content_hash,
                    state.size,
                    state.mtime,
                    state.last_synced_at,
                    state.origin,
                ),
            )

    def delete(self, abs_path: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM file_state WHERE abs_path = ?", (abs_path,)
            )

    def all_paths(self) -> list[str]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT abs_path FROM file_state"
            ).fetchall()
        return [r["abs_path"] for r in rows]
