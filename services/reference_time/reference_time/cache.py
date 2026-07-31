"""Deterministic cache key computation.

Cache identity is derived only from *trusted* values: the project the caller is
authorized for, SHA-256 digests returned by the Asset API, resolved GP revision
numbers, processor/dependency versions and canonical parameters. A caller-supplied
digest never participates in cache identity.

Canonical JSON: UTF-8, sorted keys, compact separators, no NaN/Infinity.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

# Bumped whenever the meaning of a cache key changes so that results computed by
# an older identity scheme can never be served as valid hits.
CACHE_IDENTITY_VERSION = "2"


def canonical_json(obj: Any) -> str:
    """Produce deterministic JSON: sorted keys, compact, no NaN/Infinity."""

    def _check_value(v: Any) -> Any:
        if isinstance(v, float):
            if math.isnan(v):
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


def _digest(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compute_cache_key(
    project_id: str,
    gp_revision_sha256: str,
    gp_revision_number: int | None,
    source_midi_sha256s: list[str],
    audio_sha256s: list[str],
    structure_sha256: str | None,
    processor_versions: dict[str, str],
    parameters: dict,
) -> str:
    """Full analysis cache key.

    Scoped to `project_id`: two projects holding byte-identical assets must not
    share an analysis identity, otherwise project B could be handed an
    `analysis_id` owned by project A and then fail authorization on retrieval.
    """
    return _digest(
        {
            "identity_version": CACHE_IDENTITY_VERSION,
            "scope": "analysis",
            "project_id": project_id,
            "gp_revision_sha256": gp_revision_sha256,
            "gp_revision_number": gp_revision_number,
            "source_midi_sha256s": sorted(source_midi_sha256s),
            "audio_sha256s": sorted(audio_sha256s),
            "structure_sha256": structure_sha256 or "",
            "processor_versions": processor_versions,
            "parameters": parameters,
        }
    )


def _source_relevant_parameters(parameters: dict) -> dict:
    """Subset of parameters that can change source-side evidence.

    Alignment/confidence weights do not affect parsed source evidence, so they
    are excluded — otherwise a scoring tweak would needlessly invalidate every
    persisted source evidence row.
    """
    keys = ("version", "audio_max_read_seconds", "audio_analysis_sample_rate")
    return {k: parameters[k] for k in keys if k in parameters}


def compute_source_evidence_key(
    project_id: str,
    source_midi_sha256s: list[str],
    audio_sha256s: list[str],
    processor_versions: dict[str, str],
    parameters: dict,
) -> str:
    """Identity of the *source-side* evidence only.

    Deliberately excludes the GP revision so that uploading a new GP revision
    reuses persisted source MIDI/audio evidence while the GP extraction and the
    source-to-GP mapping are recomputed. It stays project-scoped so persisted
    evidence can never be read across an authorization boundary.
    """
    return _digest(
        {
            "identity_version": CACHE_IDENTITY_VERSION,
            "scope": "source_evidence",
            "project_id": project_id,
            "source_midi_sha256s": sorted(source_midi_sha256s),
            "audio_sha256s": sorted(audio_sha256s),
            "processor_versions": processor_versions,
            "parameters": _source_relevant_parameters(parameters),
        }
    )
