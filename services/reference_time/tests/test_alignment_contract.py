"""Regression tests for the audited alignment/structure/consensus defects.

Audit item F (backtrace misclassifying source gaps as repeats), the section 6.4
scoring-sensitivity requirements, the section 6.3 structure/anchor contract and
the section 6.5 consensus normalization. Each test was observed failing against
the pre-fix implementation.
"""

from __future__ import annotations

import io
import json

import mido
import pytest
from reference_time.mapping import AnchorConstraint, align_measures
from reference_time.models import GPMeasure, MappingType, SourceMeasure
from reference_time.structure import StructureError, parse_structure_json

GP_SHA = "a" * 64


def _src(index: int, start: float, end: float, num: int = 4, den: int = 4, **kw) -> SourceMeasure:
    return SourceMeasure(
        index=index,
        tick_start=index * 1920,
        tick_end=(index + 1) * 1920,
        seconds_start=start,
        seconds_end=end,
        numerator=num,
        denominator=den,
        tempo_bpm=120.0,
        confidence=1.0,
        **kw,
    )


def _gp(index: int, num: int = 4, den: int = 4, **kw) -> GPMeasure:
    return GPMeasure(
        gp_revision_sha256=GP_SHA,
        measure_index=index,
        measure_number=index + 1,
        tick_start=index * 3840,
        tick_end=(index + 1) * 3840,
        numerator=num,
        denominator=den,
        **kw,
    )


# --------------------------------------------------------------------------
# F. Alignment backtrace must not turn a source gap into a repeat
# --------------------------------------------------------------------------

class TestDefectFBacktrace:
    def test_hard_anchor_with_more_source_than_gp_measures_keeps_source_gaps(self):
        """A source_gap transition retaining j > 0 must stay a source_gap.

        Pre-fix, reconstructing only from (i, j) reinterpreted such a transition
        as a second match against gp[j - 1] and emitted `repeat`.
        """
        source = [_src(i, i * 2.0, (i + 1) * 2.0) for i in range(6)]
        gp = [_gp(i) for i in range(3)]
        result = align_measures(
            source,
            gp,
            anchors=[AnchorConstraint(source_measure_index=0, gp_measure_index=0)],
        )
        types = [m.mapping_type for m in result.mappings if m.source_measure_index >= 0]
        assert MappingType.REPEAT not in types, (
            f"source gaps were misclassified as repeats: {types}"
        )
        assert types.count(MappingType.SOURCE_GAP) == len(source) - len(gp)

    def test_repeat_requires_repeat_evidence(self):
        """Without repeat markers no GP measure may be mapped twice."""
        source = [_src(i, i * 2.0, (i + 1) * 2.0) for i in range(5)]
        gp = [_gp(i) for i in range(2)]
        result = align_measures(source, gp)
        mapped = [
            m.gp_measure_index
            for m in result.mappings
            if m.mapping_type in (MappingType.ONE_TO_ONE, MappingType.AMBIGUOUS, MappingType.REPEAT)
        ]
        assert len(mapped) == len(set(mapped))

    def test_repeat_is_emitted_when_gp_declares_a_repeat(self):
        source = [_src(i, i * 2.0, (i + 1) * 2.0) for i in range(4)]
        gp = [
            _gp(0, has_repeat_open=True),
            _gp(1, has_repeat_close=True, repeat_close_count=2),
        ]
        result = align_measures(source, gp)
        types = [m.mapping_type for m in result.mappings if m.source_measure_index >= 0]
        assert MappingType.REPEAT in types

    def test_gp_gap_is_an_explicit_record(self):
        source = [_src(0, 0.0, 2.0)]
        gp = [_gp(0), _gp(1), _gp(2)]
        result = align_measures(source, gp)
        gp_gaps = [m for m in result.mappings if m.mapping_type == MappingType.GP_GAP]
        assert len(gp_gaps) == 2
        assert all(m.gp_measure_index is not None for m in gp_gaps)


# --------------------------------------------------------------------------
# F/6.4. Scoring must actually depend on every declared evidence channel
# --------------------------------------------------------------------------

