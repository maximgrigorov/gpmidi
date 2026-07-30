"""Blob storage: content-addressed write/read with streaming SHA-256."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def _blobs_dir() -> Path:
    from .config import BLOBS_DIR
    return BLOBS_DIR


def _tmp_dir() -> Path:
    from .config import TMP_UPLOADS_DIR
    return TMP_UPLOADS_DIR


def blob_relpath(sha256_hex: str) -> str:
    """Derive relative path: sha256/ab/cd/<full_hash>."""
    h = sha256_hex.lower()
    return f"{h[:2]}/{h[2:4]}/{h}"


def blob_abspath(sha256_hex: str, blobs_dir: Path | None = None) -> Path:
    d = blobs_dir or _blobs_dir()
    return d / blob_relpath(sha256_hex)


def ensure_dirs(blobs_dir: Path | None = None, tmp_dir: Path | None = None) -> None:
    (blobs_dir or _blobs_dir()).mkdir(parents=True, exist_ok=True)
    (tmp_dir or _tmp_dir()).mkdir(parents=True, exist_ok=True)


class StreamingHashWriter:
    """Write to a temp file while computing SHA-256 and tracking size.

    Temp file lives on the same filesystem as blobs for atomic rename.
    """

    def __init__(self, tmp_dir: Path | None = None):
        self._tmp_dir = tmp_dir or _tmp_dir()
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._hasher = hashlib.sha256()
        self._size = 0
        self._fd = tempfile.NamedTemporaryFile(
            dir=str(self._tmp_dir), delete=False, prefix="upload_"
        )
        self._path = Path(self._fd.name)
        self._closed = False

    @property
    def size(self) -> int:
        return self._size

    @property
    def tmp_path(self) -> Path:
        return self._path

    def write(self, chunk: bytes) -> None:
        self._fd.write(chunk)
        self._hasher.update(chunk)
        self._size += len(chunk)

    def finalize(self) -> str:
        """Close temp file and return hex digest."""
        self._fd.flush()
        os.fsync(self._fd.fileno())
        self._fd.close()
        self._closed = True
        return self._hasher.hexdigest()

    def abort(self) -> None:
        """Close and remove temp file."""
        if not self._closed:
            self._fd.close()
            self._closed = True
        if self._path.exists():
            self._path.unlink()

    def commit(self, sha256_hex: str, blobs_dir: Path | None = None) -> Path:
        """Atomic move from tmp to content-addressed location.

        Returns the final blob path. If the blob already exists (dedup),
        the temp is removed and the existing path is returned.
        """
        dest = blob_abspath(sha256_hex, blobs_dir)
        if dest.exists():
            self._path.unlink()
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(self._path), str(dest))
        return dest


def cleanup_stale_temps(tmp_dir: Path | None = None, max_age_seconds: int = 3600) -> int:
    """Remove temp files older than max_age_seconds. Returns count removed."""
    import time
    d = tmp_dir or _tmp_dir()
    if not d.exists():
        return 0
    now = time.time()
    removed = 0
    for f in d.iterdir():
        if f.is_file() and f.name.startswith("upload_"):
            if now - f.stat().st_mtime > max_age_seconds:
                f.unlink()
                removed += 1
    return removed
