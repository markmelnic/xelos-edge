"""SQLite-backed local store. Survives daemon restarts.

Tables:
- file_state    — per-path sync metadata (existing).
- run_history   — every dispatched run + lifecycle (so the TUI doesn't look
                  empty after a restart and we have a local audit trail).
- agent_session — Claude Code session id per agent (resume fallback when
                  the cloud doesn't pass one in `job.start`).
- log_history   — bounded log ring buffer mirrored from the daemon's own
                  log handler.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

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

CREATE TABLE IF NOT EXISTS run_history (
    run_id              TEXT PRIMARY KEY,
    agent_id            TEXT,
    agent_slug          TEXT,
    agent_name          TEXT,
    department_slug     TEXT,
    workspace_slug      TEXT,
    status              TEXT,
    started_at          REAL,
    completed_at        REAL,
    last_event_at       REAL,
    step_count          INTEGER DEFAULT 0,
    error               TEXT,
    external_session_id TEXT,
    files_modified      TEXT     -- JSON array
);

CREATE INDEX IF NOT EXISTS ix_run_history_started
    ON run_history (started_at DESC);
CREATE INDEX IF NOT EXISTS ix_run_history_agent
    ON run_history (agent_id);

CREATE TABLE IF NOT EXISTS agent_session (
    agent_id        TEXT PRIMARY KEY,
    last_session_id TEXT,
    last_run_id     TEXT,
    updated_at      REAL
);

CREATE TABLE IF NOT EXISTS log_history (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL    NOT NULL,
    level  TEXT    NOT NULL,
    msg    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_log_history_ts ON log_history (ts DESC);
"""

# Ring-buffer caps; trimmed opportunistically.
_MAX_LOG_ROWS = 5000
_MAX_RUN_ROWS = 1000


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


@dataclass(slots=True)
class RunRecord:
    run_id: str
    agent_id: str | None
    agent_slug: str | None
    agent_name: str | None
    department_slug: str | None
    workspace_slug: str | None
    status: str
    started_at: float | None
    completed_at: float | None
    last_event_at: float | None
    step_count: int
    error: str | None
    external_session_id: str | None
    files_modified: list[str]


