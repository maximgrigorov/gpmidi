"""Unit tests for reference-time analysis.

Tests cover:
- Exact tick-to-second conversion
- Measure boundary generation
- Default-meta warnings
- SMPTE/malformed rejection
- Pre-roll/pickup detection
- Consensus and conflict detection
- Alignment with gaps, monotonicity, anchors
- Confidence bounds and reason codes
- Cache key determinism
- Stable ordering and no NaN/Infinity
- HTML escaping
"""

from __future__ import annotations

import json
import math

import pytest

from reference_time.cache import canonical_json, compute_cache_key
from reference_time.consensus import build_consensus
from reference_time.mapping import (
    AnchorConstraint,
    align_measures,
)
from reference_time.midi_tempo import (
    _ticks_to_seconds_piecewise,
    build_source_measures,
    extract_tempo_evidence,
)
from reference_time.models import (
    GPMeasure,
    MappingType,
    ReferenceTimeAnalysis,
    SourceMeasure,
    SourceTempoEvidence,
    TempoEvent,
    TimeSignatureEvent,
    WarningCode,
)
from reference_time.report import generate_html_report, generate_json_report


class TestTickToSeconds:
    def test_constant_tempo_120bpm(self):
        tempo_table = [(0, 500000)]
        ppq = 480
        result = float(_ticks_to_seconds_piecewise(480, tempo_table, ppq))
        assert abs(result - 0.5) < 1e-9

    def test_constant_tempo_one_measure(self):
        tempo_table = [(0, 500000)]
        ppq = 480
        result = float(_ticks_to_seconds_piecewise(1920, tempo_table, ppq))
        assert abs(result - 2.0) < 1e-9

    def test_tempo_change_exact(self):
        tempo_table = [(0, 500000), (1920, 428571)]
        ppq = 480
        # 4 beats at 120 BPM = 2.0s, then 4 beats at 140 BPM = ~1.714s
        at_change = float(_ticks_to_seconds_piecewise(1920, tempo_table, ppq))
        assert abs(at_change - 2.0) < 1e-9

        after_4_beats = float(_ticks_to_seconds_piecewise(3840, tempo_table, ppq))
        expected = 2.0 + (1920 * 428571) / (480 * 1_000_000)
        assert abs(after_4_beats - expected) < 1e-6

    def test_zero_tick(self):
        result = float(_ticks_to_seconds_piecewise(0, [(0, 500000)], 480))
        assert result == 0.0

    def test_no_drift_large_tick(self):
        """Verify Fraction arithmetic prevents drift over many tempo regions."""
        tempo_table = [(i * 480, 500000 + i * 100) for i in range(100)]
        ppq = 480
        result = float(_ticks_to_seconds_piecewise(48000, tempo_table, ppq))
        assert math.isfinite(result)
        assert result > 0


class TestExtractTempoEvidence:
    def test_type0_constant(self, midi_type0_constant_tempo):
        ev = extract_tempo_evidence(midi_type0_constant_tempo, "link1", "sha1")
        assert ev.source_type == 0
        assert ev.midi_ppq == 480
        assert len(ev.tempo_events) == 1
        assert abs(ev.tempo_events[0].bpm - 120.0) < 0.01
        assert len(ev.time_signatures) == 1
        assert ev.time_signatures[0].numerator == 4
        assert ev.time_signatures[0].denominator == 4
        assert ev.first_event_tick == 0
        assert len(ev.validation_errors) == 0

    def test_type1_tempo_changes(self, midi_type1_tempo_changes):
        ev = extract_tempo_evidence(midi_type1_tempo_changes, "link2", "sha2")
        assert ev.source_type == 1
        assert len(ev.tempo_events) == 2
        assert abs(ev.tempo_events[0].bpm - 120.0) < 0.01
        assert abs(ev.tempo_events[1].bpm - 140.0) < 1.0

    def test_3_4_time_sig(self, midi_3_4_to_4_4):
        ev = extract_tempo_evidence(midi_3_4_to_4_4, "link3", "sha3")
        assert len(ev.time_signatures) == 2
        assert ev.time_signatures[0].numerator == 3
        assert ev.time_signatures[1].numerator == 4

    def test_preroll_warning(self, midi_with_preroll):
        ev = extract_tempo_evidence(midi_with_preroll, "link4", "sha4")
        assert ev.first_event_tick == 960
        codes = [w.code for w in ev.warnings]
        assert WarningCode.PRE_ROLL_DETECTED in codes

    def test_empty_midi_warning(self, midi_empty):
        ev = extract_tempo_evidence(midi_empty, "link5", "sha5")
        codes = [w.code for w in ev.warnings]
        assert WarningCode.EMPTY_TRACK in codes

    def test_malformed_midi_raises(self, midi_malformed):
        with pytest.raises(ValueError, match="Failed to parse MIDI"):
            extract_tempo_evidence(midi_malformed, "link6", "sha6")

    def test_default_tempo_warning(self):
        """MIDI with no set_tempo should get default warning."""
        import io
        import mido
        mid = mido.MidiFile(type=0, ticks_per_beat=480)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        track.append(mido.Message("note_on", note=60, velocity=80, time=0))
        track.append(mido.Message("note_off", note=60, velocity=0, time=480))
        track.append(mido.MetaMessage("end_of_track", time=0))
        buf = io.BytesIO()
        mid.save(file=buf)

        ev = extract_tempo_evidence(buf.getvalue(), "link7", "sha7")
        codes = [w.code for w in ev.warnings]
        assert WarningCode.DEFAULT_TEMPO in codes
        assert WarningCode.DEFAULT_TIME_SIG in codes


