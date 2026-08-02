from __future__ import annotations

import json
from pathlib import Path

import mido
from transcription_spike.models import EventKind, TranscriptionEvent
from transcription_spike.restoration import (
    ApprovalDecision,
    CandidateStatus,
    build_candidate_patch,
    compile_approved_overlay,
)


def analysis_fixture() -> dict:
    return {
        "analysis_id": "analysis-1",
        "gp_revision_sha256": "a" * 64,
        "mappings": [
            {
                "source_measure_index": 0,
                "gp_measure_index": 4,
                "gp_measure_number": 5,
                "mapping_type": "one_to_one",
                "source_seconds_start": 10.0,
                "source_seconds_end": 12.0,
                "gp_tick_start": 3840,
                "gp_tick_end": 4800,
                "confidence": 0.9,
            },
            {
                "source_measure_index": 1,
                "mapping_type": "source_gap",
                "source_seconds_start": 12.0,
                "source_seconds_end": 14.0,
                "confidence": 0.1,
            },
        ],
    }


def test_candidate_patch_preserves_articulation_velocity_and_in_measure_timing() -> None:
    events = [
        TranscriptionEvent(
            onset_seconds=10.5,
            offset_seconds=10.75,
            kind=EventKind.PERCUSSIVE,
            instrument="drums",
            midi_pitch=46,
            event_class="open_hi_hat",
            velocity=73,
            confidence=0.82,
        )
    ]

    patch = build_candidate_patch(
        analysis_fixture(),
        events,
        source_identity="events-sha",
        minimum_mapping_confidence=0.5,
    )

    assert patch.schema_version == "gpmidi-restoration-patch-v1"
    assert patch.default_action == "review_required"
    assert len(patch.candidates) == 1
    candidate = patch.candidates[0]
    assert candidate.gp_measure_number == 5
    assert candidate.target_tick == 4080
    assert candidate.target_duration_ticks == 120
    assert candidate.midi_pitch == 46
    assert candidate.velocity == 73
    assert candidate.articulation == "open_hi_hat"
    assert candidate.confidence == 0.82 * 0.9
    assert candidate.status is CandidateStatus.PROPOSED
    assert patch.velocity_strategy == "preserve"


def test_confidence_velocity_strategy_adds_dynamics_without_random_humanization() -> None:
    events = [
        TranscriptionEvent(
            onset_seconds=10.5,
            kind=EventKind.PERCUSSIVE,
            instrument="drums",
            midi_pitch=42,
            event_class="hi_hat",
            velocity=100,
            confidence=0.82,
        )
    ]

    patch = build_candidate_patch(
        analysis_fixture(),
        events,
        source_identity="events-sha",
        velocity_strategy="confidence",
    )

    assert patch.velocity_strategy == "confidence"
    assert patch.candidates[0].velocity == 110


def test_source_gaps_and_low_confidence_mappings_never_become_candidates() -> None:
    events = [
        TranscriptionEvent(
            onset_seconds=12.5,
            kind=EventKind.PERCUSSIVE,
            instrument="drums",
            midi_pitch=42,
            event_class="hi_hat",
            velocity=80,
            confidence=0.9,
        )
    ]

    patch = build_candidate_patch(
        analysis_fixture(),
        events,
        source_identity="events-sha",
        minimum_mapping_confidence=0.5,
    )

    assert patch.candidates == []
    assert patch.rejected_event_count == 1


def test_overlay_contains_only_explicitly_approved_measures(tmp_path: Path) -> None:
    events = [
        TranscriptionEvent(
            onset_seconds=10.5,
            offset_seconds=10.75,
            kind=EventKind.PERCUSSIVE,
            instrument="drums",
            midi_pitch=46,
            event_class="open_hi_hat",
            velocity=73,
            confidence=0.82,
        )
    ]
    patch = build_candidate_patch(
        analysis_fixture(), events, source_identity="events-sha"
    )
    output = tmp_path / "approved.mid"

    summary = compile_approved_overlay(
        patch,
        [ApprovalDecision(gp_measure_number=5, approved=True)],
        output,
        ticks_per_beat=960,
    )

    midi = mido.MidiFile(output)
    notes = [
        message
        for track in midi.tracks
        for message in track
        if message.type == "note_on" and message.velocity > 0
    ]
    assert midi.type == 1
    assert midi.ticks_per_beat == 960
    assert [(note.note, note.velocity) for note in notes] == [(46, 73)]
    assert summary["approved_measure_count"] == 1
    assert summary["written_event_count"] == 1
    assert json.loads(patch.model_dump_json())["default_action"] == "review_required"


def test_unreviewed_patch_compiles_to_empty_overlay(tmp_path: Path) -> None:
    events = [
        TranscriptionEvent(
            onset_seconds=10.5,
            kind=EventKind.PERCUSSIVE,
            instrument="drums",
            midi_pitch=42,
            event_class="hi_hat",
            velocity=80,
            confidence=0.9,
        )
    ]
    patch = build_candidate_patch(
        analysis_fixture(), events, source_identity="events-sha"
    )

    summary = compile_approved_overlay(patch, [], tmp_path / "empty.mid")

    assert summary["written_event_count"] == 0


def test_overlay_rescales_reference_tempo_map_to_target_grid(tmp_path: Path) -> None:
    source = mido.MidiFile(type=1, ticks_per_beat=480)
    source_meta = mido.MidiTrack()
    source_meta.append(mido.MetaMessage("set_tempo", tempo=800_000, time=0))
    source_meta.append(mido.MetaMessage("set_tempo", tempo=700_000, time=480))
    source.tracks.append(source_meta)
    tempo_path = tmp_path / "tempo.mid"
    source.save(tempo_path)
    patch = build_candidate_patch(analysis_fixture(), [], source_identity="events-sha")

    output = tmp_path / "tempo-overlay.mid"
    summary = compile_approved_overlay(
        patch,
        [],
        output,
        ticks_per_beat=960,
        tempo_source=tempo_path,
    )

    midi = mido.MidiFile(output)
    tempo_events = []
    absolute = 0
    for message in midi.tracks[0]:
        absolute += message.time
        if message.type == "set_tempo":
            tempo_events.append((absolute, message.tempo))
    assert tempo_events == [(0, 800_000), (960, 700_000)]
    assert summary["tempo_event_count"] == 2
