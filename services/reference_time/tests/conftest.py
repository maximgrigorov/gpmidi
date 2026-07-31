"""Test fixtures for reference-time analysis.

All fixtures are tiny and generated — no copyrighted content.
"""

from __future__ import annotations

import io
import struct
import sys
import os

# Ensure the service package is importable when tests are collected from root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mido
import pytest


@pytest.fixture
def midi_type0_constant_tempo() -> bytes:
    """MIDI type 0, constant 120 BPM, 4/4, 8 measures of quarter notes."""
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)

    track.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))

    for measure in range(8):
        for beat in range(4):
            track.append(mido.Message("note_on", note=60, velocity=80, time=0))
            track.append(mido.Message("note_off", note=60, velocity=0, time=480))

    track.append(mido.MetaMessage("end_of_track", time=0))

    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


@pytest.fixture
def midi_type1_tempo_changes() -> bytes:
    """MIDI type 1 with tempo changes: 120 BPM for 4 measures, 140 BPM for 4."""
    mid = mido.MidiFile(type=1, ticks_per_beat=480)

    tempo_track = mido.MidiTrack()
    mid.tracks.append(tempo_track)
    tempo_track.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    tempo_track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    tempo_track.append(mido.MetaMessage("set_tempo", tempo=428571, time=7680))
    tempo_track.append(mido.MetaMessage("end_of_track", time=7680))

    note_track = mido.MidiTrack()
    mid.tracks.append(note_track)
    for _ in range(32):
        note_track.append(mido.Message("note_on", note=60, velocity=80, time=0))
        note_track.append(mido.Message("note_off", note=60, velocity=0, time=480))
    note_track.append(mido.MetaMessage("end_of_track", time=0))

    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


@pytest.fixture
def midi_3_4_to_4_4() -> bytes:
    """MIDI with 3/4 for 4 measures, then 4/4 for 4 measures."""
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)

    track.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    track.append(mido.MetaMessage("time_signature", numerator=3, denominator=4, time=0))

    for _ in range(12):
        track.append(mido.Message("note_on", note=60, velocity=80, time=0))
        track.append(mido.Message("note_off", note=60, velocity=0, time=480))

    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))

    for _ in range(16):
        track.append(mido.Message("note_on", note=60, velocity=80, time=0))
        track.append(mido.Message("note_off", note=60, velocity=0, time=480))

    track.append(mido.MetaMessage("end_of_track", time=0))

    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


@pytest.fixture
def midi_with_preroll() -> bytes:
    """MIDI with a pre-roll (first note at tick 960 = 2 beats)."""
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)

    track.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))

    track.append(mido.Message("note_on", note=60, velocity=80, time=960))
    track.append(mido.Message("note_off", note=60, velocity=0, time=480))

    for _ in range(7):
        track.append(mido.Message("note_on", note=60, velocity=80, time=0))
        track.append(mido.Message("note_off", note=60, velocity=0, time=480))

    track.append(mido.MetaMessage("end_of_track", time=0))

    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


@pytest.fixture
def midi_conflicting_tempo() -> bytes:
    """MIDI with 100 BPM constant tempo (conflicts with 120 BPM fixtures)."""
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)

    track.append(mido.MetaMessage("set_tempo", tempo=600000, time=0))
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))

    for _ in range(32):
        track.append(mido.Message("note_on", note=60, velocity=80, time=0))
        track.append(mido.Message("note_off", note=60, velocity=0, time=480))

    track.append(mido.MetaMessage("end_of_track", time=0))

    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


@pytest.fixture
def midi_empty() -> bytes:
    """MIDI with no notes."""
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    track.append(mido.MetaMessage("end_of_track", time=480))

    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


@pytest.fixture
def midi_malformed() -> bytes:
    """Malformed MIDI bytes."""
    return b"This is not a MIDI file"


@pytest.fixture
def tiny_wav() -> bytes:
    """Tiny synthetic WAV with known click at 0.5s and 1.0s."""
    sr = 44100
    duration = 2.0
    n_samples = int(sr * duration)
    bits = 16
    channels = 1

    samples = bytearray(n_samples * 2)

    for click_time in [0.5, 1.0]:
        idx = int(click_time * sr) * 2
        if idx + 1 < len(samples):
            struct.pack_into("<h", samples, idx, 32000)
            if idx + 3 < len(samples):
                struct.pack_into("<h", samples, idx + 2, -32000)

    data = bytes(samples)
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data), b"WAVE",
        b"fmt ", 16, 1, channels, sr,
        sr * channels * bits // 8, channels * bits // 8, bits,
        b"data", len(data),
    )
    return hdr + data


@pytest.fixture
def structure_json_valid() -> str:
    """Valid structure JSON with timestamp anchors."""
    import json
    return json.dumps({
        "sections": [
            {"name": "Intro", "source_measure": 0, "gp_measure": 0},
            {"name": "Verse 1", "source_measure": 8, "gp_measure": 8},
        ]
    })


@pytest.fixture
def structure_json_conflicting() -> str:
    """Structure JSON with conflicting anchors."""
    import json
    return json.dumps({
        "sections": [
            {"name": "Intro", "source_measure": 0, "gp_measure": 0},
            {"name": "Conflict", "source_measure": 4, "gp_measure": 2},
            {"name": "Conflict2", "source_measure": 2, "gp_measure": 4},
        ]
    })


@pytest.fixture
def xss_marker_text() -> str:
    """User marker containing HTML/script for XSS testing."""
    return '<script>alert("xss")</script><img onerror="alert(1)" src=x>'


@pytest.fixture
def tmp_db_path(tmp_path):
    """Temporary database path."""
    return str(tmp_path / "test.sqlite3")
