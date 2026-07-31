"""Configuration for reference-time service."""

from __future__ import annotations

import os

DATA_ROOT = os.environ.get("RT_DATA_ROOT", "/var/lib/reference-time")
ASSET_API_URL = os.environ.get(
    "ASSET_API_URL", "http://asset-api.gpmidi-ml.svc.cluster.local:8000"
)
CORS_ORIGINS = (
    os.environ.get("CORS_ORIGINS", "").split(",")
    if os.environ.get("CORS_ORIGINS")
    else []
)

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
MAX_QUEUE_LENGTH = int(os.environ.get("MAX_QUEUE_LENGTH", "10"))
JOB_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECONDS", "300"))
# The watchdog runs for the whole life of the process, not only at startup.
TIMEOUT_WATCHDOG_INTERVAL_SECONDS = float(
    os.environ.get("TIMEOUT_WATCHDOG_INTERVAL_SECONDS", "5")
)
WORKER_SHUTDOWN_GRACE_SECONDS = float(
    os.environ.get("WORKER_SHUTDOWN_GRACE_SECONDS", "10")
)

DB_PATH = os.path.join(DATA_ROOT, "db", "reference_time.sqlite3")
# Temporary download scopes live under the pod's own bounded volume.
TMP_ROOT = os.environ.get("RT_TMP_ROOT", os.path.join(DATA_ROOT, "tmp"))

# --- bounded download limits, per asset role family -----------------------
MAX_GP_BYTES = int(os.environ.get("MAX_GP_BYTES", str(32 * 1024 * 1024)))
MAX_MIDI_BYTES = int(os.environ.get("MAX_MIDI_BYTES", str(8 * 1024 * 1024)))
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_BYTES", str(512 * 1024 * 1024)))
MAX_STRUCTURE_BYTES = int(os.environ.get("MAX_STRUCTURE_BYTES", str(1024 * 1024)))

MAX_GP_DOWNLOAD_SECONDS = float(os.environ.get("MAX_GP_DOWNLOAD_SECONDS", "60"))
MAX_MIDI_DOWNLOAD_SECONDS = float(os.environ.get("MAX_MIDI_DOWNLOAD_SECONDS", "30"))
MAX_AUDIO_DOWNLOAD_SECONDS = float(os.environ.get("MAX_AUDIO_DOWNLOAD_SECONDS", "120"))
MAX_STRUCTURE_DOWNLOAD_SECONDS = float(
    os.environ.get("MAX_STRUCTURE_DOWNLOAD_SECONDS", "15")
)

CONNECT_TIMEOUT_SECONDS = float(os.environ.get("CONNECT_TIMEOUT_SECONDS", "5"))
READ_TIMEOUT_SECONDS = float(os.environ.get("READ_TIMEOUT_SECONDS", "30"))
WRITE_TIMEOUT_SECONDS = float(os.environ.get("WRITE_TIMEOUT_SECONDS", "30"))
POOL_TIMEOUT_SECONDS = float(os.environ.get("POOL_TIMEOUT_SECONDS", "10"))
METADATA_TIMEOUT_SECONDS = float(os.environ.get("METADATA_TIMEOUT_SECONDS", "15"))

# --- bounded audio analysis ----------------------------------------------
AUDIO_MAX_READ_SECONDS = float(os.environ.get("AUDIO_MAX_READ_SECONDS", "600"))
AUDIO_MAX_ONSETS_PERSISTED = int(os.environ.get("AUDIO_MAX_ONSETS_PERSISTED", "512"))
AUDIO_MAX_DOWNBEATS_PERSISTED = int(
    os.environ.get("AUDIO_MAX_DOWNBEATS_PERSISTED", "256")
)
