"""Source-to-GP deterministic sequence alignment.

A dynamic-programming alignment of source measures onto GP measures with
explicit scoring, monotonicity, gap support, justified repeats and hard user
anchors.

Backtrace correctness
---------------------
Each DP cell stores the *operation* that produced it, not only the predecessor
coordinates. This matters: a `source_gap` transition into `(i, j)` keeps `j > 0`
because the last consumed GP measure does not change. Reconstructing from
`(i, j)` alone therefore used to reinterpret that transition as a second match
against `gp[j - 1]` and emitted a bogus `repeat`.

Tie-breaking
------------
Candidate transitions are evaluated in a fixed order — matches with the smallest
skipped-GP prefix first, then a justified repeat, then a source gap — and a
candidate replaces the incumbent only on a strictly greater score. The final
column is scanned in ascending `j`. Alignment is therefore a pure function of
its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import (
    GPMeasure,
    MappingAlternative,
    MappingType,
    MeasureMapping,
    SourceMeasure,
    Warning,
    WarningCode,
)
from .structure import StructureError, validate_anchor_monotonicity

DEFAULT_PARAMS = {
    "version": "3.0.0",
    # --- cell score weights ---
    "ts_match_score": 10.0,
    "ts_mismatch_penalty": -5.0,
    "marker_anchor_bonus": 8.0,
    "user_anchor_score": 100.0,
    "gap_penalty": -3.0,
    "repeat_transition_penalty": -1.0,
    "duration_weight": 2.0,
    "duration_tolerance_ratio": 0.3,
    "density_weight": 1.0,
    "density_reference": 4.0,
    "repeat_bonus": 2.0,
    "alternate_ending_bonus": 1.0,
    "audio_corroboration_bonus": 3.0,
    # --- confidence components (must sum to <= 1.0) ---
    "confidence_ts_match": 0.25,
    "confidence_duration": 0.25,
    "confidence_marker": 0.12,
    "confidence_monotonic": 0.15,
    "confidence_density": 0.10,
    "confidence_audio": 0.10,
    "confidence_repeat": 0.03,
    "confidence_gap": 0.10,
    "confidence_ambiguous_factor": 0.5,
    "confidence_conflict_factor": 0.5,
    "confidence_conflict_global_factor": 0.7,
    "alternative_threshold": 2.0,
    "max_alternatives": 4,
    # --- audio evidence extraction (kept here so it takes part in identity) ---
    "audio_max_read_seconds": 600,
    "audio_analysis_sample_rate": 22050,
}


class Op(str, Enum):
    """DP transition operation."""

    MATCH = "match"
    REPEAT = "repeat"
    SOURCE_GAP = "source_gap"
    ANCHOR = "anchor"


@dataclass(frozen=True)
class AnchorConstraint:
    """User-provided anchor: source measure X must map to GP measure Y."""

    source_measure_index: int
    gp_measure_index: int
    label: str | None = None


@dataclass
class AlignmentResult:
    mappings: list[MeasureMapping] = field(default_factory=list)
    global_confidence: float = 0.0
    warnings: list[Warning] = field(default_factory=list)
    anchored_source_indices: tuple[int, ...] = ()


# --------------------------------------------------------------------------
# Score components
# --------------------------------------------------------------------------

def _time_sig_score(src: SourceMeasure, gp: GPMeasure, params: dict) -> float:
    if src.numerator == gp.numerator and src.denominator == gp.denominator:
        return params["ts_match_score"]
    return params["ts_mismatch_penalty"]


def _duration_deviation(
    src: SourceMeasure,
    gp: GPMeasure,
    all_source: list[SourceMeasure],
    all_gp: list[GPMeasure],
) -> float | None:
    """Relative-duration deviation, or None when it cannot be computed.

    The source timeline is absolute seconds and the GP timeline is musical
    ticks, so only *proportions* of the respective totals are comparable.
    """
    if not all_source or not all_gp:
        return None
    total_gp_ticks = all_gp[-1].tick_end - all_gp[0].tick_start
    total_src = sum(m.seconds_end - m.seconds_start for m in all_source)
    if total_gp_ticks <= 0 or total_src <= 0:
        return None

    gp_frac = (gp.tick_end - gp.tick_start) / total_gp_ticks
    src_frac = (src.seconds_end - src.seconds_start) / total_src
    if gp_frac <= 0:
        return None
    return abs(1.0 - src_frac / gp_frac)


def _duration_score(
    src: SourceMeasure,
    gp: GPMeasure,
    all_source: list[SourceMeasure],
    all_gp: list[GPMeasure],
    params: dict,
) -> float:
    deviation = _duration_deviation(src, gp, all_source, all_gp)
    if deviation is None:
        return 0.0
    tolerance = params["duration_tolerance_ratio"]
    weight = params["duration_weight"]
    if deviation < tolerance:
        return weight * (1.0 - deviation / tolerance)
    return -weight * min(1.0, deviation)


def _density_fraction(src: SourceMeasure, params: dict) -> float:
    """Note density normalized against a reference density, clamped to [0,1]."""
    density = src.note_density or 0.0
    reference = params.get("density_reference", 4.0) or 4.0
    if density <= 0:
        return 0.0
    return min(1.0, density / reference)


def _audio_fraction(src: SourceMeasure) -> float:
    if src.audio_downbeat_evidence is None:
        return 0.0
    return max(0.0, min(1.0, src.audio_downbeat_evidence))


def _repeat_score(gp: GPMeasure, params: dict) -> float:
    bonus = 0.0
    if gp.has_repeat_open or gp.has_repeat_close:
        bonus += params["repeat_bonus"]
    if gp.has_alternate_ending:
        bonus += params["alternate_ending_bonus"]
    return bonus


def _marker_score(gp: GPMeasure, params: dict) -> float:
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
    return (
        _time_sig_score(src, gp, params)
        + _duration_score(src, gp, all_source, all_gp, params)
        + params["density_weight"] * _density_fraction(src, params)
        + _repeat_score(gp, params)
        + params["audio_corroboration_bonus"] * _audio_fraction(src)
        + _marker_score(gp, params)
    )


def repeat_eligible_indices(gp_measures: list[GPMeasure]) -> frozenset[int]:
    """GP measure indices for which mapping more than one source measure is
    musically justified.

    A measure is eligible when it carries a repeat marker or an alternate
    ending, or when it lies inside an open/close repeat span.
    """
    eligible: set[int] = set()
    open_at: int | None = None
    for pos, gp in enumerate(gp_measures):
        if gp.has_repeat_open:
            open_at = pos
        if gp.has_repeat_open or gp.has_repeat_close or gp.has_alternate_ending:
            eligible.add(pos)
        if gp.has_repeat_close:
            start = open_at if open_at is not None else 0
            eligible.update(range(start, pos + 1))
            open_at = None
    return frozenset(eligible)


# --------------------------------------------------------------------------
# Anchors
# --------------------------------------------------------------------------

def _build_anchor_map(
    anchors: list[AnchorConstraint],
    source_measures: list[SourceMeasure],
    gp_measures: list[GPMeasure],
) -> dict[int, AnchorConstraint]:
    """Build and validate anchor map: source measure index -> constraint."""
    valid_source = {m.index for m in source_measures}
    anchor_map: dict[int, AnchorConstraint] = {}
    seen_gp: dict[int, int] = {}

    for a in sorted(anchors, key=lambda x: (x.source_measure_index, x.gp_measure_index)):
        if a.source_measure_index not in valid_source:
            raise StructureError(
                "structure_source_index_out_of_range",
                f"Anchor source index {a.source_measure_index} is not a source measure",
            )
        if a.gp_measure_index < 0 or a.gp_measure_index >= len(gp_measures):
            raise StructureError(
                "structure_gp_index_out_of_range",
                f"Anchor GP index {a.gp_measure_index} out of range "
                f"[0, {len(gp_measures)})",
            )
        existing = anchor_map.get(a.source_measure_index)
        if existing is not None and existing.gp_measure_index != a.gp_measure_index:
            raise StructureError(
                "structure_conflicting_anchor",
                f"Conflicting anchors: source {a.source_measure_index} mapped to both "
                f"GP {existing.gp_measure_index} and GP {a.gp_measure_index}",
            )
        prior_src = seen_gp.get(a.gp_measure_index)
        if prior_src is not None and prior_src != a.source_measure_index:
            raise StructureError(
                "structure_conflicting_anchor",
                f"Conflicting anchors: GP {a.gp_measure_index} mapped from both "
                f"source {prior_src} and source {a.source_measure_index}",
            )
        anchor_map[a.source_measure_index] = a
        seen_gp[a.gp_measure_index] = a.source_measure_index

    validate_anchor_monotonicity(
        [(idx, c.gp_measure_index) for idx, c in anchor_map.items()]
    )
    return anchor_map


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------

def _overlaps_conflict(
    src: SourceMeasure, conflict_regions: list[dict]
) -> dict | None:
    for region in conflict_regions:
        start = region.get("start_seconds")
        end = region.get("end_seconds")
        if start is None or end is None:
            continue
        if src.seconds_start < float(end) and src.seconds_end > float(start):
            return region
    return None


def _compute_confidence(
    src: SourceMeasure,
    gp: GPMeasure | None,
    mapping_type: MappingType,
    all_source: list[SourceMeasure],
    all_gp: list[GPMeasure],
    params: dict,
    repeat_justified: bool,
) -> float:
    """Calibrated confidence in [0,1] built from the same evidence the DP scored."""
    if gp is None or mapping_type in (MappingType.SOURCE_GAP, MappingType.GP_GAP):
        return params["confidence_gap"]

    confidence = 0.0

    if src.numerator == gp.numerator and src.denominator == gp.denominator:
        confidence += params["confidence_ts_match"]

    deviation = _duration_deviation(src, gp, all_source, all_gp)
    if deviation is not None:
        tolerance = params["duration_tolerance_ratio"]
        confidence += params["confidence_duration"] * max(
            0.0, 1.0 - deviation / tolerance
        )

    if gp.marker_text or gp.section_text:
        confidence += params["confidence_marker"]

    # Monotonicity is guaranteed by DP construction.
    confidence += params["confidence_monotonic"]

    confidence += params["confidence_density"] * _density_fraction(src, params)
    confidence += params["confidence_audio"] * _audio_fraction(src)

    if repeat_justified:
        confidence += params["confidence_repeat"]

    if mapping_type == MappingType.AMBIGUOUS:
        confidence *= params["confidence_ambiguous_factor"]

    return min(1.0, max(0.0, confidence))


def _normalized_positions(
    src: SourceMeasure, all_source: list[SourceMeasure]
) -> tuple[float, float]:
    """Normalized [0,1] position of this source measure within the timeline."""
    if not all_source:
        return 0.0, 1.0
    origin = all_source[0].seconds_start
    span = all_source[-1].seconds_end - origin
    if span <= 0:
        return 0.0, 1.0
    start = (src.seconds_start - origin) / span
    end = (src.seconds_end - origin) / span
    start = max(0.0, min(1.0, start))
    end = max(start, min(1.0, end))
    return start, end


# --------------------------------------------------------------------------
# Alignment
# --------------------------------------------------------------------------

NEG_INF = float("-inf")


def align_measures(
    source_measures: list[SourceMeasure],
    gp_measures: list[GPMeasure],
    anchors: list[AnchorConstraint] | None = None,
    params: dict | None = None,
    consensus_has_conflict: bool = False,
    conflict_regions: list[dict] | None = None,
) -> AlignmentResult:
    """Align source measures to GP measures using dynamic programming."""
    if params is None:
        params = DEFAULT_PARAMS.copy()
    conflict_regions = list(conflict_regions or [])

    warnings: list[Warning] = []

    if not source_measures:
        return AlignmentResult(
            mappings=[],
            global_confidence=0.0,
            warnings=[
                Warning(
                    code=WarningCode.LOW_CONFIDENCE,
                    message="No source measures to align",
                )
            ],
        )

    if not gp_measures:
        mappings = []
        for src in source_measures:
            n_start, n_end = _normalized_positions(src, source_measures)
            mappings.append(
                MeasureMapping(
                    source_measure_index=src.index,
                    mapping_type=MappingType.SOURCE_GAP,
                    source_seconds_start=src.seconds_start,
                    source_seconds_end=src.seconds_end,
                    normalized_position_start=n_start,
                    normalized_position_end=n_end,
                    confidence=params["confidence_gap"],
                    evidence=["source_gap"],
                    reason_codes=["no_gp_measures"],
                )
            )
        return AlignmentResult(
            mappings=mappings,
            global_confidence=0.0,
            warnings=[
                Warning(
                    code=WarningCode.LOW_CONFIDENCE,
                    message="No GP measures to align against",
                )
            ],
        )

    anchor_map = _build_anchor_map(list(anchors or []), source_measures, gp_measures)
    repeatable = repeat_eligible_indices(gp_measures)

    n_src = len(source_measures)
    n_gp = len(gp_measures)

    # Pre-compute every cell score so alternatives are available for all rows,
    # including anchored ones.
    cell: list[list[float]] = [[0.0] * (n_gp + 1) for _ in range(n_src + 1)]
    for i in range(1, n_src + 1):
        for j in range(1, n_gp + 1):
            cell[i][j] = _compute_cell_score(
                source_measures[i - 1],
                gp_measures[j - 1],
                source_measures,
                gp_measures,
                params,
            )

    dp = [[NEG_INF] * (n_gp + 1) for _ in range(n_src + 1)]
    bt: list[list[tuple[int, int, Op] | None]] = [
        [None] * (n_gp + 1) for _ in range(n_src + 1)
    ]
    dp[0][0] = 0.0

    gap_penalty = params["gap_penalty"]
    repeat_penalty = params["repeat_transition_penalty"]

    for i in range(1, n_src + 1):
        src = source_measures[i - 1]
        constraint = anchor_map.get(src.index)

        for j in range(n_gp + 1):
            best = NEG_INF
            best_bt: tuple[int, int, Op] | None = None

            if constraint is not None:
                # Hard anchor: this source measure may only land on its GP measure.
                if j == constraint.gp_measure_index + 1:
                    for k in range(j):
                        if dp[i - 1][k] == NEG_INF:
                            continue
                        skipped = (j - 1 - k) * gap_penalty
                        candidate = (
                            dp[i - 1][k] + params["user_anchor_score"] + skipped
                        )
                        if candidate > best:
                            best = candidate
                            best_bt = (i - 1, k, Op.ANCHOR)
            else:
                # 1. match, smallest skipped-GP prefix first
                if j > 0:
                    for k in range(j):
                        if dp[i - 1][k] == NEG_INF:
                            continue
                        skipped = (j - 1 - k) * gap_penalty
                        candidate = dp[i - 1][k] + cell[i][j] + skipped
                        if candidate > best:
                            best = candidate
                            best_bt = (i - 1, k, Op.MATCH)

                # 2. justified repeat of the same GP measure
                if j > 0 and (j - 1) in repeatable and dp[i - 1][j] != NEG_INF:
                    candidate = dp[i - 1][j] + cell[i][j] + repeat_penalty
                    if candidate > best:
                        best = candidate
                        best_bt = (i - 1, j, Op.REPEAT)

                # 3. source gap — the source measure has no GP counterpart
                if dp[i - 1][j] != NEG_INF:
                    candidate = dp[i - 1][j] + gap_penalty
                    if candidate > best:
                        best = candidate
                        best_bt = (i - 1, j, Op.SOURCE_GAP)

            if best_bt is not None:
                dp[i][j] = best
                bt[i][j] = best_bt

    best_final = NEG_INF
    best_j = 0
    for j in range(n_gp + 1):
        if dp[n_src][j] > best_final:
            best_final = dp[n_src][j]
            best_j = j

    if best_final == NEG_INF:
        # Only reachable when anchors make every path impossible.
        raise StructureError(
            "structure_anchor_unsatisfiable",
            "No monotonic alignment satisfies the supplied anchors",
        )

    # Walk the backtrace, recording the operation for each source measure.
    path: list[tuple[int, int, Op]] = []
    ci, cj = n_src, best_j
    while ci > 0:
        entry = bt[ci][cj]
        assert entry is not None, "backtrace hole"
        pi, pj, op = entry
        path.append((ci, cj, op))
        ci, cj = pi, pj
    path.reverse()

    mappings: list[MeasureMapping] = []
    matched_gp_positions: set[int] = set()
    alt_threshold = params.get("alternative_threshold", 2.0)
    max_alts = int(params.get("max_alternatives", 4))

    for i, j, op in path:
        src = source_measures[i - 1]
        n_start, n_end = _normalized_positions(src, source_measures)

        if op is Op.SOURCE_GAP:
            confidence = params["confidence_gap"]
            reason_codes = ["no_gp_match"]
            mappings.append(
                MeasureMapping(
                    source_measure_index=src.index,
                    mapping_type=MappingType.SOURCE_GAP,
                    source_seconds_start=src.seconds_start,
                    source_seconds_end=src.seconds_end,
                    normalized_position_start=n_start,
                    normalized_position_end=n_end,
                    confidence=confidence,
                    evidence=["source_gap"],
                    reason_codes=reason_codes,
                    warnings=[
                        Warning(
                            code=WarningCode.LOW_CONFIDENCE,
                            message=(
                                f"Source measure {src.index} has no GP counterpart"
                            ),
                        )
                    ],
                )
            )
            continue

        gp = gp_measures[j - 1]
        matched_gp_positions.add(j - 1)
        repeat_justified = op is Op.REPEAT

        if op is Op.REPEAT:
            mapping_type = MappingType.REPEAT
        elif src.numerator != gp.numerator or src.denominator != gp.denominator:
            mapping_type = MappingType.AMBIGUOUS
        else:
            mapping_type = MappingType.ONE_TO_ONE

        evidence: list[str] = []
        reason_codes: list[str] = []

        if src.numerator == gp.numerator and src.denominator == gp.denominator:
            evidence.append("time_sig_match")
            reason_codes.append("ts_match")
        else:
            evidence.append("time_sig_mismatch")
            reason_codes.append("ts_mismatch")

        deviation = _duration_deviation(src, gp, source_measures, gp_measures)
        if deviation is not None:
            if deviation < params["duration_tolerance_ratio"]:
                evidence.append(f"duration_match:{deviation:.3f}")
                reason_codes.append("duration_match")
            else:
                evidence.append(f"duration_mismatch:{deviation:.3f}")
                reason_codes.append("duration_mismatch")

        audio_frac = _audio_fraction(src)
        if audio_frac > 0:
            evidence.append(f"audio_corroboration:{audio_frac:.3f}")
            reason_codes.append("audio_confirmed")

        density_frac = _density_fraction(src, params)
        if density_frac > 0:
            evidence.append(f"density:{src.note_density:.3f}")
            reason_codes.append("density_evidence")

        if gp.marker_text:
            evidence.append(f"marker:{gp.marker_text}")
            reason_codes.append("marker_evidence")
        if gp.section_text:
            evidence.append(f"section:{gp.section_text}")
        if gp.has_repeat_open:
            evidence.append("repeat_open")
        if gp.has_repeat_close:
            evidence.append(f"repeat_close:{gp.repeat_close_count}")
        if gp.has_alternate_ending:
            evidence.append(
                "alternate_ending:"
                + ",".join(str(n) for n in gp.alternate_ending_numbers)
            )
            reason_codes.append("alternate_ending")
        if op is Op.REPEAT:
            reason_codes.append("repeat_justified")

        confidence = _compute_confidence(
            src,
            gp,
            mapping_type,
            source_measures,
            gp_measures,
            params,
            repeat_justified,
        )

        if op is Op.ANCHOR:
            evidence.append("user_anchor")
            reason_codes.append("anchored")
            confidence = 1.0

        region = _overlaps_conflict(src, conflict_regions)
        if region is not None:
            confidence *= params["confidence_conflict_factor"]
            reason_codes.append("consensus_conflict_region")
            evidence.append("consensus_conflict")

        # Alternatives: other GP measures whose cell score is within the
        # near-tie threshold of the selected one.
        chosen_score = cell[i][j]
        alternatives: list[MappingAlternative] = []
        for aj in range(1, n_gp + 1):
            if aj == j:
                continue
            delta = chosen_score - cell[i][aj]
            if delta < alt_threshold:
                alt_gp = gp_measures[aj - 1]
                reason = "near_tie"
                if alt_gp.marker_text:
                    reason += f":marker={alt_gp.marker_text}"
                alternatives.append(
                    MappingAlternative(
                        gp_measure_index=alt_gp.measure_index,
                        score=round(cell[i][aj], 6),
                        reason=reason,
                    )
                )
        alternatives.sort(key=lambda a: (-a.score, a.gp_measure_index or 0))
        alternatives = alternatives[:max_alts]

        m_warnings: list[Warning] = []
        if confidence < 0.5:
            m_warnings.append(
                Warning(
                    code=WarningCode.LOW_CONFIDENCE,
                    message=f"Low confidence mapping: {confidence:.2f}",
                )
            )
        if mapping_type == MappingType.AMBIGUOUS:
            m_warnings.append(
                Warning(
                    code=WarningCode.AMBIGUOUS_MAPPING,
                    message="Ambiguous mapping due to time signature mismatch",
                )
            )
        if region is not None:
            m_warnings.append(
                Warning(
                    code=WarningCode.TEMPO_MAP_CONFLICT,
                    message=(
                        "Source measure lies in a MIDI consensus conflict region; "
                        "confidence reduced"
                    ),
                    context={k: region.get(k) for k in ("start_seconds", "end_seconds")},
                )
            )

        mappings.append(
            MeasureMapping(
                source_measure_index=src.index,
                gp_measure_index=gp.measure_index,
                gp_measure_number=gp.measure_number,
                mapping_type=mapping_type,
                source_seconds_start=src.seconds_start,
                source_seconds_end=src.seconds_end,
                gp_tick_start=gp.tick_start,
                gp_tick_end=gp.tick_end,
                normalized_position_start=n_start,
                normalized_position_end=n_end,
                confidence=confidence,
                evidence=evidence,
                reason_codes=reason_codes,
                warnings=m_warnings,
                alternatives=alternatives,
            )
        )

    # Explicit GP-gap records for GP measures no source measure reached.
    unmapped_gp = sorted(set(range(n_gp)) - matched_gp_positions)
    for pos in unmapped_gp:
        gp = gp_measures[pos]
        mappings.append(
            MeasureMapping(
                source_measure_index=-1,
                gp_measure_index=gp.measure_index,
                gp_measure_number=gp.measure_number,
                mapping_type=MappingType.GP_GAP,
                source_seconds_start=0.0,
                source_seconds_end=0.0,
                gp_tick_start=gp.tick_start,
                gp_tick_end=gp.tick_end,
                normalized_position_start=0.0,
                normalized_position_end=0.0,
                confidence=params["confidence_gap"],
                evidence=["gp_gap"],
                reason_codes=["no_source_match"],
                warnings=[
                    Warning(
                        code=WarningCode.LOW_CONFIDENCE,
                        message=(
                            f"GP measure {gp.measure_number} has no source mapping"
                        ),
                    )
                ],
            )
        )

    if unmapped_gp:
        warnings.append(
            Warning(
                code=WarningCode.LOW_CONFIDENCE,
                message=f"{len(unmapped_gp)} GP measures not mapped to any source measure",
                context={"unmapped_gp_indices": unmapped_gp},
            )
        )

    source_gaps = [m for m in mappings if m.mapping_type == MappingType.SOURCE_GAP]
    if source_gaps:
        warnings.append(
            Warning(
                code=WarningCode.LOW_CONFIDENCE,
                message=f"{len(source_gaps)} source measures have no GP counterpart",
                context={
                    "source_gap_indices": [m.source_measure_index for m in source_gaps]
                },
            )
        )

    if consensus_has_conflict:
        warnings.append(
            Warning(
                code=WarningCode.TEMPO_MAP_CONFLICT,
                message=(
                    f"MIDI consensus conflict across {len(conflict_regions)} region(s); "
                    "affected mapping confidence reduced"
                ),
                context={"conflict_region_count": len(conflict_regions)},
            )
        )

    matched = [
        m
        for m in mappings
        if m.mapping_type not in (MappingType.SOURCE_GAP, MappingType.GP_GAP)
    ]
    global_confidence = (
        sum(m.confidence for m in matched) / len(matched) if matched else 0.0
    )
    # Gaps are real evidence of a partial reconciliation, so they pull the
    # global figure down instead of being averaged away.
    coverage = len(matched) / max(1, len(mappings))
    global_confidence *= coverage

    # A consensus conflict is evidence against the whole reconciliation, even
    # when the disagreement could not be localized to specific regions.
    if consensus_has_conflict:
        global_confidence *= params["confidence_conflict_global_factor"]

    return AlignmentResult(
        mappings=mappings,
        global_confidence=min(1.0, max(0.0, global_confidence)),
        warnings=warnings,
        anchored_source_indices=tuple(sorted(anchor_map)),
    )