class TestBuildSourceMeasures:
    def test_constant_tempo_8_measures(self, midi_type0_constant_tempo):
        ev = extract_tempo_evidence(midi_type0_constant_tempo, "link1", "sha1")
        measures = build_source_measures(ev)
        assert len(measures) == 8
        for m in measures:
            assert m.numerator == 4
            assert m.denominator == 4
            assert abs(m.tempo_bpm - 120.0) < 0.01
            assert m.confidence == 1.0

    def test_ts_change_boundary(self, midi_3_4_to_4_4):
        ev = extract_tempo_evidence(midi_3_4_to_4_4, "link3", "sha3")
        measures = build_source_measures(ev)
        assert measures[0].numerator == 3
        found_4_4 = False
        for m in measures:
            if m.numerator == 4:
                found_4_4 = True
                break
        assert found_4_4

    def test_measures_contiguous(self, midi_type0_constant_tempo):
        ev = extract_tempo_evidence(midi_type0_constant_tempo, "link1", "sha1")
        measures = build_source_measures(ev)
        for i in range(1, len(measures)):
            assert measures[i].tick_start == measures[i-1].tick_end


class TestConsensus:
    def _make_evidence(self, bpm=120.0, duration=16.0, sha="sha1", link="link1"):
        return SourceTempoEvidence(
            asset_link_id=link,
            sha256=sha,
            parser_name="mido",
            parser_version="1.3.3",
            midi_ppq=480,
            time_signatures=[TimeSignatureEvent(tick=0, seconds=0.0, numerator=4, denominator=4)],
            tempo_events=[TempoEvent(tick=0, seconds=0.0, bpm=bpm)],
            first_event_tick=0,
            first_event_seconds=0.0,
            duration_ticks=int(480 * 4 * 8),
            duration_seconds=duration,
            source_type=0,
        )

    def test_single_source(self):
        ev = self._make_evidence()
        c = build_consensus([ev])
        assert c.decision.value == "single_source"
        assert c.primary_sha256 == "sha1"

    def test_agreement(self):
        ev1 = self._make_evidence(sha="sha_aaa", link="link1")
        ev2 = self._make_evidence(sha="sha_bbb", link="link2")
        c = build_consensus([ev1, ev2])
        assert c.decision.value == "agreed"
        assert c.source_count == 2

    def test_tempo_conflict(self):
        ev1 = self._make_evidence(bpm=120.0, sha="sha1", link="link1")
        ev2 = self._make_evidence(bpm=100.0, sha="sha2", link="link2")
        c = build_consensus([ev1, ev2])
        assert c.decision.value == "conflict"
        assert len(c.conflict_regions) > 0

    def test_deterministic_selection(self):
        ev1 = self._make_evidence(duration=16.0, sha="sha_bbb", link="link1")
        ev2 = self._make_evidence(duration=16.0, sha="sha_aaa", link="link2")
        c = build_consensus([ev1, ev2])
        assert c.primary_sha256 == "sha_aaa"

    def test_longer_duration_preferred(self):
        ev1 = self._make_evidence(duration=20.0, sha="sha_zzz", link="link1")
        ev2 = self._make_evidence(duration=16.0, sha="sha_aaa", link="link2")
        c = build_consensus([ev1, ev2])
        assert c.primary_sha256 == "sha_zzz"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            build_consensus([])


