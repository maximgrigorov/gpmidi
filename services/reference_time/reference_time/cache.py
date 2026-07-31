"""Deterministic cache key computation.

Cache key includes exact hashes/IDs for all inputs, processor versions,
and canonical parameters. Canonical JSON: UTF-8, sorted keys, compact
separators, no NaN/Infinity.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Produce deterministic JSON: sorted keys, compact, no NaN/Infinity."""

    def _check_value(v: Any) -> Any:
        if isinstance(v, float):
            if v != v:  # NaN check
                raise ValueError("NaN is not allowed in canonical JSON")
            if v == float("inf") or v == float("-inf"):
                raise ValueError("Infinity is not allowed in canonical JSON")
        elif isinstance(v, dict):
            return {k: _check_value(val) for k, val in v.items()}
        elif isinstance(v, (list, tuple)):
            return [_check_value(item) for item in v]
        return v

    checked = _check_value(obj)
    return json.dumps(
        checked,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def compute_cache_key(
    gp_revision_sha256: str,
    source_midi_sha256s: list[str],
    audio_sha256s: list[str],
    structure_sha256: str | None,
    processor_versions: dict[str, str],
    parameters: dict,
) -> str:
    """Compute deterministic cache key for analysis inputs.

    Changes to any input identity, processor version, or parameter
    produce a different cache key.
    """
    key_data = {
        "gp_revision_sha256": gp_revision_sha256,
        "source_midi_sha256s": sorted(source_midi_sha256s),
        "audio_sha256s": sorted(audio_sha256s),
        "structure_sha256": structure_sha256 or "",
        "processor_versions": processor_versions,
        "parameters": parameters,
    }

    key_json = canonical_json(key_data)
    return hashlib.sha256(key_json.encode("utf-8")).hexdigest()


def compute_source_evidence_cache_key(
    source_midi_sha256s: list[str],
    processor_versions: dict[str, str],
) -> str:
    """Cache key for source MIDI evidence (reusable across GP revisions)."""
    key_data = {
        "source_midi_sha256s": sorted(source_midi_sha256s),
        "processor_versions": processor_versions,
    }
    key_json = canonical_json(key_data)
    return hashlib.sha256(key_json.encode("utf-8")).hexdigest()
