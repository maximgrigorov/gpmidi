from __future__ import annotations

from io import BytesIO

import mido
import pytest


@pytest.fixture
def midi_factory():
    def make() -> bytes:
        midi = mido.MidiFile(type=1, ticks_per_beat=480)
        conductor = mido.MidiTrack()
        conductor.append(mido.MetaMessage("track_name", name="Conductor", time=0))
        conductor.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
        midi.tracks.append(conductor)
        for name, pitch in (("Vocal", 60), ("Instrumental", 67), ("Chords", 48)):
            track = mido.MidiTrack()
            track.append(mido.MetaMessage("track_name", name=name, time=0))
            track.append(mido.Message("note_on", note=pitch, velocity=90, time=0))
            track.append(mido.Message("note_off", note=pitch, velocity=0, time=480))
            midi.tracks.append(track)
        buffer = BytesIO()
        midi.save(file=buffer)
        return buffer.getvalue()

    return make
