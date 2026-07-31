"""Source-to-GP deterministic sequence alignment.

Implements a dynamic-programming alignment of source measures to GP measures
with explicit scoring, monotonicity constraints, gap support, and
user anchor handling.

Scoring constants live in a versioned parameters dict included in the cache key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import (
    GPMeasure,
    MappingAlternative,
    MappingType,
    MeasureMapping,
    SourceMeasure,
    Warning,
    WarningCode,
)

DEFAULT_PARAMS = {
    "version": "2.0.0",
    "ts_match_score": 10.0,
    "ts_mismatch_penalty": -5.0,
    "marker_anchor_bonus": 8.0,
    "user_anchor_score": 100.0,
    "gap_penalty": -3.0,
    "duration_weight": 2.0,
    "duration_tolerance_ratio": 0.3,
    "density_weight": 1.0,
    "density_tolerance": 0.5,
    "repeat_bonus": 2.0,
    "audio_corroboration_bonus": 3.0,
    "confidence_ts_match": 0.25,
    "confidence_duration": 0.25,
    "confidence_marker": 0.15,
    "confidence_monotonic": 0.15,
    "confidence_density": 0.10,
    "confidence_audio": 0.10,
    "alternative_threshold": 2.0,
}


@dataclass
class AnchorConstraint:
    """User-provided anchor: source measure X must map to GP measure Y."""
    source_measure_index: int
    gp_measure_index: int
    label: Optional[str] = None


@dataclass
class AlignmentResult:
    mappings: list[MeasureMapping] = field(default_factory=list)
    global_confidence: float = 0.0
    warnings: list[Warning] = field(default_factory=list)


def _time_sig_score(
    src: SourceMeasure,
    gp: GPMeasure,
    params: dict,
) -> float:
    if src.numerator == gp.numerator and src.denominator == gp.denominator:
        return params["ts_match_score"]
    return params["ts_mismatch_penalty"]


def _duration_score(
    src: SourceMeasure,
    gp: GPMeasure,
    all_source: list[SourceMeasure],
    all_gp: list[GPMeasure],
    params: dict,
) -> float:
    """Score based on relative measure duration similarity.

    Compares the fraction of total source duration to the fraction of total
    GP ticks. If both measures represent a similar proportion of their
    respective totals, score positively.
    """
    src_dur = src.seconds_end - src.seconds_start
    gp_ticks = gp.tick_end - gp.tick_start

    if not all_gp or not all_source:
        return 0.0

    total_gp_ticks = all_gp[-1].tick_end - all_gp[0].tick_start
    if total_gp_ticks <= 0:
        return 0.0

    total_src_dur = max(0.001, sum(
        m.seconds_end - m.seconds_start for m in all_source
    ))

    gp_frac = gp_ticks / total_gp_ticks
    src_frac = src_dur / total_src_dur

    if src_frac <= 0:
        return 0.0

    ratio = src_frac / max(0.001, gp_frac) if gp_frac > 0 else 0.0
    tolerance = params["duration_tolerance_ratio"]
    weight = params["duration_weight"]

    deviation = abs(1.0 - ratio)
    if deviation < tolerance:
        return weight * (1.0 - deviation / tolerance)
    return -weight * min(1.0, deviation)


def _density_score(
    src: SourceMeasure,
    params: dict,
) -> float:
    """Score nudge based on note density — denser measures get a small bonus."""
    density = getattr(src, "note_density", 0.0) or 0.0
    if density <= 0:
        return 0.0
    weight = params.get("density_weight", 1.0)
    tolerance = params.get("density_tolerance", 0.5)
    return weight * min(1.0, density * tolerance)


def _repeat_score(
    gp: GPMeasure,
    params: dict,
) -> float:
    """Bonus for GP repeat boundaries."""
    bonus = 0.0
    if gp.has_repeat_open or gp.has_repeat_close:
        bonus += params["repeat_bonus"]
    return bonus


def _audio_corroboration_score(
    src: SourceMeasure,
    params: dict,
) -> float:
    """Bonus if audio evidence corroborates this measure's downbeat."""
    if src.audio_downbeat_evidence is not None and src.audio_downbeat_evidence > 0.5:
        return params.get("audio_corroboration_bonus", 3.0)
    return 0.0


