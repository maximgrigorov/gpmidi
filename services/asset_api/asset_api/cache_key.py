"""Cache key computation for deterministic pipeline invalidation."""

from __future__ import annotations

import hashlib
import json


def canonical_json(obj: dict) -> bytes:
    """UTF-8 JSON with sorted keys and compact separators. No NaN/Infinity."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def cache_key(
    input_sha256: str,
    processor_name: str,
    processor_version: str,
    checkpoint_sha256: str | None = None,
    parameters: dict | None = None,
) -> str:
    """Compute a cache key as SHA-256 of canonical JSON representation."""
    payload = {
        "input_sha256": input_sha256,
        "processor_name": processor_name,
        "processor_version": processor_version,
        "checkpoint_sha256": checkpoint_sha256 or "",
        "parameters": parameters or {},
    }
    raw = canonical_json(payload)
    return hashlib.sha256(raw).hexdigest()
