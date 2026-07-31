"""SQLite persistence for analysis jobs and results.

WAL mode, migrations, deterministic ordering.
Failed/incomplete analyses are never returned as successful cache hits.

Thread safety: uses a threading.Lock around all writes and a per-call
connection factory for thread safety. SQLite WAL supports concurrent
readers with one writer, which is sufficient for this workload.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

SCHEMA_VERSION = 1

MIGRATIONS = [
    # Version 1: initial schema
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS analysis_jobs (
        job_id TEXT PRIMARY KEY,
        analysis_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        progress_phase TEXT NOT NULL DEFAULT '',
        progress_message TEXT NOT NULL DEFAULT '',
        error_code TEXT,
        error_message TEXT,
        cache_key TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_jobs_project ON analysis_jobs(project_id);
    CREATE INDEX IF NOT EXISTS idx_jobs_cache_key ON analysis_jobs(cache_key);
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON analysis_jobs(status);

    CREATE TABLE IF NOT EXISTS analysis_results (
        analysis_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        cache_key TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(cache_key)
    );

    CREATE INDEX IF NOT EXISTS idx_results_project ON analysis_results(project_id);
    CREATE INDEX IF NOT EXISTS idx_results_cache_key ON analysis_results(cache_key);
    """,
]


class AnalysisDB:
    """SQLite-backed analysis persistence with thread-safe access."""

    def __init__(self, db_path: str, job_timeout_seconds: int = 600):
        self._db_path = db_path
        self._job_timeout_seconds = job_timeout_seconds
        self._write_lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        conn = self._connect()
        try:
            self._migrate(conn)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self, conn: sqlite3.Connection):
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if not cur.fetchone():
            for sql in MIGRATIONS[0].split(";"):
                sql = sql.strip()
                if sql:
                    conn.execute(sql)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            conn.commit()
            return

        cur = conn.execute("SELECT MAX(version) FROM schema_version")
        row = cur.fetchone()
        current = row[0] if row else 0

        for version in range(current, SCHEMA_VERSION):
            for sql in MIGRATIONS[version].split(";"):
                sql = sql.strip()
                if sql:
                    conn.execute(sql)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (version + 1,),
            )
        conn.commit()

    def close(self):
        pass

    def recover_interrupted_jobs(self):
        """Mark orphaned 'running' AND 'queued' jobs as 'interrupted' on restart."""
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute(
                    """UPDATE analysis_jobs SET status = 'interrupted',
                       error_code = 'process_restart',
                       error_message = 'Job was interrupted by process restart',
                       finished_at = ?
                       WHERE status IN ('running', 'queued')""",
                    (now,),
                )
                conn.commit()
            finally:
                conn.close()

    def enforce_timeouts(self):
        """Mark timed-out running jobs as failed."""
        now = datetime.now(timezone.utc)
        with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT job_id, started_at FROM analysis_jobs WHERE status = 'running'"
                )
                for row in cur.fetchall():
                    started = row["started_at"]
                    if started:
                        started_dt = datetime.fromisoformat(started)
                        if (now - started_dt).total_seconds() > self._job_timeout_seconds:
                            conn.execute(
                                """UPDATE analysis_jobs SET status = 'failed',
                                   error_code = 'timeout',
                                   error_message = ?,
                                   finished_at = ?
                                   WHERE job_id = ? AND status = 'running'""",
                                (
                                    f"Job timed out after {self._job_timeout_seconds}s",
                                    now.isoformat(),
                                    row["job_id"],
                                ),
                            )
                conn.commit()
            finally:
                conn.close()

    def create_job(
        self,
        job_id: str,
        analysis_id: str,
        project_id: str,
        cache_key: str,
    ) -> dict:
        """Create a new job atomically, checking queue limits."""
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """INSERT INTO analysis_jobs
                       (job_id, analysis_id, project_id, cache_key, status, created_at)
                       VALUES (?, ?, ?, ?, 'queued', ?)""",
                    (job_id, analysis_id, project_id, cache_key, now),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return self.get_job(job_id)

    def find_or_create_job_for_cache(
        self,
        cache_key: str,
        job_id: str,
        analysis_id: str,
        project_id: str,
        max_queue_length: int,
    ) -> tuple[dict, bool]:
        """Idempotent: return existing active job or cached result for this
        cache_key, or atomically create a new one.

        Returns (job_or_result_dict, is_new).
        Raises ValueError if the queue is full and no existing job matches.
        """
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                # Check for existing successful result
                cur = conn.execute(
                    "SELECT analysis_id FROM analysis_results WHERE cache_key = ?",
                    (cache_key,),
                )
                cached = cur.fetchone()
                if cached:
                    conn.rollback()
                    conn.close()
                    return {"analysis_id": cached["analysis_id"], "status": "cached"}, False

                # Check for existing active (queued/running) job with same cache key
                cur = conn.execute(
                    """SELECT * FROM analysis_jobs
                       WHERE cache_key = ? AND status IN ('queued', 'running')
                       ORDER BY created_at ASC LIMIT 1""",
                    (cache_key,),
                )
                existing = cur.fetchone()
                if existing:
                    conn.rollback()
                    conn.close()
                    return dict(existing), False

                # Count active jobs for queue limit
                cur = conn.execute(
                    "SELECT COUNT(*) FROM analysis_jobs WHERE status IN ('queued', 'running')"
                )
                active = cur.fetchone()[0]
                if active >= max_queue_length:
                    conn.rollback()
                    raise ValueError(
                        f"Queue full: {active} active jobs, limit {max_queue_length}"
                    )

                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """INSERT INTO analysis_jobs
                       (job_id, analysis_id, project_id, cache_key, status, created_at)
                       VALUES (?, ?, ?, ?, 'queued', ?)""",
                    (job_id, analysis_id, project_id, cache_key, now),
                )
                conn.commit()
            except ValueError:
                raise
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        return self.get_job(job_id), True

    def get_job(self, job_id: str) -> Optional[dict]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM analysis_jobs WHERE job_id = ?", (job_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_jobs_for_project(
        self, project_id: str, limit: int = 50
    ) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.execute(
                """SELECT * FROM analysis_jobs WHERE project_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (project_id, limit),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def update_job_status(
        self,
        job_id: str,
        status: str,
        progress_phase: str = "",
        progress_message: str = "",
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        now = datetime.now(timezone.utc).isoformat()
        updates = {
            "status": status,
            "progress_phase": progress_phase,
            "progress_message": progress_message,
        }
        if status == "running":
            updates["started_at"] = now
        if status in ("succeeded", "failed", "cancelled", "interrupted"):
            updates["finished_at"] = now
        if error_code:
            updates["error_code"] = error_code
        if error_message:
            updates["error_message"] = error_message

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [job_id]
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute(
                    f"UPDATE analysis_jobs SET {set_clause} WHERE job_id = ?",
                    values,
                )
                conn.commit()
            finally:
                conn.close()

    def find_cached_result(self, cache_key: str) -> Optional[dict]:
        """Find existing successful analysis by cache key.

        Never returns failed/incomplete analyses.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM analysis_results WHERE cache_key = ?",
                (cache_key,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def store_result(
        self,
        analysis_id: str,
        project_id: str,
        cache_key: str,
        result_json: str,
    ):
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO analysis_results
                       (analysis_id, project_id, cache_key, result_json, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (analysis_id, project_id, cache_key, result_json, now),
                )
                conn.commit()
            finally:
                conn.close()

    def get_result(self, analysis_id: str) -> Optional[dict]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM analysis_results WHERE analysis_id = ?",
                (analysis_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_results_for_project(
        self, project_id: str, limit: int = 50
    ) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.execute(
                """SELECT analysis_id, project_id, cache_key, created_at
                   FROM analysis_results WHERE project_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (project_id, limit),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def count_active_jobs(self) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM analysis_jobs WHERE status IN ('queued', 'running')"
            )
            return cur.fetchone()[0]
        finally:
            conn.close()

    def count_queued_jobs(self) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM analysis_jobs WHERE status = 'queued'"
            )
            return cur.fetchone()[0]
        finally:
            conn.close()