def _marker_score(
    gp: GPMeasure,
    params: dict,
) -> float:
    if gp.marker_text or gp.section_text:
        return params["marker_anchor_bonus"]
    return 0.0


def _compute_cell_score(
    src: SourceMeasure,
    gp: GPMeasure,
    all_source: list[SourceMeasure],
    all_gp: list[GPMeasure],
    params: dict,
) -> float:
    """Composite score for matching src to gp."""
    score = 0.0
    score += _time_sig_score(src, gp, params)
    score += _duration_score(src, gp, all_source, all_gp, params)
    score += _density_score(src, params)
    score += _repeat_score(gp, params)
    score += _audio_corroboration_score(src, params)
    score += _marker_score(gp, params)
    return score


def _build_anchor_map(
    anchors: list[AnchorConstraint],
    source_measures: list[SourceMeasure],
    gp_measures: list[GPMeasure],
) -> dict[int, int]:
    """Build and validate anchor map: source_idx -> gp_idx."""
    anchor_map: dict[int, int] = {}
    seen_gp: dict[int, int] = {}

    for a in sorted(anchors, key=lambda x: x.source_measure_index):
        if a.source_measure_index < 0 or a.source_measure_index >= len(source_measures):
            raise ValueError(
                f"Anchor source index {a.source_measure_index} out of range "
                f"[0, {len(source_measures)})"
            )
        if a.gp_measure_index < 0 or a.gp_measure_index >= len(gp_measures):
            raise ValueError(
                f"Anchor GP index {a.gp_measure_index} out of range "
                f"[0, {len(gp_measures)})"
            )
        if a.source_measure_index in anchor_map:
            existing_gp = anchor_map[a.source_measure_index]
            if existing_gp != a.gp_measure_index:
                raise ValueError(
                    f"Conflicting anchors: source {a.source_measure_index} "
                    f"mapped to both GP {existing_gp} and GP {a.gp_measure_index}"
                )
        if a.gp_measure_index in seen_gp:
            existing_src = seen_gp[a.gp_measure_index]
            if existing_src != a.source_measure_index:
                raise ValueError(
                    f"Conflicting anchors: GP {a.gp_measure_index} "
                    f"mapped from both source {existing_src} and source {a.source_measure_index}"
                )
        anchor_map[a.source_measure_index] = a.gp_measure_index
        seen_gp[a.gp_measure_index] = a.source_measure_index

    sorted_anchors = sorted(anchor_map.items())
    for i in range(1, len(sorted_anchors)):
        prev_src, prev_gp = sorted_anchors[i - 1]
        curr_src, curr_gp = sorted_anchors[i]
        if curr_gp <= prev_gp:
            raise ValueError(
                f"Non-monotonic anchors: source {prev_src}->GP {prev_gp}, "
                f"source {curr_src}->GP {curr_gp}"
            )

    return anchor_map


def _compute_confidence(
    src: SourceMeasure,
    gp: Optional[GPMeasure],
    mapping_type: MappingType,
    all_source: list[SourceMeasure],
    all_gp: list[GPMeasure],
    params: dict,
) -> float:
    """Compute calibrated confidence in [0,1] from actual score components."""
    if mapping_type == MappingType.SOURCE_GAP:
        return 0.1
    if mapping_type == MappingType.GP_GAP:
        return 0.1
    if gp is None:
        return 0.1

    confidence = 0.0

    # Time-signature match component
    if src.numerator == gp.numerator and src.denominator == gp.denominator:
        confidence += params["confidence_ts_match"]

    # Duration component — fraction similarity
    src_dur = src.seconds_end - src.seconds_start
    total_src_dur = max(0.001, sum(m.seconds_end - m.seconds_start for m in all_source))
    total_gp_ticks = (all_gp[-1].tick_end - all_gp[0].tick_start) if all_gp else 1
    gp_ticks = gp.tick_end - gp.tick_start
    if total_gp_ticks > 0 and total_src_dur > 0:
        src_frac = src_dur / total_src_dur
        gp_frac = gp_ticks / total_gp_ticks
        ratio = src_frac / max(0.001, gp_frac) if gp_frac > 0 else 0.0
        tolerance = params["duration_tolerance_ratio"]
        deviation = abs(1.0 - ratio)
        if deviation < tolerance:
            confidence += params["confidence_duration"] * (1.0 - deviation / tolerance)

    # Marker component
    if gp.marker_text or gp.section_text:
        confidence += params["confidence_marker"]

    # Monotonicity is always satisfied by DP construction
    confidence += params["confidence_monotonic"]

    # Density component
    density = getattr(src, "note_density", 0.0) or 0.0
    if density > 0:
        confidence += params["confidence_density"]

    # Audio corroboration component
    if src.audio_downbeat_evidence is not None and src.audio_downbeat_evidence > 0.5:
        confidence += params["confidence_audio"]

    if mapping_type == MappingType.AMBIGUOUS:
        confidence *= 0.5

    return min(1.0, max(0.0, confidence))


