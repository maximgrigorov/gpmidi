"""Multiple Suno MIDI consensus analysis.

Compares tempo maps from several source MIDI files, produces agreement
metrics, selects a deterministic primary, and reports conflicts.
Selection does NOT depend on upload/filesystem/hash-map order.

Comparison is PPQ/time-normalized: trajectories are resampled onto a
common time grid so different-length MIDIs or different PPQs can be
compared without requiring identical event counts.
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
BOUNDARY_TOLERANCE_SECONDS = 0.5
TRAJECTORY_SAMPLE_COUNT = 100


def _normalize_tempo_trajectory(
    evidence: SourceTempoEvidence,
) -> list[tuple[float, float]]:
    """PPQ-normalized (seconds, bpm) trajectory."""
    return [(e.seconds, e.bpm) for e in evidence.tempo_events]


def _sample_bpm_at_time(trajectory: list[tuple[float, float]], t: float) -> float:
    """Interpolate BPM at time t from a trajectory of (seconds, bpm) events.

    Uses step-function semantics: the BPM holds until the next event.
    """
    if not trajectory:
        return 120.0
    if t <= trajectory[0][0]:
        return trajectory[0][1]
    for i in range(len(trajectory) - 1):
        if trajectory[i][0] <= t < trajectory[i + 1][0]:
            return trajectory[i][1]
    return trajectory[-1][1]


def _trajectory_boundaries(trajectory: list[tuple[float, float]]) -> list[float]:
    """Extract tempo change boundary times."""
    if len(trajectory) <= 1:
        return []
    boundaries = []
    for i in range(1, len(trajectory)):
        if abs(trajectory[i][1] - trajectory[i - 1][1]) > 0.01:
            boundaries.append(trajectory[i][0])
    return boundaries


def _compare_tempo_trajectories(
    a: list[tuple[float, float]],
    b: list[tuple[float, float]],
    duration_a: float,
    duration_b: float,
) -> dict:
    """Compare two PPQ-normalized tempo trajectories by resampling onto
    a common normalized time grid. Reports max/mean BPM deviation,
    boundary alignment, and regional conflicts.
    """
    max_dur = max(duration_a, duration_b, 0.001)
    n_samples = TRAJECTORY_SAMPLE_COUNT
    sample_times = [i * max_dur / n_samples for i in range(n_samples + 1)]

    max_bpm_diff = 0.0
    sum_bpm_diff = 0.0
    conflict_segments: list[dict] = []
    segment_start = None

    for t in sample_times:
        bpm_a = _sample_bpm_at_time(a, t)
        bpm_b = _sample_bpm_at_time(b, t)
        diff = abs(bpm_a - bpm_b)
        max_bpm_diff = max(max_bpm_diff, diff)
        sum_bpm_diff += diff

        if diff > TEMPO_TOLERANCE_BPM:
            if segment_start is None:
                segment_start = t
        else:
            if segment_start is not None:
                conflict_segments.append({
                    "start_seconds": round(segment_start, 3),
                    "end_seconds": round(t, 3),
                    "max_bpm_diff": round(max_bpm_diff, 3),
                })
                segment_start = None

    if segment_start is not None:
        conflict_segments.append({
            "start_seconds": round(segment_start, 3),
            "end_seconds": round(max_dur, 3),
            "max_bpm_diff": round(max_bpm_diff, 3),
        })

    mean_bpm_diff = sum_bpm_diff / (n_samples + 1)

    # Boundary alignment: check if tempo change times are similar
    bounds_a = _trajectory_boundaries(a)
    bounds_b = _trajectory_boundaries(b)
    matched_boundaries = 0
    unmatched_boundaries = []
    for ba in bounds_a:
        if any(abs(ba - bb) < BOUNDARY_TOLERANCE_SECONDS for bb in bounds_b):
            matched_boundaries += 1
        else:
            unmatched_boundaries.append(round(ba, 3))
    for bb in bounds_b:
        if not any(abs(bb - ba) < BOUNDARY_TOLERANCE_SECONDS for ba in bounds_a):
            unmatched_boundaries.append(round(bb, 3))

    total_boundaries = len(bounds_a) + len(bounds_b)
    if total_boundaries == 0:
        boundary_agreement = 1.0
    else:
        boundary_agreement = (2.0 * matched_boundaries) / total_boundaries

    return {
        "event_count_a": len(a),
        "event_count_b": len(b),
        "max_bpm_diff": round(max_bpm_diff, 3),
        "mean_bpm_diff": round(mean_bpm_diff, 3),
        "boundary_agreement": round(boundary_agreement, 3),
        "unmatched_boundaries": unmatched_boundaries,
        "conflict_segments": conflict_segments,
    }


def _compare_time_signatures(
    a: SourceTempoEvidence,
    b: SourceTempoEvidence,
) -> dict:
    """Compare time signature sequences."""
    a_sigs = [(ts.tick, ts.numerator, ts.denominator) for ts in a.time_signatures]
    b_sigs = [(ts.tick, ts.numerator, ts.denominator) for ts in b.time_signatures]

    # Normalize by comparing (numerator, denominator) sequences only
    a_norms = [(n, d) for _, n, d in a_sigs]
    b_norms = [(n, d) for _, n, d in b_sigs]

    return {
        "match": a_norms == b_norms,
        "a_count": len(a_sigs),
        "b_count": len(b_sigs),
        "a_signatures": a_norms,
        "b_signatures": b_norms,
    }


def _measure_starts(evidence: SourceTempoEvidence) -> list[float]:
    """Absolute measure/downbeat start times derived from tempo + time signature.

    Computed from evidence only (no MIDI bytes needed) so consensus can compare
    bar grids, not just tempo-change instants.
    """
    from .midi_tempo import build_source_measures

    return [m.seconds_start for m in build_source_measures(evidence)]


def _compare_measure_boundaries(
    a: SourceTempoEvidence,
    b: SourceTempoEvidence,
) -> dict:
    """Compare downbeat/measure boundaries over the overlapping prefix.

    Different total lengths are expected and are not by themselves a conflict:
    only boundaries that both sources claim are compared.
    """
    starts_a = _measure_starts(a)
    starts_b = _measure_starts(b)
    overlap = min(len(starts_a), len(starts_b))

    max_diff = 0.0
    mismatched: list[int] = []
    for i in range(overlap):
        diff = abs(starts_a[i] - starts_b[i])
        max_diff = max(max_diff, diff)
        if diff > BOUNDARY_TOLERANCE_SECONDS:
            mismatched.append(i)

    return {
        "measure_count_a": len(starts_a),
        "measure_count_b": len(starts_b),
        "compared_boundaries": overlap,
        "max_boundary_diff_seconds": round(max_diff, 3),
        "mismatched_measure_indices": mismatched,
        "aligned": not mismatched,
    }


def _compare_part_entries(
    a: SourceTempoEvidence,
    b: SourceTempoEvidence,
) -> dict:
    """Compare first sounding notes as part-entry diagnostics only.

    Separate stems naturally enter at different song positions.  This signal
    must never be promoted to a disagreement about the shared tempo grid.
    """
    a_first = a.first_event_seconds or 0.0
    b_first = b.first_event_seconds or 0.0
    diff = abs(a_first - b_first)
    return {
        "a_first_seconds": a_first,
        "b_first_seconds": b_first,
        "diff_seconds": round(diff, 3),
        "aligned": diff < BOUNDARY_TOLERANCE_SECONDS,
    }


def _compare_timeline_origins(
    a: SourceTempoEvidence,
    b: SourceTempoEvidence,
) -> dict:
    """Compare explicit timeline origins independently of first notes."""
    a_origin = a.timeline_origin_seconds
    b_origin = b.timeline_origin_seconds
    diff = abs(a_origin - b_origin)
    return {
        "a_origin_seconds": a_origin,
        "b_origin_seconds": b_origin,
        "diff_seconds": round(diff, 3),
        "aligned": diff < BOUNDARY_TOLERANCE_SECONDS,
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

    for src in sorted_sources:
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

        tempo_cmp = _compare_tempo_trajectories(
            ref_trajectory, other_trajectory,
            reference.duration_seconds, other.duration_seconds,
        )
        ts_cmp = _compare_time_signatures(reference, other)
        part_entry_cmp = _compare_part_entries(reference, other)
        timeline_origin_cmp = _compare_timeline_origins(reference, other)
        measure_cmp = _compare_measure_boundaries(reference, other)
        duration_diff = abs(reference.duration_seconds - other.duration_seconds)

        agreement_metrics[pair_key] = {
            "tempo_comparison": tempo_cmp,
            "time_signature_comparison": ts_cmp,
            # Keep the legacy key as a compatibility alias, but its semantics
            # are now the actual timeline origin rather than first note onset.
            "preroll_comparison": timeline_origin_cmp,
            "timeline_origin_comparison": timeline_origin_cmp,
            "part_entry_comparison": part_entry_cmp,
            "measure_boundary_comparison": measure_cmp,
            "duration_diff_seconds": round(duration_diff, 3),
        }

        # Trajectory conflict: resampled BPM differs beyond tolerance
        if tempo_cmp["max_bpm_diff"] > TEMPO_TOLERANCE_BPM:
            has_conflict = True
            for seg in tempo_cmp["conflict_segments"]:
                conflict_regions.append({
                    "type": "tempo_trajectory_mismatch",
                    "sources": [reference.sha256, other.sha256],
                    "region": seg,
                })

        # Boundary conflict: tempo change points don't align
        if tempo_cmp["boundary_agreement"] < 0.5:
            has_conflict = True
            conflict_regions.append({
                "type": "boundary_mismatch",
                "sources": [reference.sha256, other.sha256],
                "unmatched": tempo_cmp["unmatched_boundaries"],
            })

        # Time signature mismatch
        if not ts_cmp["match"]:
            has_conflict = True
            conflict_regions.append({
                "type": "time_signature_mismatch",
                "sources": [reference.sha256, other.sha256],
                "detail": ts_cmp,
            })

        # Measure/downbeat grid mismatch over the shared prefix
        if not measure_cmp["aligned"]:
            has_conflict = True
            conflict_regions.append({
                "type": "measure_boundary_mismatch",
                "sources": [reference.sha256, other.sha256],
                "detail": measure_cmp,
            })

        # Duration mismatch
        if duration_diff > DURATION_TOLERANCE_SECONDS:
            has_conflict = True
            conflict_regions.append({
                "type": "duration_mismatch",
                "sources": [reference.sha256, other.sha256],
                "diff_seconds": round(duration_diff, 3),
            })

        # Only explicit timeline origins can conflict. Different first-note
        # times are expected across independent stems and stay diagnostic.
        if not timeline_origin_cmp["aligned"]:
            has_conflict = True
            conflict_regions.append({
                "type": "timeline_origin_mismatch",
                "sources": [reference.sha256, other.sha256],
                "detail": timeline_origin_cmp,
            })

    primary = sorted_sources[0]
    decision = ConsensusDecision.CONFLICT if has_conflict else ConsensusDecision.AGREED

    selection_reason = (
        f"Selected by deterministic sort: longest duration "
        f"({primary.duration_seconds:.2f}s), SHA tie-break {primary.sha256[:12]}"
    )
    if has_conflict:
        n_regions = len(conflict_regions)
        selection_reason = (
            f"CONFLICT detected ({n_regions} region(s)). {selection_reason}"
        )

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


def conflict_time_regions(
    consensus: MidiConsensus, timeline_end_seconds: float
) -> list[dict]:
    """Time intervals, in source seconds, where the sources genuinely disagree.

    Only region-bearing conflicts contribute a localized interval. Whole-file
    conflicts (time signature, duration, pre-roll, measure grid) are not
    localizable, so they cover the whole timeline. All values stay finite so
    they can be serialized into canonical JSON.
    """
    end_cap = max(0.0, float(timeline_end_seconds))
    regions: list[dict] = []
    for entry in consensus.conflict_regions:
        region = entry.get("region")
        if isinstance(region, dict) and "start_seconds" in region:
            regions.append({
                "start_seconds": float(region["start_seconds"]),
                "end_seconds": float(region["end_seconds"]),
                "type": entry.get("type", "unknown"),
            })
        else:
            regions.append({
                "start_seconds": 0.0,
                "end_seconds": end_cap,
                "type": entry.get("type", "unknown"),
            })
    return regions
