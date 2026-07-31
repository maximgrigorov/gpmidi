"""Multiple Suno MIDI consensus analysis.

Compares tempo maps from several source MIDI files, produces agreement
metrics, selects a deterministic primary, and reports conflicts.
Selection does NOT depend on upload/filesystem/hash-map order.
"""

from __future__ import annotations

from .models import (
    ConsensusDecision,
    MidiConsensus,
    SourceTempoEvidence,
    Warning,
    WarningCode,
)

TEMPO_TOLERANCE_BPM = 1.0
DURATION_TOLERANCE_SECONDS = 2.0
TS_TOLERANCE_MEASURES = 1
DOWNBEAT_TOLERANCE_SECONDS = 0.1


def _normalize_tempo_trajectory(
    evidence: SourceTempoEvidence,
) -> list[tuple[float, float]]:
    """PPQ-normalized (seconds, bpm) trajectory."""
    return [(e.seconds, e.bpm) for e in evidence.tempo_events]


def _compare_tempo_trajectories(
    a: list[tuple[float, float]],
    b: list[tuple[float, float]],
) -> dict:
    """Compare two PPQ-normalized tempo trajectories."""
    if len(a) != len(b):
        return {
            "event_count_match": False,
            "a_count": len(a),
            "b_count": len(b),
            "max_bpm_diff": None,
            "max_time_diff": None,
        }

    max_bpm = 0.0
    max_time = 0.0
    for (ta, ba), (tb, bb) in zip(a, b):
        max_bpm = max(max_bpm, abs(ba - bb))
        max_time = max(max_time, abs(ta - tb))

    return {
        "event_count_match": True,
        "a_count": len(a),
        "b_count": len(b),
        "max_bpm_diff": max_bpm,
        "max_time_diff": max_time,
    }


def _compare_time_signatures(
    a: SourceTempoEvidence,
    b: SourceTempoEvidence,
) -> dict:
    """Compare time signature sequences."""
    a_sigs = [(ts.tick, ts.numerator, ts.denominator) for ts in a.time_signatures]
    b_sigs = [(ts.tick, ts.numerator, ts.denominator) for ts in b.time_signatures]
    return {
        "match": a_sigs == b_sigs,
        "a_count": len(a_sigs),
        "b_count": len(b_sigs),
    }


def _sort_key(ev: SourceTempoEvidence) -> tuple:
    """Deterministic sort key: duration descending, then sha256 ascending."""
    return (-ev.duration_seconds, ev.sha256)


def build_consensus(
    sources: list[SourceTempoEvidence],
) -> MidiConsensus:
    """Build consensus from one or more source MIDI evidence objects.

    Selection uses documented deterministic sort key:
    longest duration first, then SHA-256 lexicographic for tie-break.
    """
    if not sources:
        raise ValueError("At least one source MIDI is required")

    if len(sources) == 1:
        s = sources[0]
        return MidiConsensus(
            decision=ConsensusDecision.SINGLE_SOURCE,
            primary_asset_link_id=s.asset_link_id,
            primary_sha256=s.sha256,
            source_count=1,
            selection_reason="Single source MIDI, no consensus needed",
        )

    sorted_sources = sorted(sources, key=_sort_key)

    agreement_metrics: dict = {}
    per_source_warnings: dict[str, list[Warning]] = {}
    conflict_regions: list[dict] = []
    has_conflict = False

    for i, src in enumerate(sorted_sources):
        src_warnings: list[Warning] = []
        if src.validation_errors:
            src_warnings.append(Warning(
                code=WarningCode.MALFORMED_MIDI,
                message=f"Source has validation errors: {src.validation_errors}",
            ))
        per_source_warnings[src.sha256] = src_warnings

    reference = sorted_sources[0]
    ref_trajectory = _normalize_tempo_trajectory(reference)

    for i in range(1, len(sorted_sources)):
        other = sorted_sources[i]
        other_trajectory = _normalize_tempo_trajectory(other)

        pair_key = f"{reference.sha256[:12]}_vs_{other.sha256[:12]}"

        tempo_cmp = _compare_tempo_trajectories(ref_trajectory, other_trajectory)
        ts_cmp = _compare_time_signatures(reference, other)
        duration_diff = abs(reference.duration_seconds - other.duration_seconds)

        agreement_metrics[pair_key] = {
            "tempo_comparison": tempo_cmp,
            "time_signature_comparison": ts_cmp,
            "duration_diff_seconds": duration_diff,
        }

        if not tempo_cmp["event_count_match"]:
            has_conflict = True
            conflict_regions.append({
                "type": "tempo_event_count_mismatch",
                "sources": [reference.sha256, other.sha256],
                "detail": tempo_cmp,
            })
        elif tempo_cmp["max_bpm_diff"] is not None and tempo_cmp["max_bpm_diff"] > TEMPO_TOLERANCE_BPM:
            has_conflict = True
            conflict_regions.append({
                "type": "tempo_value_mismatch",
                "sources": [reference.sha256, other.sha256],
                "max_bpm_diff": tempo_cmp["max_bpm_diff"],
            })

        if not ts_cmp["match"]:
            has_conflict = True
            conflict_regions.append({
                "type": "time_signature_mismatch",
                "sources": [reference.sha256, other.sha256],
            })

        if duration_diff > DURATION_TOLERANCE_SECONDS:
            has_conflict = True
            conflict_regions.append({
                "type": "duration_mismatch",
                "sources": [reference.sha256, other.sha256],
                "diff_seconds": duration_diff,
            })

    primary = sorted_sources[0]
    decision = ConsensusDecision.CONFLICT if has_conflict else ConsensusDecision.AGREED

    selection_reason = (
        f"Selected by deterministic sort: longest duration "
        f"({primary.duration_seconds:.2f}s), SHA tie-break {primary.sha256[:12]}"
    )
    if has_conflict:
        selection_reason = f"CONFLICT detected. {selection_reason}"

    return MidiConsensus(
        decision=decision,
        primary_asset_link_id=primary.asset_link_id,
        primary_sha256=primary.sha256,
        source_count=len(sources),
        agreement_metrics=agreement_metrics,
        per_source_warnings=per_source_warnings,
        conflict_regions=conflict_regions,
        selection_reason=selection_reason,
    )