class TestScoringSensitivity:
    def _base(self):
        source = [_src(i, i * 2.0, (i + 1) * 2.0) for i in range(4)]
        gp = [_gp(i) for i in range(4)]
        return source, gp

    def test_duration_influences_confidence(self):
        source, gp = self._base()
        baseline = align_measures(source, gp).mappings[0].confidence
        skewed = [_src(0, 0.0, 0.2)] + [_src(i, i * 2.0, (i + 1) * 2.0) for i in range(1, 4)]
        assert align_measures(skewed, gp).mappings[0].confidence != baseline

    def test_density_influences_confidence(self):
        source, gp = self._base()
        baseline = align_measures(source, gp).mappings[0].confidence
        dense = [_src(0, 0.0, 2.0, note_density=8.0)] + source[1:]
        assert align_measures(dense, gp).mappings[0].confidence != baseline

    def test_audio_influences_confidence(self):
        source, gp = self._base()
        baseline = align_measures(source, gp).mappings[0].confidence
        with_audio = [_src(0, 0.0, 2.0, audio_downbeat_evidence=0.95)] + source[1:]
        assert align_measures(with_audio, gp).mappings[0].confidence > baseline

    def test_marker_influences_confidence(self):
        source, gp = self._base()
        baseline = align_measures(source, gp).mappings[0].confidence
        marked = [_gp(0, marker_text="Intro")] + gp[1:]
        assert align_measures(source, marked).mappings[0].confidence != baseline

    def test_repeat_influences_score(self):
        source, gp = self._base()
        baseline = align_measures(source, gp)
        with_repeat = align_measures(source, [_gp(0, has_repeat_open=True)] + gp[1:])
        assert baseline.global_confidence != with_repeat.global_confidence or [
            m.evidence for m in baseline.mappings
        ] != [m.evidence for m in with_repeat.mappings]

    def test_conflict_region_lowers_mapping_confidence(self):
        source, gp = self._base()
        baseline = align_measures(source, gp)
        conflicted = align_measures(
            source,
            gp,
            consensus_has_conflict=True,
            conflict_regions=[{"start_seconds": 0.0, "end_seconds": 2.0}],
        )
        assert conflicted.mappings[0].confidence < baseline.mappings[0].confidence
        assert "consensus_conflict_region" in conflicted.mappings[0].reason_codes
        assert conflicted.global_confidence < baseline.global_confidence

    def test_alternatives_are_reported_for_near_ties(self):
        source, gp = self._base()
        result = align_measures(source, gp)
        assert any(m.alternatives for m in result.mappings)

    def test_normalized_positions_span_the_timeline(self):
        source, gp = self._base()
        result = align_measures(source, gp)
        matched = [m for m in result.mappings if m.mapping_type == MappingType.ONE_TO_ONE]
        assert matched[0].normalized_position_start == pytest.approx(0.0)
        assert matched[-1].normalized_position_end == pytest.approx(1.0)
        assert all(
            0.0 <= m.normalized_position_start <= m.normalized_position_end <= 1.0
            for m in result.mappings
        )

    def test_alignment_is_deterministic(self):
        source, gp = self._base()
        a = align_measures(source, gp)
        b = align_measures(source, gp)
        assert [m.model_dump() for m in a.mappings] == [m.model_dump() for m in b.mappings]


# --------------------------------------------------------------------------
# 6.3. Structure/anchor contract must fail closed with distinct codes
# --------------------------------------------------------------------------

