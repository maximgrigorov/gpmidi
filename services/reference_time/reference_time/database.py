"""SQLite persistence for analysis jobs and results.

WAL mode, migrations, deterministic ordering.
Failed/incomplete analyses are never returned as successful cache hits.
"""

from __future__ import annotations

import json
import os
import sqlite3
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
    """SQLite-backed analysis persistence."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self):
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if not cur.fetchone():
            for sql in MIGRATIONS[0].split(";"):
                sql = sql.strip()
                if sql:
                    self._conn.execute(sql)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.commit()
            return

        cur = self._conn.execute("SELECT MAX(version) FROM schema_version")
        row = cur.fetchone()
        current = row[0] if row else 0

        for version in range(current, SCHEMA_VERSION):
            for sql in MIGRATIONS[version].split(";"):
                sql = sql.strip()
                if sql:
                    self._conn.execute(sql)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (version + 1,),
            )
        self._conn.commit()

    def close(self):
        self._conn.close()

    def recover_interrupted_jobs(self):
        """Mark jobs left 'running' after restart as 'interrupted'."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE analysis_jobs SET status = 'interrupted',
               error_code = 'process_restart',
               error_message = 'Job was interrupted by process restart',
               finished_at = ?
               WHERE status = 'running'""",
            (now,),
        )
        self._conn.commit()

    def create_job(
        self,
        job_id: str,
        analysis_id: str,
        project_id: str,
        cache_key: str,
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO analysis_jobs
               (job_id, analysis_id, project_id, cache_key, status, created_at)
               VALUES (?, ?, ?, ?, 'queued', ?)""",
            (job_id, analysis_id, project_id, cache_key, now),
        )
        self._conn.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT * FROM analysis_jobs WHERE job_id = ?", (job_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_jobs_for_project(
        self, project_id: str, limit: int = 50
    ) -> list[dict]:
        cur = self._conn.execute(
            """SELECT * FROM analysis_jobs WHERE project_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (project_id, limit),
        )
        return [dict(row) for row in cur.fetchall()]

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
        self._conn.execute(
            f"UPDATE analysis_jobs SET {set_clause} WHERE job_id = ?",
            values,
        )
        self._conn.commit()

    def find_cached_result(self, cache_key: str) -> Optional[dict]:
        """Find existing successful analysis by cache key.

        Never returns failed/incomplete analyses.
        """
        cur = self._conn.execute(
            "SELECT * FROM analysis_results WHERE cache_key = ?",
            (cache_key,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def store_result(
        self,
        analysis_id: str,
        project_id: str,
        cache_key: str,
        result_json: str,
    ):
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT OR REPLACE INTO analysis_results
               (analysis_id, project_id, cache_key, result_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (analysis_id, project_id, cache_key, result_json, now),
        )
        self._conn.commit()

    def get_result(self, analysis_id: str) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT * FROM analysis_results WHERE analysis_id = ?",
            (analysis_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_results_for_project(
        self, project_id: str, limit: int = 50
    ) -> list[dict]:
        cur = self._conn.execute(
            """SELECT analysis_id, project_id, cache_key, created_at
               FROM analysis_results WHERE project_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (project_id, limit),
        )
        return [dict(row) for row in cur.fetchall()]

    def count_active_jobs(self) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM analysis_jobs WHERE status IN ('queued', 'running')"
        )
        return cur.fetchone()[0]

    def count_queued_jobs(self) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM analysis_jobs WHERE status = 'queued'"
        )
        return cur.fetchone()[0]
