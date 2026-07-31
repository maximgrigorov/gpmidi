"""Configuration for reference-time service."""

from __future__ import annotations

import os

DATA_ROOT = os.environ.get("RT_DATA_ROOT", "/var/lib/reference-time")
ASSET_API_URL = os.environ.get("ASSET_API_URL", "http://asset-api.gpmidi-ml.svc.cluster.local:8000")
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "").split(",") if os.environ.get("CORS_ORIGINS") else []

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
MAX_QUEUE_LENGTH = int(os.environ.get("MAX_QUEUE_LENGTH", "10"))
JOB_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECONDS", "300"))

DB_PATH = os.path.join(DATA_ROOT, "db", "reference_time.sqlite3")