class TestStructureContract:
    SRC = [_src(i, i * 2.0, (i + 1) * 2.0) for i in range(8)]
    N_GP = 8

    def _parse(self, doc, source=None, n_gp=None):
        payload = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
        return parse_structure_json(
            payload,
            source_measures=source if source is not None else self.SRC,
            n_gp_measures=self.N_GP if n_gp is None else n_gp,
        )

    @pytest.mark.parametrize(
        ("doc", "code"),
        [
            (b"{not json", "structure_malformed_json"),
            ({"version": "9.9", "anchors": []}, "structure_unsupported_version"),
            ({"version": "1.0", "anchors": [{"gp_measure": 1}]}, "structure_field_missing"),
            (
                {"version": "1.0", "anchors": [{"source_measure": "x", "gp_measure": 1}]},
                "structure_index_not_integer",
            ),
            (
                {"version": "1.0", "anchors": [{"source_measure": 99, "gp_measure": 1}]},
                "structure_source_index_out_of_range",
            ),
            (
                {"version": "1.0", "anchors": [{"source_measure": 1, "gp_measure": 99}]},
                "structure_gp_index_out_of_range",
            ),
            (
                {
                    "version": "1.0",
                    "anchors": [
                        {"source_measure": 1, "gp_measure": 1},
                        {"source_measure": 1, "gp_measure": 2},
                    ],
                },
                "structure_conflicting_anchor",
            ),
            (
                {
                    "version": "1.0",
                    "anchors": [
                        {"source_measure": 1, "gp_measure": 4},
                        {"source_measure": 3, "gp_measure": 2},
                    ],
                },
                "structure_non_monotonic_anchors",
            ),
        ],
    )
    def test_invalid_structure_fails_with_stable_code(self, doc, code):
        with pytest.raises(StructureError) as exc:
            self._parse(doc)
        assert exc.value.code == code

    def test_non_object_root_fails_with_stable_code(self):
        with pytest.raises(StructureError) as exc:
            self._parse([1, 2, 3])
        assert exc.value.code == "structure_root_not_object"

    def test_duplicate_identical_anchor_is_accepted_once(self):
        result = self._parse(
            {
                "version": "1.0",
                "anchors": [
                    {"source_measure": 2, "gp_measure": 3},
                    {"source_measure": 2, "gp_measure": 3},
                ],
            }
        )
        assert len(result.anchors) == 1

    def test_section_derived_anchors_and_labels(self):
        result = self._parse(
            {
                "version": "1.0",
                "sections": [
                    {"label": "Intro", "source_measure": 0, "gp_measure": 0},
                    {"label": "Verse", "source_measure": 4, "gp_measure": 4},
                ],
            }
        )
        assert [(a.source_measure_index, a.gp_measure_index) for a in result.anchors] == [
            (0, 0),
            (4, 4),
        ]
        assert [s.label for s in result.sections] == ["Intro", "Verse"]

    def test_timestamp_anchor_is_converted_to_a_source_measure(self):
        result = self._parse(
            {
                "version": "1.0",
                "anchors": [{"source_seconds": 5.0, "gp_measure": 2}],
            }
        )
        assert len(result.anchors) == 1
        # measure 2 spans [4.0, 6.0)
        assert result.anchors[0].source_measure_index == 2

    def test_timestamp_anchor_outside_the_timeline_fails(self):
        with pytest.raises(StructureError) as exc:
            self._parse({"version": "1.0", "anchors": [{"source_seconds": 999.0, "gp_measure": 2}]})
        assert exc.value.code == "structure_source_index_out_of_range"

    def test_valid_hard_anchor_locks_the_selected_mapping(self):
        source = [_src(i, i * 2.0, (i + 1) * 2.0) for i in range(6)]
        gp = [_gp(i) for i in range(6)]
        free = align_measures(source, gp)
        anchored = align_measures(
            source,
            gp,
            anchors=[AnchorConstraint(source_measure_index=1, gp_measure_index=4)],
        )
        by_src = {m.source_measure_index: m for m in anchored.mappings}
        assert by_src[1].gp_measure_index == 4
        assert "anchored" in by_src[1].reason_codes
        assert [m.gp_measure_index for m in free.mappings] != [
            m.gp_measure_index for m in anchored.mappings
        ]

    def test_anchor_conflicting_with_monotonicity_is_rejected_by_the_aligner(self):
        source = [_src(i, i * 2.0, (i + 1) * 2.0) for i in range(4)]
        gp = [_gp(i) for i in range(4)]
        with pytest.raises(StructureError) as exc:
            align_measures(
                source,
                gp,
                anchors=[
                    AnchorConstraint(source_measure_index=1, gp_measure_index=3),
                    AnchorConstraint(source_measure_index=2, gp_measure_index=1),
                ],
            )
        assert exc.value.code == "structure_non_monotonic_anchors"


# --------------------------------------------------------------------------
# Consensus must not require equal raw tempo-event counts
# --------------------------------------------------------------------------

def _midi(tempo_us: int = 500_000, n_measures: int = 8, extra_redundant_tempo: bool = False) -> bytes:
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=tempo_us, time=0))
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    for measure in range(n_measures):
        for beat in range(4):
            if extra_redundant_tempo and beat == 0:
                track.append(mido.MetaMessage("set_tempo", tempo=tempo_us, time=0))
            track.append(mido.Message("note_on", note=60, velocity=80, time=0))
            track.append(mido.Message("note_off", note=60, velocity=0, time=480))
    track.append(mido.MetaMessage("end_of_track", time=0))
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


class TestConsensusNormalization:
    def test_redundant_tempo_events_do_not_create_a_conflict(self):
        from reference_time.consensus import build_consensus
        from reference_time.midi_tempo import extract_tempo_evidence

        a = extract_tempo_evidence(_midi(), "l1", "1" * 64)
        b = extract_tempo_evidence(_midi(extra_redundant_tempo=True), "l2", "2" * 64)
        assert len(a.tempo_events) != len(b.tempo_events)
        consensus = build_consensus([a, b])
        assert consensus.decision.value == "agreed", consensus.conflict_regions

    def test_disagreeing_tempo_produces_regional_conflict(self):
        from reference_time.consensus import build_consensus
        from reference_time.midi_tempo import extract_tempo_evidence

        a = extract_tempo_evidence(_midi(500_000), "l1", "1" * 64)
        b = extract_tempo_evidence(_midi(600_000), "l2", "2" * 64)
        consensus = build_consensus([a, b])
        assert consensus.decision.value == "conflict"
        assert consensus.conflict_regions
        assert any("start_seconds" in json.dumps(r) for r in consensus.conflict_regions)

    def test_measure_boundary_disagreement_is_reported(self):
        from reference_time.consensus import build_consensus
        from reference_time.midi_tempo import extract_tempo_evidence

        a = extract_tempo_evidence(_midi(500_000, n_measures=8), "l1", "1" * 64)
        b = extract_tempo_evidence(_midi(500_000, n_measures=16), "l2", "2" * 64)
        consensus = build_consensus([a, b])
        metrics = next(iter(consensus.agreement_metrics.values()))
        assert "measure_boundary_comparison" in metrics
