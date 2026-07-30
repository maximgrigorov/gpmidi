"""Asset API configuration via environment variables."""

from __future__ import annotations

import os
from pathlib import Path


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)))


DATA_ROOT = Path(os.environ.get("ASSET_DATA_ROOT", "/var/lib/gpmidi"))
DB_PATH = DATA_ROOT / "db" / "projects.sqlite3"
BLOBS_DIR = DATA_ROOT / "blobs" / "sha256"
TMP_UPLOADS_DIR = DATA_ROOT / "tmp" / "uploads"

CORS_ORIGINS: list[str] = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "").split(",")
    if o.strip()
]

MAX_AUDIO_BYTES = _env_int("MAX_AUDIO_BYTES", 1 * 1024 * 1024 * 1024)  # 1 GiB
MAX_MIDI_BYTES = _env_int("MAX_MIDI_BYTES", 100 * 1024 * 1024)  # 100 MiB
MAX_GP_BYTES = _env_int("MAX_GP_BYTES", 100 * 1024 * 1024)  # 100 MiB
MAX_TEXT_BYTES = _env_int("MAX_TEXT_BYTES", 5 * 1024 * 1024)  # 5 MiB

TICKET_TTL_SECONDS = _env_int("TICKET_TTL_SECONDS", 900)  # 15 min

UPLOAD_RATE_LIMIT_PER_MIN = _env_int("UPLOAD_RATE_LIMIT_PER_MIN", 30)

PROJECT_NAME_MAX_LEN = 120
PROJECT_DESC_MAX_LEN = 4000