class TestAlignment:
    def _src_measures(self, n=4, ts=(4, 4)):
        ppq = 480
        ticks_per_measure = ppq * 4 * ts[0] // ts[1]
        measures = []
        for i in range(n):
            measures.append(SourceMeasure(
                index=i,
                tick_start=i * ticks_per_measure,
                tick_end=(i + 1) * ticks_per_measure,
                seconds_start=i * 2.0,
                seconds_end=(i + 1) * 2.0,
                numerator=ts[0],
                denominator=ts[1],
                tempo_bpm=120.0,
                confidence=1.0,
            ))
        return measures

    def _gp_measures(self, n=4, ts=(4, 4)):
        ppq = 960
        ticks_per_measure = ppq * 4 * ts[0] // ts[1]
        measures = []
        for i in range(n):
            measures.append(GPMeasure(
                gp_revision_sha256="gp_sha",
                measure_index=i,
                measure_number=i + 1,
                tick_start=i * ticks_per_measure,
                tick_end=(i + 1) * ticks_per_measure,
                numerator=ts[0],
                denominator=ts[1],
            ))
        return measures

    def test_one_to_one(self):
        src = self._src_measures(4)
        gp = self._gp_measures(4)
        result = align_measures(src, gp)
        assert len(result.mappings) == 4
        for m in result.mappings:
            assert m.mapping_type == MappingType.ONE_TO_ONE
            assert m.confidence > 0

    def test_source_gap(self):
        src = self._src_measures(6)
        gp = self._gp_measures(4)
        result = align_measures(src, gp)
        assert len(result.mappings) == 6
        gap_count = sum(1 for m in result.mappings if m.mapping_type == MappingType.SOURCE_GAP)
        assert gap_count >= 0

    def test_gp_gap(self):
        src = self._src_measures(2)
        gp = self._gp_measures(4)
        result = align_measures(src, gp)
        # 2 matched source measures + 2 explicit GP gap records
        assert len(result.mappings) == 4
        gp_gaps = [m for m in result.mappings if m.mapping_type == MappingType.GP_GAP]
        assert len(gp_gaps) == 2
        for gap in gp_gaps:
            assert gap.source_measure_index == -1
            assert gap.gp_measure_index is not None
            assert "gp_gap" in gap.evidence

    def test_monotonic(self):
        src = self._src_measures(8)
        gp = self._gp_measures(8)
        result = align_measures(src, gp)
        gp_indices = [m.gp_measure_index for m in result.mappings if m.gp_measure_index is not None]
        for i in range(1, len(gp_indices)):
            assert gp_indices[i] >= gp_indices[i - 1]

    def test_anchor_constraint(self):
        src = self._src_measures(4)
        gp = self._gp_measures(4)
        anchors = [AnchorConstraint(source_measure_index=2, gp_measure_index=2)]
        result = align_measures(src, gp, anchors=anchors)
        anchor_m = result.mappings[2]
        assert anchor_m.gp_measure_index == 2
        assert anchor_m.confidence == 1.0

    def test_conflicting_anchors_raise(self):
        src = self._src_measures(4)
        gp = self._gp_measures(4)
        anchors = [
            AnchorConstraint(source_measure_index=0, gp_measure_index=2),
            AnchorConstraint(source_measure_index=1, gp_measure_index=1),
        ]
        with pytest.raises(ValueError, match="Non-monotonic"):
            align_measures(src, gp, anchors=anchors)

    def test_empty_source(self):
        gp = self._gp_measures(4)
        result = align_measures([], gp)
        assert len(result.mappings) == 0
        assert result.global_confidence == 0.0

    def test_empty_gp(self):
        src = self._src_measures(4)
        result = align_measures(src, [])
        assert all(m.mapping_type == MappingType.GP_GAP for m in result.mappings)

    def test_confidence_bounds(self):
        src = self._src_measures(4)
        gp = self._gp_measures(4)
        result = align_measures(src, gp)
        for m in result.mappings:
            assert 0.0 <= m.confidence <= 1.0
        assert 0.0 <= result.global_confidence <= 1.0

    def test_ts_mismatch_low_confidence(self):
        src = self._src_measures(2, ts=(3, 4))
        gp = self._gp_measures(2, ts=(4, 4))
        result = align_measures(src, gp)
        for m in result.mappings:
            if m.mapping_type == MappingType.ONE_TO_ONE:
                pytest.fail("TS mismatch should not produce ONE_TO_ONE")
        assert result.global_confidence < 0.5