def _compute_normalized_positions(
    src: SourceMeasure,
    gp: Optional[GPMeasure],
    all_source: list[SourceMeasure],
    all_gp: list[GPMeasure],
) -> tuple[float, float]:
    """Compute normalized [0,1] position of this mapping within the full timeline."""
    if not all_source or not all_gp:
        return 0.0, 1.0

    total_src_dur = max(0.001, all_source[-1].seconds_end - all_source[0].seconds_start)
    src_start_frac = (src.seconds_start - all_source[0].seconds_start) / total_src_dur
    src_end_frac = (src.seconds_end - all_source[0].seconds_start) / total_src_dur

    return max(0.0, min(1.0, src_start_frac)), max(0.0, min(1.0, src_end_frac))


def align_measures(
    source_measures: list[SourceMeasure],
    gp_measures: list[GPMeasure],
    anchors: list[AnchorConstraint] | None = None,
    params: dict | None = None,
    consensus_has_conflict: bool = False,
) -> AlignmentResult:
    """Align source measures to GP measures using dynamic programming.

    Monotonic alignment with gap support, anchor constraints,
    and explicit scoring. Produces explicit MeasureMapping records for
    both source gaps and GP gaps.
    """
    if params is None:
        params = DEFAULT_PARAMS.copy()

    warnings: list[Warning] = []

    if not source_measures:
        return AlignmentResult(
            mappings=[],
            global_confidence=0.0,
            warnings=[Warning(
                code=WarningCode.LOW_CONFIDENCE,
                message="No source measures to align",
            )],
        )

    if not gp_measures:
        mappings = []
        for src in source_measures:
            n_start, n_end = _compute_normalized_positions(
                src, None, source_measures, gp_measures
            )
            mappings.append(MeasureMapping(
                source_measure_index=src.index,
                mapping_type=MappingType.GP_GAP,
                source_seconds_start=src.seconds_start,
                source_seconds_end=src.seconds_end,
                normalized_position_start=n_start,
                normalized_position_end=n_end,
                confidence=0.0,
                reason_codes=["no_gp_measures"],
            ))
        return AlignmentResult(
            mappings=mappings,
            global_confidence=0.0,
            warnings=[Warning(
                code=WarningCode.LOW_CONFIDENCE,
                message="No GP measures to align against",
            )],
        )

    anchor_map: dict[int, int] = {}
    if anchors:
        anchor_map = _build_anchor_map(anchors, source_measures, gp_measures)

    n_src = len(source_measures)
    n_gp = len(gp_measures)

    NEG_INF = float("-inf")
    dp = [[NEG_INF] * (n_gp + 1) for _ in range(n_src + 1)]
    bt = [[(-1, -1)] * (n_gp + 1) for _ in range(n_src + 1)]
    dp[0][0] = 0.0

    # Also store all cell scores for alternatives computation
    cell_scores: dict[tuple[int, int], float] = {}

    for i in range(1, n_src + 1):
        src = source_measures[i - 1]
        src_idx = src.index

        for j in range(n_gp + 1):
            best = NEG_INF
            best_from = (-1, -1)

            if src_idx in anchor_map:
                required_gp = anchor_map[src_idx]
                if j - 1 == required_gp:
                    for k in range(j):
                        candidate = dp[i - 1][k] + params["user_anchor_score"]
                        if candidate > best:
                            best = candidate
                            best_from = (i - 1, k)
                elif j == 0:
                    pass
                else:
                    continue
            else:
                if j > 0:
                    gp = gp_measures[j - 1]
                    cs = _compute_cell_score(
                        src, gp, source_measures, gp_measures, params
                    )
                    cell_scores[(i, j)] = cs
                    for k in range(j):
                        skip_penalty = (j - 1 - k) * params["gap_penalty"] if j - 1 > k else 0
                        candidate = dp[i - 1][k] + cs + skip_penalty
                        if candidate > best:
                            best = candidate
                            best_from = (i - 1, k)

                src_gap_score = dp[i - 1][j] + params["gap_penalty"]
                if src_gap_score > best:
                    best = src_gap_score
                    best_from = (i - 1, j)

            if best > NEG_INF:
                dp[i][j] = best
                bt[i][j] = best_from

    best_final = NEG_INF
    best_j = 0
    for j in range(n_gp + 1):
        if dp[n_src][j] > best_final:
            best_final = dp[n_src][j]
            best_j = j

    path: list[tuple[int, int]] = []
    ci, cj = n_src, best_j
    while ci > 0:
        path.append((ci, cj))
        pi, pj = bt[ci][cj]
        ci, cj = pi, pj
    path.reverse()

    mappings: list[MeasureMapping] = []
    used_gp: set[int] = set()
    alt_threshold = params.get("alternative_threshold", 2.0)

    for ci, cj in path:
        src = source_measures[ci - 1]
        n_start, n_end = _compute_normalized_positions(
            src, gp_measures[cj - 1] if cj > 0 else None,
            source_measures, gp_measures
        )

        if cj == 0:
            mapping_type = MappingType.SOURCE_GAP
            gp_idx = None
            gp_num = None
            gp_t_start = None
            gp_t_end = None
            confidence = _compute_confidence(
                src, None, MappingType.SOURCE_GAP,
                source_measures, gp_measures, params
            )
            evidence = ["source_gap"]
            reason_codes = ["no_gp_match"]
            alternatives = []
        else:
            gp = gp_measures[cj - 1]
            gp_idx = gp.measure_index
            gp_num = gp.measure_number
            gp_t_start = gp.tick_start
            gp_t_end = gp.tick_end

            if gp_idx in used_gp:
                mapping_type = MappingType.REPEAT
            elif (src.numerator != gp.numerator or src.denominator != gp.denominator):
                mapping_type = MappingType.AMBIGUOUS
            else:
                mapping_type = MappingType.ONE_TO_ONE

            confidence = _compute_confidence(
                src, gp, mapping_type, source_measures, gp_measures, params
            )

            evidence = []
            reason_codes = []
            if src.numerator == gp.numerator and src.denominator == gp.denominator:
                evidence.append("time_sig_match")
                reason_codes.append("ts_match")
            else:
                evidence.append("time_sig_mismatch")
                reason_codes.append("ts_mismatch")

            # Duration evidence
            src_dur = src.seconds_end - src.seconds_start
            total_src = max(0.001, sum(m.seconds_end - m.seconds_start for m in source_measures))
            total_gp_t = (gp_measures[-1].tick_end - gp_measures[0].tick_start) if gp_measures else 1
            if total_gp_t > 0 and total_src > 0:
                s_frac = src_dur / total_src
                g_frac = (gp.tick_end - gp.tick_start) / total_gp_t
                if g_frac > 0:
                    dur_ratio = s_frac / g_frac
                    if abs(1.0 - dur_ratio) < params["duration_tolerance_ratio"]:
                        evidence.append("duration_match")
                    else:
                        evidence.append("duration_mismatch")

            # Audio evidence
            if src.audio_downbeat_evidence is not None and src.audio_downbeat_evidence > 0.5:
                evidence.append("audio_corroboration")
                reason_codes.append("audio_confirmed")

            # Density evidence
            density = getattr(src, "note_density", 0.0) or 0.0
            if density > 0:
                evidence.append(f"density:{density:.1f}")

            if gp.marker_text:
                evidence.append(f"marker:{gp.marker_text}")
            if gp.has_repeat_open:
                evidence.append("repeat_open")
            if gp.has_repeat_close:
                evidence.append(f"repeat_close:{gp.repeat_close_count}")

            if src.index in anchor_map:
                evidence.append("user_anchor")
                reason_codes.append("anchored")
                confidence = 1.0

            if gp_idx is not None:
                used_gp.add(gp_idx)

            # Compute alternatives: other GP measures with scores close to best
            alternatives = []
            best_cell = cell_scores.get((ci, cj), 0.0)
            for aj in range(1, n_gp + 1):
                if aj == cj:
                    continue
                alt_score = cell_scores.get((ci, aj), None)
                if alt_score is not None and (best_cell - alt_score) < alt_threshold:
                    alt_gp = gp_measures[aj - 1]
                    alt_reason = "near_tie"
                    if alt_gp.marker_text:
                        alt_reason += f":marker={alt_gp.marker_text}"
                    alternatives.append(MappingAlternative(
                        gp_measure_index=alt_gp.measure_index,
                        score=alt_score,
                        reason=alt_reason,
                    ))
            alternatives.sort(key=lambda a: -a.score)

        m_warnings: list[Warning] = []
        if confidence < 0.5:
            m_warnings.append(Warning(
                code=WarningCode.LOW_CONFIDENCE,
                message=f"Low confidence mapping: {confidence:.2f}",
            ))
        if mapping_type == MappingType.AMBIGUOUS:
            m_warnings.append(Warning(
                code=WarningCode.AMBIGUOUS_MAPPING,
                message="Ambiguous mapping due to time signature mismatch",
            ))

        mappings.append(MeasureMapping(
            source_measure_index=src.index,
            gp_measure_index=gp_idx,
            gp_measure_number=gp_num,
            mapping_type=mapping_type,
            source_seconds_start=src.seconds_start,
            source_seconds_end=src.seconds_end,
            gp_tick_start=gp_t_start,
            gp_tick_end=gp_t_end,
            normalized_position_start=n_start,
            normalized_position_end=n_end,
            confidence=confidence,
            evidence=evidence,
            reason_codes=reason_codes,
            warnings=m_warnings,
            alternatives=alternatives,
        ))

    # Explicit GP gap mappings for unmapped GP measures
    unmapped_gp = sorted(set(range(n_gp)) - used_gp)
    for gp_idx in unmapped_gp:
        gp = gp_measures[gp_idx]
        mappings.append(MeasureMapping(
            source_measure_index=-1,
            gp_measure_index=gp.measure_index,
            gp_measure_number=gp.measure_number,
            mapping_type=MappingType.GP_GAP,
            source_seconds_start=0.0,
            source_seconds_end=0.0,
            gp_tick_start=gp.tick_start,
            gp_tick_end=gp.tick_end,
            confidence=0.1,
            evidence=["gp_gap"],
            reason_codes=["no_source_match"],
            warnings=[Warning(
                code=WarningCode.LOW_CONFIDENCE,
                message=f"GP measure {gp.measure_number} has no source mapping",
            )],
        ))

    if unmapped_gp:
        warnings.append(Warning(
            code=WarningCode.LOW_CONFIDENCE,
            message=f"{len(unmapped_gp)} GP measures not mapped to any source measure",
            context={"unmapped_gp_indices": unmapped_gp},
        ))

    # Consensus conflict reduces global confidence
    if consensus_has_conflict:
        warnings.append(Warning(
            code=WarningCode.TEMPO_MAP_CONFLICT,
            message="Consensus conflict detected; global confidence reduced",
        ))

    matched = [m for m in mappings if m.mapping_type not in (MappingType.SOURCE_GAP, MappingType.GP_GAP)]
    total_confidence = (
        sum(m.confidence for m in matched) / len(matched) if matched else 0.0
    )

    if consensus_has_conflict:
        total_confidence *= 0.7

    return AlignmentResult(
        mappings=mappings,
        global_confidence=min(1.0, total_confidence),
        warnings=warnings,
    )