@dataclass(slots=True)
class LogRecord:
    ts: float
    level: str
    msg: str


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

    # --- run_history ----------------------------------------------------

    def upsert_run(self, rec: RunRecord) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO run_history(
                    run_id, agent_id, agent_slug, agent_name,
                    department_slug, workspace_slug, status,
                    started_at, completed_at, last_event_at,
                    step_count, error, external_session_id, files_modified
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    agent_id            = COALESCE(excluded.agent_id, run_history.agent_id),
                    agent_slug          = COALESCE(excluded.agent_slug, run_history.agent_slug),
                    agent_name          = COALESCE(excluded.agent_name, run_history.agent_name),
                    department_slug     = COALESCE(excluded.department_slug, run_history.department_slug),
                    workspace_slug      = COALESCE(excluded.workspace_slug, run_history.workspace_slug),
                    status              = COALESCE(excluded.status, run_history.status),
                    started_at          = COALESCE(excluded.started_at, run_history.started_at),
                    completed_at        = COALESCE(excluded.completed_at, run_history.completed_at),
                    last_event_at       = COALESCE(excluded.last_event_at, run_history.last_event_at),
                    step_count          = MAX(excluded.step_count, run_history.step_count),
                    error               = COALESCE(excluded.error, run_history.error),
                    external_session_id = COALESCE(excluded.external_session_id, run_history.external_session_id),
                    files_modified      = COALESCE(excluded.files_modified, run_history.files_modified)
                """,
                (
                    rec.run_id,
                    rec.agent_id,
                    rec.agent_slug,
                    rec.agent_name,
                    rec.department_slug,
                    rec.workspace_slug,
                    rec.status,
                    rec.started_at,
                    rec.completed_at,
                    rec.last_event_at,
                    rec.step_count,
                    rec.error,
                    rec.external_session_id,
                    json.dumps(rec.files_modified) if rec.files_modified else None,
                ),
            )
            # Opportunistic trim — bounded by `_MAX_RUN_ROWS`, oldest first.
            conn.execute(
                """
                DELETE FROM run_history WHERE run_id IN (
                    SELECT run_id FROM run_history
                    ORDER BY started_at DESC NULLS LAST
                    LIMIT -1 OFFSET ?
                )
                """,
                (_MAX_RUN_ROWS,),
            )

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str | None = None,
        bump_step: bool = False,
        last_event_at: float | None = None,
        completed_at: float | None = None,
        error: str | None = None,
        external_session_id: str | None = None,
        files_modified: list[str] | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if last_event_at is not None:
            sets.append("last_event_at = ?")
            params.append(last_event_at)
        if completed_at is not None:
            sets.append("completed_at = ?")
            params.append(completed_at)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if external_session_id is not None:
            sets.append("external_session_id = ?")
            params.append(external_session_id)
        if files_modified is not None:
            sets.append("files_modified = ?")
            params.append(json.dumps(files_modified))
        if bump_step:
            sets.append("step_count = step_count + 1")
        if not sets:
            return
        params.append(run_id)
        with self._tx() as conn:
            conn.execute(
                f"UPDATE run_history SET {', '.join(sets)} WHERE run_id = ?",
                params,
            )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM run_history WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        files: list[str] = []
        if row["files_modified"]:
            try:
                files = list(json.loads(row["files_modified"]))
            except (json.JSONDecodeError, TypeError):
                files = []
        return RunRecord(
            run_id=row["run_id"],
            agent_id=row["agent_id"],
            agent_slug=row["agent_slug"],
            agent_name=row["agent_name"],
            department_slug=row["department_slug"],
            workspace_slug=row["workspace_slug"],
            status=row["status"] or "queued",
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            last_event_at=row["last_event_at"],
            step_count=int(row["step_count"] or 0),
            error=row["error"],
            external_session_id=row["external_session_id"],
            files_modified=files,
        )

    def recent_runs(self, limit: int = 100) -> list[RunRecord]:
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT * FROM run_history
                ORDER BY started_at DESC NULLS LAST
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out: list[RunRecord] = []
        for r in rows:
            files = []
            raw = r["files_modified"]
            if raw:
                try:
                    files = list(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    files = []
            out.append(
                RunRecord(
                    run_id=r["run_id"],
                    agent_id=r["agent_id"],
                    agent_slug=r["agent_slug"],
                    agent_name=r["agent_name"],
                    department_slug=r["department_slug"],
                    workspace_slug=r["workspace_slug"],
                    status=r["status"] or "queued",
                    started_at=r["started_at"],
                    completed_at=r["completed_at"],
                    last_event_at=r["last_event_at"],
                    step_count=int(r["step_count"] or 0),
                    error=r["error"],
                    external_session_id=r["external_session_id"],
                    files_modified=files,
                )
            )
        return out

    # --- agent_session --------------------------------------------------

    def get_agent_session(self, agent_id: str) -> str | None:
        if not agent_id:
            return None
        with self._tx() as conn:
            row = conn.execute(
                "SELECT last_session_id FROM agent_session WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        return row["last_session_id"] if row else None

    def update_agent_session(
        self,
        *,
        agent_id: str,
        session_id: str,
        run_id: str | None,
    ) -> None:
        if not agent_id or not session_id:
            return
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO agent_session(agent_id, last_session_id, last_run_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    last_session_id = excluded.last_session_id,
                    last_run_id     = excluded.last_run_id,
                    updated_at      = excluded.updated_at
                """,
                (agent_id, session_id, run_id, time.time()),
            )

    # --- log_history ----------------------------------------------------

    def append_log(self, level: str, msg: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO log_history(ts, level, msg) VALUES (?, ?, ?)",
                (time.time(), level, msg),
            )
            conn.execute(
                """
                DELETE FROM log_history WHERE id IN (
                    SELECT id FROM log_history
                    ORDER BY id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (_MAX_LOG_ROWS,),
            )

    def recent_logs(self, limit: int = 200) -> list[LogRecord]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT ts, level, msg FROM log_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        # Caller wants chronological order for the tail view.
        return [
            LogRecord(ts=r["ts"], level=r["level"], msg=r["msg"])
            for r in reversed(rows)
        ]
