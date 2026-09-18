from __future__ import annotations

from pathlib import Path

import pytest
from mido import Message, MetaMessage, MidiFile, MidiTrack

import solo_expression_humanize as solo


def _solo_track() -> MidiTrack:
    track = MidiTrack()
    track.extend([
        MetaMessage("track_name", name="Guitar (Solo)", time=0),
        Message("note_on", note=12, velocity=95, time=0),
        Message("note_off", note=12, velocity=0, time=8),
        Message("pitchwheel", pitch=0, time=0),
        Message("control_change", control=1, value=0, time=0),
        Message("note_on", note=60, velocity=95, time=12),
        Message("control_change", control=1, value=8, time=12),
        Message("control_change", control=1, value=16, time=12),
        Message("pitchwheel", pitch=2341, time=72),
        Message("control_change", control=1, value=24, time=12),
        Message("control_change", control=1, value=16, time=12),
        Message("control_change", control=1, value=8, time=12),
        Message("control_change", control=1, value=0, time=12),
        Message("note_off", note=60, velocity=0, time=84),
        Message("pitchwheel", pitch=0, time=0),
        Message("note_on", note=64, velocity=95, time=24),
        Message("note_off", note=64, velocity=0, time=120),
        Message("note_on", note=67, velocity=63, time=24),
        Message("note_off", note=67, velocity=0, time=120),
        MetaMessage("end_of_track", time=0),
    ])
    return track


def _plain_track() -> MidiTrack:
    return MidiTrack([
        MetaMessage("track_name", name="Drums", time=0),
        Message("note_on", channel=9, note=36, velocity=95, time=0),
        Message("note_off", channel=9, note=36, velocity=0, time=120),
        MetaMessage("end_of_track", time=0),
    ])


def _long_solo_track(*, authored_bend: bool = False, overlap: bool = False) -> MidiTrack:
    track = MidiTrack([
        MetaMessage("track_name", name="Lead Guitar", time=0),
        MetaMessage("set_tempo", tempo=800_000, time=0),  # 75 BPM
        Message("pitchwheel", pitch=0, time=0),
        Message("note_on", note=64, velocity=95, time=0),
    ])
    if overlap:
        track.extend([
            Message("note_on", note=67, velocity=88, time=240),
            Message("note_off", note=67, velocity=0, time=960),
            Message("note_off", note=64, velocity=0, time=720),
        ])
    elif authored_bend:
        track.extend([
            Message("pitchwheel", pitch=1170, time=480),
            Message("pitchwheel", pitch=0, time=480),
            Message("note_off", note=64, velocity=0, time=960),
        ])
    else:
        track.append(Message("note_off", note=64, velocity=0, time=1920))
    track.append(MetaMessage("end_of_track", time=0))
    return track


def _absolute(track: MidiTrack, predicate=lambda _m: True):
    tick = 0
    rows = []
    for message in track:
        tick += message.time
        if predicate(message):
            rows.append((tick, message.dict()))
    return rows


def _note_signature(track: MidiTrack):
    tick = 0
    rows = []
    for message in track:
        tick += message.time
        if message.type in {"note_on", "note_off"}:
            semantic = "off" if message.type == "note_off" or message.velocity == 0 else "on"
            rows.append((tick, semantic, message.note))
    return rows


def _pitch_anchors(track: MidiTrack):
    return [(tick, row["pitch"]) for tick, row in _absolute(track) if row["type"] == "pitchwheel"]


def _cc1(track: MidiTrack):
    return [(tick, row["value"]) for tick, row in _absolute(track) if row["type"] == "control_change" and row["control"] == 1]


def _note_velocities(track: MidiTrack):
    return [(tick, row["note"], row["velocity"]) for tick, row in _absolute(track)
            if row["type"] == "note_on" and row["velocity"] > 0]


def test_requires_at_least_one_explicit_dimension():
    with pytest.raises(ValueError, match="at least one"):
        solo.humanize_solo_track(_solo_track(), ticks_per_beat=960, seed=7)


def test_file_processing_refuses_in_place_overwrite(tmp_path: Path):
    source = tmp_path / "accepted-baseline.mid"
    midi = MidiFile(type=1, ticks_per_beat=960)
    midi.tracks.extend([_plain_track(), _solo_track()])
    midi.save(source)

    with pytest.raises(ValueError, match="distinct"):
        solo.process_file(source, source, seed=23, velocity=True)


