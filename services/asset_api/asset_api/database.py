"""SQLite database layer with WAL mode and explicit schema."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

_SCHEMA_VERSION = 2

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS assets (
    sha256 TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    original_name TEXT,
    blob_relpath TEXT NOT NULL,
    created_at TEXT NOT NULL,
    integrity TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS project_assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    asset_sha256 TEXT NOT NULL REFERENCES assets(sha256),
    role TEXT NOT NULL,
    label TEXT,
    original_filename TEXT NOT NULL,
    created_at TEXT NOT NULL,
    provenance TEXT,
    UNIQUE(project_id, asset_sha256, role)
);

CREATE TABLE IF NOT EXISTS gp_revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    asset_sha256 TEXT NOT NULL REFERENCES assets(sha256),
    original_filename TEXT NOT NULL,
    created_at TEXT NOT NULL,
    note TEXT,
    UNIQUE(project_id, revision)
);

CREATE TABLE IF NOT EXISTS upload_tickets (
    ticket_hash TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    max_bytes INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);
"""


def _db_path() -> Path:
    from .config import DB_PATH
    return DB_PATH


def init_db(db_path: Path | None = None) -> None:
    """Create tables if they don't exist. Called at startup."""
    p = db_path or _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.executescript(_SCHEMA_SQL)
    cur = conn.execute("SELECT version FROM schema_version")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
        conn.commit()
    else:
        current_ver = row[0]
        if current_ver < 2:
            _migrate_v1_to_v2(conn)
        conn.execute("UPDATE schema_version SET version=?", (_SCHEMA_VERSION,))
        conn.commit()
    conn.close()


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add status column if missing (safe for existing DBs)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(upload_tickets)").fetchall()]
    if "status" not in cols:
        conn.execute("ALTER TABLE upload_tickets ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
    # Invalidate any pending tickets for deleted projects
    conn.execute("""
        UPDATE upload_tickets SET status='expired'
        WHERE status='pending'
          AND project_id NOT IN (SELECT id FROM projects)
    """)


@contextmanager
def get_db(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    """Provide a database connection with WAL and FK enforcement."""
    p = db_path or _db_path()
    conn = sqlite3.connect(str(p), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()
