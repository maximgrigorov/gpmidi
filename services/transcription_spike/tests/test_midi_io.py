from __future__ import annotations

from pathlib import Path

import mido
import pytest
from transcription_spike.midi_io import read_pitched_midi_events


def write_tempo_fixture(path: Path) -> None:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    track.append(mido.Message("note_on", note=60, velocity=90, channel=0, time=0))
    track.append(mido.Message("note_off", note=60, velocity=0, channel=0, time=480))
    track.append(mido.MetaMessage("set_tempo", tempo=1_000_000, time=0))
    track.append(mido.Message("note_on", note=62, velocity=80, channel=0, time=0))
    track.append(mido.Message("note_off", note=62, velocity=0, channel=0, time=480))
    midi.save(path)


def test_reads_notes_in_absolute_seconds_across_tempo_changes(tmp_path: Path) -> None:
    path = tmp_path / "source.mid"
    write_tempo_fixture(path)

    events = read_pitched_midi_events(path, instrument="bass")

    assert [(event.midi_pitch, event.velocity) for event in events] == [(60, 90), (62, 80)]
    assert events[0].onset_seconds == pytest.approx(0.0)
    assert events[0].offset_seconds == pytest.approx(0.5)
    assert events[1].onset_seconds == pytest.approx(0.5)
    assert events[1].offset_seconds == pytest.approx(1.5)


def test_offsets_excerpt_to_source_timeline_then_filters_by_onset(tmp_path: Path) -> None:
    path = tmp_path / "prediction.mid"
    write_tempo_fixture(path)

    events = read_pitched_midi_events(
        path,
        instrument="bass",
        timeline_offset_seconds=195.0,
        window_start_seconds=195.4,
        window_end_seconds=196.0,
    )

    assert len(events) == 1
    assert events[0].midi_pitch == 62
    assert events[0].onset_seconds == pytest.approx(195.5)
    assert events[0].offset_seconds == pytest.approx(196.5)


def test_rejects_invalid_window(tmp_path: Path) -> None:
    path = tmp_path / "source.mid"
    write_tempo_fixture(path)

    with pytest.raises(ValueError, match="window"):
        read_pitched_midi_events(
            path,
            instrument="bass",
            window_start_seconds=3.0,
            window_end_seconds=2.0,
        )
