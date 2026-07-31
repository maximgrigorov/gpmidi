"""SQLite persistence for analysis jobs, results and source evidence.

Invariants this layer enforces (each one is a fossilised defect):

* **Terminal states are immutable.** A late worker cannot overwrite
  `failed`/`timeout`/`interrupted` with `succeeded`. Every status write carries
  `WHERE status NOT IN (<terminal>)` and reports whether it applied.
* **`started_at` is written once.** Progress updates must not slide the timeout
  deadline forward; `started_at = COALESCE(started_at, ?)`.
* **Results are published atomically against the owning job.** `store_result`
  runs in one `BEGIN IMMEDIATE` transaction that first re-reads the job and
  refuses to insert unless it is still `queued`/`running`. A job that timed out
  therefore cannot publish a cache entry afterwards.
* **Cache admission is atomic.** Looking for an existing result or active job
  and inserting a new job happen in the same immediate transaction, so two
  concurrent cold-cache requests admit exactly one computation.
* **No `INSERT OR REPLACE` on identity columns.** `analysis_results` uses
  `ON CONFLICT(cache_key) DO NOTHING` (first writer wins).
* **Deterministic ordering.** Every listing has an explicit tie-breaker.

Thread safety: one connection per operation plus a process-wide write lock.
SQLite WAL allows concurrent readers with a single writer, which suits a
single-replica analyzer.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 2

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")
ACTIVE_STATUSES = ("queued", "running")

_TERMINAL_SQL = ", ".join("?" for _ in TERMINAL_STATUSES)
_ACTIVE_SQL = ", ".join("?" for _ in ACTIVE_STATUSES)

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
    # Version 2: persisted, reusable source-side evidence
    """
    CREATE TABLE IF NOT EXISTS source_evidence (
        source_evidence_key TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_source_evidence_project
        ON source_evidence(project_id);
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

    # -- plumbing ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self, conn: sqlite3.Connection):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] or 0

        for version in range(current, SCHEMA_VERSION):
            for sql in MIGRATIONS[version].split(";"):
                sql = sql.strip()
                if sql:
                    conn.execute(sql)
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                (version + 1,),
            )
        conn.commit()

    def schema_version(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            return row[0] or 0
        finally:
            conn.close()

    def close(self):
        """No-op: connections are per-operation and always closed."""

    # -- recovery and timeouts -------------------------------------------

    def recover_interrupted_jobs(self) -> dict[str, int]:
        """Terminally recover orphaned jobs after a process restart.

        Policy (documented, terminal — no automatic retry): a job that was
        `running` or `queued` when the process died has no owner any more, so it
        becomes `interrupted` with `error_code='process_restart'`. Successful
        results are never touched, so re-requesting the same inputs is a cache
        hit; re-requesting a genuinely unfinished analysis creates a new job.
        """
        now = datetime.now(timezone.utc).isoformat()
        counts = {"running": 0, "queued": 0}
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for status in ("running", "queued"):
                    cur = conn.execute(
                        """UPDATE analysis_jobs SET status = 'interrupted',
                           error_code = 'process_restart',
                           error_message = 'Job was interrupted by process restart',
                           finished_at = ?
                           WHERE status = ?""",
                        (now, status),
                    )
                    counts[status] = cur.rowcount or 0
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return counts

    def enforce_timeouts(self) -> list[str]:
        """Fail every running job whose one-time `started_at` is too old.

        Returns the job ids that were timed out. Called continuously by the
        watchdog thread, not only at startup.
        """
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=self._job_timeout_seconds)).isoformat()
        timed_out: list[str] = []
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """SELECT job_id FROM analysis_jobs
                       WHERE status = 'running' AND started_at IS NOT NULL
                         AND started_at < ?
                       ORDER BY started_at, job_id""",
                    (cutoff,),
                ).fetchall()
                for row in rows:
                    cur = conn.execute(
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
                    if cur.rowcount:
                        timed_out.append(row["job_id"])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return timed_out

    # -- jobs -------------------------------------------------------------

    def create_job(
        self, job_id: str, analysis_id: str, project_id: str, cache_key: str
    ) -> dict:
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
        """Atomic idempotent admission by cache identity.

        In one immediate transaction: return an existing successful result, or an
        existing active job with the same identity, or count active jobs and
        insert a new one. Two concurrent cold-cache requests therefore produce
        exactly one admitted computation and one idempotent follower.

        Returns `(job_or_result, is_new)`. Raises ValueError when the queue is
        full and no existing work matches.
        """
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached = conn.execute(
                    """SELECT analysis_id, project_id FROM analysis_results
                       WHERE cache_key = ?""",
                    (cache_key,),
                ).fetchone()
                if cached:
                    conn.rollback()
                    return (
                        {
                            "analysis_id": cached["analysis_id"],
                            "project_id": cached["project_id"],
                            "status": "cached",
                        },
                        False,
                    )

                existing = conn.execute(
                    f"""SELECT * FROM analysis_jobs
                        WHERE cache_key = ? AND status IN ({_ACTIVE_SQL})
                        ORDER BY created_at ASC, job_id ASC LIMIT 1""",
                    (cache_key, *ACTIVE_STATUSES),
                ).fetchone()
                if existing:
                    conn.rollback()
                    return dict(existing), False

                active = conn.execute(
                    f"SELECT COUNT(*) FROM analysis_jobs WHERE status IN ({_ACTIVE_SQL})",
                    ACTIVE_STATUSES,
                ).fetchone()[0]
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

    def get_job(self, job_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM analysis_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_jobs_for_project(self, project_id: str, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM analysis_jobs WHERE project_id = ?
                   ORDER BY created_at DESC, job_id DESC LIMIT ?""",
                (project_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_job_status(
        self,
        job_id: str,
        status: str,
        progress_phase: str = "",
        progress_message: str = "",
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Write a status/progress update unless the job is already terminal.

        Returns True when the row was updated. A False return means the job
        reached a terminal state (typically `timeout`) while the worker was still
        running, and the worker must abandon its result.
        """
        now = datetime.now(timezone.utc).isoformat()
        assignments = [
            "status = ?",
            "progress_phase = ?",
            "progress_message = ?",
        ]
        values: list[object] = [status, progress_phase, progress_message]

        if status == "running":
            # Written once: a progress update must never extend the deadline.
            assignments.append("started_at = COALESCE(started_at, ?)")
            values.append(now)
        if status in TERMINAL_STATUSES:
            assignments.append("finished_at = ?")
            values.append(now)
        if error_code is not None:
            assignments.append("error_code = ?")
            values.append(error_code)
        if error_message is not None:
            assignments.append("error_message = ?")
            values.append(error_message)

        sql = (
            f"UPDATE analysis_jobs SET {', '.join(assignments)} "
            f"WHERE job_id = ? AND status NOT IN ({_TERMINAL_SQL})"
        )
        values.append(job_id)
        values.extend(TERMINAL_STATUSES)

        with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, values)
                conn.commit()
                return bool(cur.rowcount)
            finally:
                conn.close()

    def is_job_active(self, job_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                f"""SELECT 1 FROM analysis_jobs
                    WHERE job_id = ? AND status IN ({_ACTIVE_SQL})""",
                (job_id, *ACTIVE_STATUSES),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # -- results ----------------------------------------------------------

    def find_cached_result(self, cache_key: str) -> dict | None:
        """Successful analysis for this identity, if any.

        `analysis_results` only ever holds published successes, so a failed,
        interrupted or timed-out job can never surface as a cache hit.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM analysis_results WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def store_result(
        self,
        analysis_id: str,
        project_id: str,
        cache_key: str,
        result_json: str,
        owning_job_id: str,
    ) -> bool:
        """Publish a result only while its owning job is still active.

        One transaction re-reads the job and inserts only when it is
        `queued`/`running`, so a worker that finishes after its job timed out
        cannot publish. First writer wins on `cache_key`.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT status FROM analysis_jobs WHERE job_id = ?",
                    (owning_job_id,),
                ).fetchone()
                if row is None or row["status"] not in ACTIVE_STATUSES:
                    conn.rollback()
                    return False
                conn.execute(
                    """INSERT INTO analysis_results
                       (analysis_id, project_id, cache_key, result_json, created_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(cache_key) DO NOTHING""",
                    (analysis_id, project_id, cache_key, result_json, now),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def get_result(self, analysis_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM analysis_results WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_results_for_project(self, project_id: str, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT analysis_id, project_id, cache_key, created_at
                   FROM analysis_results WHERE project_id = ?
                   ORDER BY created_at DESC, analysis_id DESC LIMIT ?""",
                (project_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -- persisted source evidence ---------------------------------------

    def get_source_evidence(
        self, source_evidence_key: str, project_id: str
    ) -> dict | None:
        """Read persisted source evidence, scoped to the owning project.

        The `project_id` predicate is a second barrier on top of the
        project-scoped key: evidence can never be read across projects even if a
        key were ever to collide.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT * FROM source_evidence
                   WHERE source_evidence_key = ? AND project_id = ?""",
                (source_evidence_key, project_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def store_source_evidence(
        self, source_evidence_key: str, project_id: str, evidence_json: str
    ) -> bool:
        """Persist source evidence; first writer wins.

        Race-safe under concurrent requests: a second writer with the same
        identity is a no-op rather than an identity-corrupting replace.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """INSERT INTO source_evidence
                       (source_evidence_key, project_id, evidence_json, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(source_evidence_key) DO NOTHING""",
                    (source_evidence_key, project_id, evidence_json, now),
                )
                conn.commit()
                return bool(cur.rowcount)
            finally:
                conn.close()

    def count_source_evidence(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM source_evidence").fetchone()[0]
        finally:
            conn.close()

    # -- counters ---------------------------------------------------------

    def count_active_jobs(self) -> int:
        conn = self._connect()
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM analysis_jobs WHERE status IN ({_ACTIVE_SQL})",
                ACTIVE_STATUSES,
            ).fetchone()[0]
        finally:
            conn.close()

    def count_queued_jobs(self) -> int:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM analysis_jobs WHERE status = 'queued'"
            ).fetchone()[0]
        finally:
            conn.close()