def test_velocity_mode_changes_only_musical_attack_velocity():
    baseline = _solo_track()
    enriched, stats = solo.humanize_solo_track(
        baseline, ticks_per_beat=960, seed=19, velocity=True,
    )

    assert _note_signature(enriched) == _note_signature(baseline)
    before = _note_velocities(baseline)
    after = _note_velocities(enriched)
    assert after[0] == before[0]  # Hydra C-1 service note is immutable.
    assert [(t, n) for t, n, _v in after] == [(t, n) for t, n, _v in before]
    assert any(a[2] != b[2] for a, b in zip(after[1:], before[1:]))
    assert all(20 <= velocity <= 119 for _tick, note, velocity in after if note not in solo.HYDRA_SERVICE_NOTES)
    assert _pitch_anchors(enriched) == _pitch_anchors(baseline)
    assert _cc1(enriched) == _cc1(baseline)
    assert stats["velocity_events_changed"] > 0
    assert stats["note_timing_preserved"] is True


def test_velocity_mode_leaves_rake_and_pinch_zone_attacks_untouched():
    """120-126 = Rake, 127 = Pinch на sustain у Hydra — артикуляция, не громкость.

    Раньше _humanize_velocity зажимал такие атаки в VELOCITY_CEILING=119 и
    pinch harmonic превращался в обычный громкий sustain; тесты подавали на
    вход только <=119 и пробел не ловили.
    """
    track = MidiTrack()
    track.extend([
        MetaMessage("track_name", name="Guitar (Solo)", time=0),
        Message("note_on", note=60, velocity=127, time=0),   # pinch
        Message("note_off", note=60, velocity=0, time=120),
        Message("note_on", note=64, velocity=123, time=0),   # rake zone
        Message("note_off", note=64, velocity=0, time=120),
        Message("note_on", note=67, velocity=95, time=0),    # normal attack
        Message("note_off", note=67, velocity=0, time=120),
        MetaMessage("end_of_track", time=0),
    ])

    enriched, stats = solo.humanize_solo_track(
        track, ticks_per_beat=960, seed=19, velocity=True,
    )

    velocities = {note: velocity for _tick, note, velocity in _note_velocities(enriched)}
    assert velocities[60] == 127
    assert velocities[64] == 123
    assert velocities[67] != 95  # normal attacks are still humanized
    assert stats["velocity_events_changed"] == 1


def test_pitch_mode_densifies_only_between_immutable_anchors():
    baseline = _solo_track()
    enriched, stats = solo.humanize_solo_track(
        baseline, ticks_per_beat=960, seed=19, pitch_bend=True,
    )

    assert _note_signature(enriched) == _note_signature(baseline)
    anchors = _pitch_anchors(baseline)
    output = _pitch_anchors(enriched)
    assert all(anchor in output for anchor in anchors)
    assert len(output) > len(anchors)
    assert _note_velocities(enriched) == _note_velocities(baseline)
    assert _cc1(enriched) == _cc1(baseline)
    assert stats["pitch_anchors_preserved"] is True
    assert stats["pitch_events_added"] == len(output) - len(anchors)


def test_finger_vibrato_adds_irregular_micro_bends_only_to_eligible_long_note():
    baseline = _long_solo_track()
    first, stats = solo.humanize_solo_track(
        baseline,
        ticks_per_beat=960,
        seed=23,
        finger_vibrato=True,
        finger_vibrato_style="expressive",
        pitch_bend_range=7,
    )
    second, second_stats = solo.humanize_solo_track(
        baseline,
        ticks_per_beat=960,
        seed=23,
        finger_vibrato=True,
        finger_vibrato_style="expressive",
        pitch_bend_range=7,
    )

    assert _note_signature(first) == _note_signature(baseline)
    assert _absolute(first) == _absolute(second)
    assert stats == second_stats
    assert stats["finger_vibrato_notes"] == 1
    assert stats["finger_vibrato_events_added"] > 2
    assert all(anchor in _pitch_anchors(first) for anchor in _pitch_anchors(baseline))
    generated = [pitch for _tick, pitch in _pitch_anchors(first) if pitch]
    assert generated
    assert len(set(pitch for pitch in generated if pitch > 0)) >= 4
    assert max(generated) <= round(8192 * 0.38 / 7)
    assert min(generated) > -round(8192 * 0.38 / 7)
    assert _pitch_anchors(first)[-1] == (1920, 0)