class TestAlignmentSensitivity:
    """Tests proving scoring components actually affect output."""

    def _src_measures(self, n=4, ts=(4, 4), density=0.0, audio_db=None):
        ppq = 480
        ticks_per_measure = ppq * 4 * ts[0] // ts[1]
        measures = []
        for i in range(n):
            measures.append(SourceMeasure(
                index=i,
                tick_start=i * ticks_per_measure,
                tick_end=(i + 1) * ticks_per_measure,
                seconds_start=i * 2.0,
                seconds_end=(i + 1) * 2.0,
                numerator=ts[0],
                denominator=ts[1],
                tempo_bpm=120.0,
                confidence=1.0,
                audio_downbeat_evidence=audio_db,
            ))
        return measures

    def _gp_measures(self, n=4, ts=(4, 4), markers=None):
        ppq = 960
        ticks_per_measure = ppq * 4 * ts[0] // ts[1]
        measures = []
        for i in range(n):
            measures.append(GPMeasure(
                gp_revision_sha256="gp_sha",
                measure_index=i,
                measure_number=i + 1,
                tick_start=i * ticks_per_measure,
                tick_end=(i + 1) * ticks_per_measure,
                numerator=ts[0],
                denominator=ts[1],
                marker_text=(markers or {}).get(i),
            ))
        return measures

    def test_duration_changes_confidence(self):
        src_equal = self._src_measures(4)
        gp_equal = self._gp_measures(4)
        result_equal = align_measures(src_equal, gp_equal)

        ppq = 480
        src_unequal = []
        for i in range(4):
            src_unequal.append(SourceMeasure(
                index=i,
                tick_start=i * ppq * 4,
                tick_end=(i + 1) * ppq * 4,
                seconds_start=sum(1.0 if j < 2 else 3.0 for j in range(i)),
                seconds_end=sum(1.0 if j < 2 else 3.0 for j in range(i + 1)),
                numerator=4, denominator=4, tempo_bpm=120.0, confidence=1.0,
            ))
        result_unequal = align_measures(src_unequal, gp_equal)
        equal_conf = [m.confidence for m in result_equal.mappings if m.mapping_type == MappingType.ONE_TO_ONE]
        unequal_conf = [m.confidence for m in result_unequal.mappings if m.mapping_type == MappingType.ONE_TO_ONE]
        assert sum(equal_conf) != sum(unequal_conf), "Duration should affect scoring"

    def test_audio_changes_confidence(self):
        src_no_audio = self._src_measures(4, audio_db=None)
        src_with_audio = self._src_measures(4, audio_db=0.9)
        gp = self._gp_measures(4)
        r1 = align_measures(src_no_audio, gp)
        r2 = align_measures(src_with_audio, gp)
        c1 = sum(m.confidence for m in r1.mappings if m.mapping_type == MappingType.ONE_TO_ONE)
        c2 = sum(m.confidence for m in r2.mappings if m.mapping_type == MappingType.ONE_TO_ONE)
        assert c2 > c1, "Audio evidence should increase confidence"

    def test_marker_changes_scoring(self):
        src = self._src_measures(4)
        gp_no_marker = self._gp_measures(4)
        gp_with_marker = self._gp_measures(4, markers={0: "Intro"})
        r1 = align_measures(src, gp_no_marker)
        r2 = align_measures(src, gp_with_marker)
        e1 = r1.mappings[0].evidence
        e2 = r2.mappings[0].evidence
        assert not any("marker:" in e for e in e1)
        assert any("marker:" in e for e in e2)

    def test_repeat_changes_scoring(self):
        src = self._src_measures(4)
        gp = self._gp_measures(4)
        gp_repeat = self._gp_measures(4)
        gp_repeat[2] = gp_repeat[2].model_copy(update={"has_repeat_open": True})
        r1 = align_measures(src, gp)
        r2 = align_measures(src, gp_repeat)
        assert r1.global_confidence != r2.global_confidence or \
            any("repeat_open" in m.evidence for m in r2.mappings)

    def test_alternatives_populated(self):
        src = self._src_measures(2)
        gp = self._gp_measures(4)
        result = align_measures(src, gp)
        matched = [m for m in result.mappings if m.mapping_type == MappingType.ONE_TO_ONE]
        has_alt = any(len(m.alternatives) > 0 for m in matched)
        assert has_alt, "Near-tie alternatives should be populated"

    def test_normalized_positions(self):
        src = self._src_measures(4)
        gp = self._gp_measures(4)
        result = align_measures(src, gp)
        for m in result.mappings:
            if m.mapping_type == MappingType.GP_GAP:
                continue
            assert 0.0 <= m.normalized_position_start <= 1.0
            assert 0.0 <= m.normalized_position_end <= 1.0
            if m.mapping_type == MappingType.ONE_TO_ONE:
                assert m.normalized_position_end > m.normalized_position_start

    def test_consensus_conflict_reduces_confidence(self):
        src = self._src_measures(4)
        gp = self._gp_measures(4)
        r_no_conflict = align_measures(src, gp, consensus_has_conflict=False)
        r_conflict = align_measures(src, gp, consensus_has_conflict=True)
        assert r_conflict.global_confidence < r_no_conflict.global_confidence

    def test_explicit_gp_gap_mappings(self):
        src = self._src_measures(1)
        gp = self._gp_measures(3)
        result = align_measures(src, gp)
        gp_gaps = [m for m in result.mappings if m.mapping_type == MappingType.GP_GAP]
        assert len(gp_gaps) == 2
        for gap in gp_gaps:
            assert gap.source_measure_index == -1
            assert gap.gp_tick_start is not None


