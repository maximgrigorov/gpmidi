from __future__ import annotations

import io

import mido
from transcription_spike.midi_reference import extract_midi_reference
from transcription_spike.models import EventKind


def midi_bytes(mid: mido.MidiFile) -> bytes:
    buffer = io.BytesIO()
    mid.save(file=buffer)
    return buffer.getvalue()


def test_extracts_bass_note_bounds_in_absolute_seconds() -> None:
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.extend(
        [
            mido.MetaMessage("set_tempo", tempo=500_000, time=0),
            mido.Message("note_on", note=40, velocity=90, channel=0, time=0),
            mido.Message("note_off", note=40, velocity=0, channel=0, time=480),
        ]
    )

    result = extract_midi_reference(midi_bytes(mid), instrument="bass", kind=EventKind.PITCHED)

    assert result.midi_type == 0
    assert result.ticks_per_beat == 480
    assert result.warnings == ()
    assert len(result.events) == 1
    event = result.events[0]
    assert event.onset_seconds == 0.0
    assert event.offset_seconds == 0.5
    assert event.midi_pitch == 40
    assert event.velocity == 90


def test_type1_tempo_track_controls_note_track_seconds() -> None:
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    tempo = mido.MidiTrack()
    notes = mido.MidiTrack()
    mid.tracks.extend([tempo, notes])
    tempo.extend(
        [
            mido.MetaMessage("set_tempo", tempo=500_000, time=0),
            mido.MetaMessage("set_tempo", tempo=1_000_000, time=480),
        ]
    )
    notes.extend(
        [
            mido.Message("note_on", note=40, velocity=80, time=0),
            mido.Message("note_off", note=40, velocity=0, time=480),
            mido.Message("note_on", note=41, velocity=81, time=0),
            mido.Message("note_off", note=41, velocity=0, time=480),
        ]
    )

    result = extract_midi_reference(midi_bytes(mid), instrument="bass", kind=EventKind.PITCHED)

    assert [(event.midi_pitch, event.onset_seconds, event.offset_seconds) for event in result.events] == [
        (40, 0.0, 0.5),
        (41, 0.5, 1.5),
    ]


def test_channel_filter_excludes_unrelated_tracks() -> None:
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.extend(
        [
            mido.Message("note_on", note=40, velocity=80, channel=0, time=0),
            mido.Message("note_off", note=40, velocity=0, channel=0, time=240),
            mido.Message("note_on", note=50, velocity=80, channel=1, time=0),
            mido.Message("note_off", note=50, velocity=0, channel=1, time=240),
        ]
    )

    result = extract_midi_reference(
        midi_bytes(mid), instrument="bass", kind=EventKind.PITCHED, channels={1}
    )

    assert [event.midi_pitch for event in result.events] == [50]


def test_drum_reference_maps_gm_pitches_to_explicit_classes() -> None:
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.extend(
        [
            mido.Message("note_on", note=36, velocity=100, channel=9, time=0),
            mido.Message("note_off", note=36, velocity=0, channel=9, time=10),
            mido.Message("note_on", note=38, velocity=101, channel=9, time=230),
            mido.Message("note_off", note=38, velocity=0, channel=9, time=10),
            mido.Message("note_on", note=81, velocity=70, channel=9, time=0),
            mido.Message("note_off", note=81, velocity=0, channel=9, time=10),
        ]
    )

    result = extract_midi_reference(
        midi_bytes(mid),
        instrument="drums",
        kind=EventKind.PERCUSSIVE,
        channels={9},
        drum_map={36: "kick", 38: "snare"},
    )

    assert [(event.event_class, event.midi_pitch) for event in result.events] == [
        ("kick", 36),
        ("snare", 38),
    ]
    assert result.warnings == ("ignored_unmapped_drum_pitch:81",)


def test_unclosed_note_is_reported_and_not_used_as_ground_truth() -> None:
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.Message("note_on", note=40, velocity=80, time=0))

    result = extract_midi_reference(midi_bytes(mid), instrument="bass", kind=EventKind.PITCHED)

    assert result.events == ()
    assert result.warnings == ("unclosed_note:channel=0,pitch=40",)


def test_output_order_is_chronological_then_pitch() -> None:
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    first = mido.MidiTrack()
    second = mido.MidiTrack()
    mid.tracks.extend([first, second])
    first.extend(
        [
            mido.Message("note_on", note=50, velocity=80, time=0),
            mido.Message("note_off", note=50, velocity=0, time=120),
        ]
    )
    second.extend(
        [
            mido.Message("note_on", note=40, velocity=80, time=0),
            mido.Message("note_off", note=40, velocity=0, time=120),
        ]
    )

    result = extract_midi_reference(midi_bytes(mid), instrument="bass", kind=EventKind.PITCHED)

    assert [event.midi_pitch for event in result.events] == [40, 50]