@pytest.mark.parametrize("track", [
    _long_solo_track(authored_bend=True),
    _long_solo_track(overlap=True),
])
def test_finger_vibrato_skips_authored_bends_and_polyphonic_spans(track: MidiTrack):
    enriched, stats = solo.humanize_solo_track(
        track,
        ticks_per_beat=960,
        seed=23,
        finger_vibrato=True,
        finger_vibrato_style="expressive",
    )

    assert _absolute(enriched) == _absolute(track)
    assert stats["finger_vibrato_notes"] == 0
    assert stats["finger_vibrato_events_added"] == 0


def test_modulation_mode_keeps_event_ticks_and_zero_resets():
    baseline = _solo_track()
    enriched, stats = solo.humanize_solo_track(
        baseline, ticks_per_beat=960, seed=19, modulation=True,
    )

    assert _note_signature(enriched) == _note_signature(baseline)
    before = _cc1(baseline)
    after = _cc1(enriched)
    assert [tick for tick, _value in after] == [tick for tick, _value in before]
    assert [value for _tick, value in after if value == 0] == [value for _tick, value in before if value == 0]
    assert any(a[1] != b[1] for a, b in zip(after, before) if b[1] > 0)
    assert _note_velocities(enriched) == _note_velocities(baseline)
    assert _pitch_anchors(enriched) == _pitch_anchors(baseline)
    assert stats["modulation_events_changed"] > 0


def test_all_modes_are_deterministic_and_leave_non_solo_tracks_semantically_identical(tmp_path: Path):
    source = tmp_path / "baseline.mid"
    output_a = tmp_path / "candidate-a.mid"
    output_b = tmp_path / "candidate-b.mid"
    midi = MidiFile(type=1, ticks_per_beat=960)
    midi.tracks.extend([_plain_track(), _solo_track()])
    midi.save(source)

    manifest_a = solo.process_file(
        source, output_a, seed=23, velocity=True, pitch_bend=True, modulation=True,
    )
    manifest_b = solo.process_file(
        source, output_b, seed=23, velocity=True, pitch_bend=True, modulation=True,
    )
    candidate_a = MidiFile(output_a)
    baseline = MidiFile(source)

    assert output_a.read_bytes() == output_b.read_bytes()
    assert _absolute(candidate_a.tracks[0]) == _absolute(baseline.tracks[0])
    assert _note_signature(candidate_a.tracks[1]) == _note_signature(baseline.tracks[1])
    assert manifest_a["source_sha256"] == manifest_b["source_sha256"]
    assert manifest_a["output_sha256"] == manifest_b["output_sha256"]
    assert manifest_a["dimensions"] == {
        "velocity": True,
        "pitch_bend": True,
        "modulation": True,
        "finger_vibrato": False,
        "finger_vibrato_style": None,
    }
    assert manifest_a["safety"] == {
        "note_timing_preserved": True,
        "note_pitches_preserved": True,
        "note_durations_preserved": True,
        "hydra_service_notes_preserved": True,
        "non_solo_tracks_preserved": True,
        "pitch_anchors_preserved": True,
    }


def test_file_processing_can_emit_conductor_plus_changed_track_only(tmp_path: Path):
    source = tmp_path / "accepted-baseline.mid"
    output = tmp_path / "changed-only.mid"
    conductor = MidiTrack([
        MetaMessage("track_name", name="Conductor", time=0),
        MetaMessage("set_tempo", tempo=800_000, time=0),
        MetaMessage("time_signature", numerator=4, denominator=4, time=0),
        MetaMessage("end_of_track", time=7680),
    ])
    midi = MidiFile(type=1, ticks_per_beat=960)
    midi.tracks.extend([conductor, _long_solo_track(), _plain_track()])
    midi.save(source)

    manifest = solo.process_file(
        source,
        output,
        seed=23,
        finger_vibrato=True,
        finger_vibrato_style="expressive",
        changed_tracks_only=True,
    )
    result = MidiFile(output)

    assert result.type == 1
    assert [solo._track_name(track) for track in result.tracks] == ["Conductor", "Lead Guitar"]
    assert manifest["output_mode"] == "changed_tracks_only"
    assert manifest["conductor_track_preserved"] is True
    assert manifest["omitted_track_indices"] == [2]
    assert manifest["stats"]["finger_vibrato_notes"] == 1