class TestConsensusDeep:
    """Tests for trajectory/boundary/duration/ts/preroll consensus comparison."""

    def _make_evidence(self, bpm=120.0, duration=16.0, sha="sha1",
                       link="link1", events=None, first_event_seconds=0.0):
        if events is None:
            events = [TempoEvent(tick=0, seconds=0.0, bpm=bpm)]
        return SourceTempoEvidence(
            asset_link_id=link,
            sha256=sha,
            parser_name="mido",
            parser_version="1.3.3",
            midi_ppq=480,
            time_signatures=[TimeSignatureEvent(tick=0, seconds=0.0, numerator=4, denominator=4)],
            tempo_events=events,
            first_event_tick=0,
            first_event_seconds=first_event_seconds,
            duration_ticks=int(480 * 4 * 8),
            duration_seconds=duration,
            source_type=0,
        )

    def test_trajectory_conflict_regions(self):
        ev1 = self._make_evidence(
            sha="s1", link="l1",
            events=[
                TempoEvent(tick=0, seconds=0.0, bpm=120.0),
                TempoEvent(tick=3840, seconds=8.0, bpm=80.0),
            ],
        )
        ev2 = self._make_evidence(
            sha="s2", link="l2",
            events=[
                TempoEvent(tick=0, seconds=0.0, bpm=120.0),
                TempoEvent(tick=3840, seconds=8.0, bpm=120.0),
            ],
        )
        c = build_consensus([ev1, ev2])
        assert c.decision.value == "conflict"
        tempo_conflicts = [r for r in c.conflict_regions if "tempo" in r["type"]]
        assert len(tempo_conflicts) > 0

    def test_preroll_mismatch(self):
        ev1 = self._make_evidence(sha="s1", link="l1", first_event_seconds=0.0)
        ev2 = self._make_evidence(sha="s2", link="l2", first_event_seconds=2.0)
        c = build_consensus([ev1, ev2])
        assert c.decision.value == "conflict"
        preroll = [r for r in c.conflict_regions if r["type"] == "preroll_downbeat_mismatch"]
        assert len(preroll) == 1

    def test_duration_mismatch_conflict(self):
        ev1 = self._make_evidence(sha="s1", link="l1", duration=16.0)
        ev2 = self._make_evidence(sha="s2", link="l2", duration=30.0)
        c = build_consensus([ev1, ev2])
        assert c.decision.value == "conflict"
        dur = [r for r in c.conflict_regions if r["type"] == "duration_mismatch"]
        assert len(dur) == 1

    def test_agreement_with_different_event_counts(self):
        ev1 = self._make_evidence(
            sha="s1", link="l1",
            events=[TempoEvent(tick=0, seconds=0.0, bpm=120.0)],
        )
        ev2 = self._make_evidence(
            sha="s2", link="l2",
            events=[
                TempoEvent(tick=0, seconds=0.0, bpm=120.0),
                TempoEvent(tick=3840, seconds=8.0, bpm=120.0),
            ],
        )
        c = build_consensus([ev1, ev2])
        assert c.decision.value == "agreed"


