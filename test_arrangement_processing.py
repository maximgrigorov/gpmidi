from __future__ import annotations

from types import SimpleNamespace

from mido import Message, MetaMessage, MidiTrack

import arrangement_processing as processing
from arrangement_processing import (
    apply_expression_plan,
    build_arrangement_context,
    measure_spans,
    musical_note_signature,
    validate_plan,
)


def _track(*messages):
    track = MidiTrack()
    track.extend(messages)
    return track


def _absolute(track):
    tick = 0
    rows = []
    for message in track:
        tick += message.time
        rows.append((tick, message))
    return rows


def _song():
    denominator = SimpleNamespace(value=4)
    signature = SimpleNamespace(numerator=4, denominator=denominator)
    headers = [
        SimpleNamespace(start=960, timeSignature=signature),
        SimpleNamespace(start=4800, timeSignature=signature),
    ]
    return SimpleNamespace(measureHeaders=headers, tempo=120)


def test_module_has_no_network_or_coder_model_planner():
    source = open(processing.__file__, encoding="utf-8").read()
    assert "requests" not in source
    assert "qwen3-coder-next" not in source
    assert not hasattr(processing, "ArrangementPlanner")


def test_measure_spans_use_gp_canonical_ticks():
    assert measure_spans(_song()) == {1: (0, 3840), 2: (3840, 7680)}


def test_context_summarizes_already_mapped_tracks_without_mutation():
    midi = _track(
        MetaMessage("track_name", name="Solo", time=0),
        Message("note_on", note=24, velocity=100, time=0),
        Message("note_off", note=24, velocity=0, time=10),
        Message("note_on", note=60, velocity=70, time=10),
        Message("note_off", note=60, velocity=0, time=120),
        Message("note_on", note=62, velocity=90, time=3710),
        Message("note_off", note=62, velocity=0, time=120),
    )
    before = [message.copy() for message in midi]

    context = build_arrangement_context(
        _song(), [{"track_name": "Solo", "track_type": "GUITAR", "midi_track": midi}],
        service_notes={"GUITAR": {24}},
    )

    assert list(midi) == before
    assert context["processing_order"] == "target_articulations_then_hermes_draft"
    assert context["tracks"][0]["measures"] == [
        {"measure": 1, "notes": 1, "mean_velocity": 70, "onset_beats": [1.021]},
        {"measure": 2, "notes": 1, "mean_velocity": 90, "onset_beats": [1.01]},
    ]


def test_validate_plan_clamps_untrusted_draft_and_requires_known_tracks():
    plan = validate_plan(
        {
            "summary": "x" * 500,
            "global_velocity_shift": 999,
            "measure_energy": [
                {"measure_start": 1, "measure_end": 2, "velocity_shift": -99, "accents": [0, 1, 9]},
            ],
            "tracks": [
                {"track": "Solo", "role": "lead", "velocity_shift": 99, "variance": 99},
                {"track": "Invented", "role": "drums", "velocity_shift": 0, "variance": 0},
            ],
            "solo_humanization": {"enabled": True},
            "rewrite_suggestions": [{"proposal": "add cymbal hits"}],
        },
        track_names=["Solo"], measure_count=2, allow_solo_humanization=True,
    )

    assert plan["global_velocity_shift"] == 12
    assert [row["measure"] for row in plan["measure_energy"]] == [1, 2]
    assert all(row["velocity_shift"] == -12 for row in plan["measure_energy"])
    assert all(row["accents"] == [1.0, 9.0] for row in plan["measure_energy"])
    assert plan["tracks"] == [{"track": "Solo", "role": "lead", "velocity_shift": 16, "variance": 12}]
    assert plan["solo_humanization"] == {"enabled": True}
    assert "rewrite_suggestions" not in plan


def test_expression_runs_after_mapping_preserving_keyswitch_reserved_zone_and_timing():
    midi = _track(
        Message("note_on", note=24, velocity=100, time=0),
        Message("note_off", note=24, velocity=0, time=10),
        Message("note_on", note=60, velocity=80, time=10),
        Message("note_off", note=60, velocity=0, time=100),
        Message("note_on", note=62, velocity=127, time=10),
        Message("note_off", note=62, velocity=0, time=100),
    )
    before_ticks = [(tick, msg.type, getattr(msg, "note", None)) for tick, msg in _absolute(midi)]
    before_signature = musical_note_signature(midi, {24})
    plan = {
        "global_velocity_shift": 10,
        "measure_energy": [],
        "tracks": [{"track": "Guitar Solo", "role": "lead", "velocity_shift": 5, "variance": 0}],
        "solo_humanization": {"enabled": True},
    }

    stats = apply_expression_plan(
        midi, "Guitar Solo", "GUITAR", plan, {1: (0, 3840)},
        seed=7, service_notes={24},
    )

    after_ticks = [(tick, msg.type, getattr(msg, "note", None)) for tick, msg in _absolute(midi)]
    note_ons = [(tick, msg.note, msg.velocity) for tick, msg in _absolute(midi)
                if msg.type == "note_on" and msg.velocity > 0]
    assert after_ticks == before_ticks
    assert musical_note_signature(midi, {24}) == before_signature
    assert note_ons == [(0, 24, 100), (20, 60, 95), (130, 62, 127)]
    assert stats == {
        "changed_notes": 1,
        "service_events_preserved": 1,
        "reserved_notes_preserved": 1,
        "mean_velocity_before": 103.5,
        "mean_velocity_after": 111.0,
    }


def test_drum_expression_never_adds_or_moves_hits():
    midi = _track(
        Message("note_on", note=36, velocity=95, time=0),
        Message("note_off", note=36, velocity=0, time=60),
        Message("note_on", note=49, velocity=95, time=420),
        Message("note_off", note=49, velocity=0, time=60),
    )
    before = musical_note_signature(midi)
    plan = {
        "global_velocity_shift": 0,
        "measure_energy": [{"measure": 1, "velocity_shift": 3, "accents": [1.0]}],
        "tracks": [{"track": "Drums", "role": "drums", "velocity_shift": 2, "variance": 2}],
        "solo_humanization": {"enabled": False},
    }

    apply_expression_plan(midi, "Drums", "DRUMS", plan, {1: (0, 3840)}, seed=11)

    assert musical_note_signature(midi) == before
    assert [(tick, msg.note) for tick, msg in _absolute(midi)
            if msg.type == "note_on" and msg.velocity > 0] == [(0, 36), (480, 49)]