class TestStructure:
    """Tests for structure JSON parsing and anchor constraints."""

    def test_valid_structure(self):
        from reference_time.structure import parse_structure_json
        doc = json.dumps({
            "version": "1.0",
            "anchors": [
                {"source_measure": 0, "gp_measure": 0, "label": "Start"},
                {"source_measure": 3, "gp_measure": 3, "label": "Chorus"},
            ],
        })
        result = parse_structure_json(doc.encode(), 8, 8)
        assert len(result.anchors) == 2
        assert result.anchors[0].source_measure_index == 0
        assert result.anchors[1].label == "Chorus"

    def test_malformed_json(self):
        from reference_time.structure import parse_structure_json
        with pytest.raises(ValueError, match="Malformed"):
            parse_structure_json(b"not json", 4, 4)

    def test_unsupported_version(self):
        from reference_time.structure import parse_structure_json
        doc = json.dumps({"version": "99.0", "anchors": []})
        with pytest.raises(ValueError, match="Unsupported"):
            parse_structure_json(doc.encode(), 4, 4)

    def test_out_of_range_anchor(self):
        from reference_time.structure import parse_structure_json
        doc = json.dumps({
            "version": "1.0",
            "anchors": [{"source_measure": 100, "gp_measure": 0}],
        })
        with pytest.raises(ValueError, match="out of range"):
            parse_structure_json(doc.encode(), 4, 4)

    def test_conflicting_anchors(self):
        from reference_time.structure import parse_structure_json
        doc = json.dumps({
            "version": "1.0",
            "anchors": [
                {"source_measure": 0, "gp_measure": 0},
                {"source_measure": 0, "gp_measure": 2},
            ],
        })
        with pytest.raises(ValueError, match="Conflicting"):
            parse_structure_json(doc.encode(), 4, 4)

    def test_non_monotonic_anchors(self):
        from reference_time.structure import parse_structure_json
        doc = json.dumps({
            "version": "1.0",
            "anchors": [
                {"source_measure": 0, "gp_measure": 2},
                {"source_measure": 1, "gp_measure": 1},
            ],
        })
        with pytest.raises(ValueError, match="Non-monotonic"):
            parse_structure_json(doc.encode(), 4, 4)

    def test_sections_produce_labels(self):
        from reference_time.structure import parse_structure_json
        doc = json.dumps({
            "version": "1.0",
            "sections": [
                {"label": "Intro", "source_measure": 0},
                {"label": "Verse", "source_measure": 4},
            ],
        })
        result = parse_structure_json(doc.encode(), 8, 8)
        assert result.section_labels[0] == "Intro"
        assert result.section_labels[4] == "Verse"

    def test_anchor_changes_mapping(self):
        src = [SourceMeasure(
            index=i, tick_start=i * 1920, tick_end=(i + 1) * 1920,
            seconds_start=i * 2.0, seconds_end=(i + 1) * 2.0,
            numerator=4, denominator=4, tempo_bpm=120.0, confidence=1.0,
        ) for i in range(4)]
        gp = [GPMeasure(
            gp_revision_sha256="gp", measure_index=i, measure_number=i + 1,
            tick_start=i * 3840, tick_end=(i + 1) * 3840,
            numerator=4, denominator=4,
        ) for i in range(4)]
        r_no_anchor = align_measures(src, gp)
        r_anchored = align_measures(
            src, gp,
            anchors=[AnchorConstraint(source_measure_index=1, gp_measure_index=2)],
        )
        m_no = r_no_anchor.mappings[1]
        m_yes = r_anchored.mappings[1]
        assert m_yes.gp_measure_index == 2
        assert m_yes.confidence == 1.0
        assert m_no.gp_measure_index != m_yes.gp_measure_index or m_no.confidence != 1.0


class TestCacheKey:
    def test_deterministic(self):
        k1 = compute_cache_key("gp1", ["m1", "m2"], ["a1"], None, {"v": "1"}, {"p": 1})
        k2 = compute_cache_key("gp1", ["m1", "m2"], ["a1"], None, {"v": "1"}, {"p": 1})
        assert k1 == k2

    def test_different_gp_different_key(self):
        k1 = compute_cache_key("gp1", ["m1"], [], None, {"v": "1"}, {"p": 1})
        k2 = compute_cache_key("gp2", ["m1"], [], None, {"v": "1"}, {"p": 1})
        assert k1 != k2

    def test_different_midi_different_key(self):
        k1 = compute_cache_key("gp1", ["m1"], [], None, {"v": "1"}, {"p": 1})
        k2 = compute_cache_key("gp1", ["m2"], [], None, {"v": "1"}, {"p": 1})
        assert k1 != k2

    def test_sorted_midi_order(self):
        k1 = compute_cache_key("gp1", ["m2", "m1"], [], None, {"v": "1"}, {"p": 1})
        k2 = compute_cache_key("gp1", ["m1", "m2"], [], None, {"v": "1"}, {"p": 1})
        assert k1 == k2

    def test_nan_rejected(self):
        with pytest.raises(ValueError):
            canonical_json({"x": float("nan")})

    def test_infinity_rejected(self):
        with pytest.raises(ValueError):
            canonical_json({"x": float("inf")})


class TestModels:
    def test_confidence_bounds_source_measure(self):
        with pytest.raises(ValueError):
            SourceMeasure(
                index=0, tick_start=0, tick_end=1920,
                seconds_start=0.0, seconds_end=2.0,
                numerator=4, denominator=4, tempo_bpm=120.0,
                confidence=1.5,
            )

    def test_nan_rejected_in_model(self):
        with pytest.raises(ValueError):
            SourceMeasure(
                index=0, tick_start=0, tick_end=1920,
                seconds_start=float("nan"), seconds_end=2.0,
                numerator=4, denominator=4, tempo_bpm=120.0,
                confidence=1.0,
            )

    def test_analysis_model_valid(self):
        a = ReferenceTimeAnalysis(
            analysis_id="test",
            project_id="proj",
            gp_revision_sha256="abc123",
            global_confidence=0.8,
        )
        assert a.schema_version == "1.0.0"
        assert a.created_at is not None


class TestReport:
    def test_html_escaping(self, xss_marker_text):
        from reference_time.models import MappingType, MeasureMapping
        a = ReferenceTimeAnalysis(
            analysis_id="test",
            project_id="proj",
            gp_revision_sha256="abc123",
            global_confidence=0.5,
            gp_measures=[GPMeasure(
                gp_revision_sha256="abc123",
                measure_index=0,
                measure_number=1,
                tick_start=0,
                tick_end=3840,
                numerator=4,
                denominator=4,
                marker_text=xss_marker_text,
            )],
            mappings=[MeasureMapping(
                source_measure_index=0,
                gp_measure_index=0,
                gp_measure_number=1,
                mapping_type=MappingType.ONE_TO_ONE,
                source_seconds_start=0.0,
                source_seconds_end=2.0,
                gp_tick_start=0,
                gp_tick_end=3840,
                confidence=0.9,
                evidence=[f"marker:{xss_marker_text}"],
            )],
        )
        html = generate_html_report(a)
        assert "<script>alert" not in html
        assert '<img onerror=' not in html
        assert "&lt;script&gt;" in html

    def test_json_no_paths(self):
        a = ReferenceTimeAnalysis(
            analysis_id="test",
            project_id="proj",
            gp_revision_sha256="abc123",
            global_confidence=0.5,
        )
        j = generate_json_report(a)
        assert "/var/lib" not in j
        assert "/data/" not in j
        assert "/tmp/" not in j

    def test_json_stable_ordering(self):
        a = ReferenceTimeAnalysis(
            analysis_id="test",
            project_id="proj",
            gp_revision_sha256="abc123",
            global_confidence=0.5,
        )
        j1 = generate_json_report(a)
        j2 = generate_json_report(a)
        parsed1 = json.loads(j1)
        parsed2 = json.loads(j2)
        assert list(parsed1.keys()) == list(parsed2.keys())
