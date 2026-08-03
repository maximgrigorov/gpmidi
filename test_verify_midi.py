from __future__ import annotations

from mido import Message, MetaMessage, MidiFile, MidiTrack

from verify_midi import smoke_check


def _save(path, messages):
    midi = MidiFile(type=0, ticks_per_beat=960)
    track = MidiTrack()
    track.append(MetaMessage("track_name", name="Bass", time=0))
    track.extend(messages)
    track.append(MetaMessage("end_of_track", time=0))
    midi.tracks.append(track)
    midi.save(path)


def _codes(path):
    return [code for severity, code, _message in smoke_check(path, "BASS") if severity == "ERROR"]


def test_initial_keyswitch_at_tick_zero_is_valid_when_ordered_before_first_note(tmp_path):
    path = tmp_path / "initial-before.mid"
    _save(path, [
        Message("note_on", note=0, velocity=100, time=0),
        Message("note_on", note=43, velocity=95, time=0),
        Message("note_off", note=0, velocity=0, time=6),
        Message("note_off", note=43, velocity=0, time=954),
    ])

    assert "KS_LEAD" not in _codes(path)


def test_same_tick_articulation_change_after_music_is_still_rejected(tmp_path):
    path = tmp_path / "transition-after.mid"
    _save(path, [
        Message("note_on", note=0, velocity=100, time=0),
        Message("note_off", note=0, velocity=0, time=6),
        Message("note_on", note=43, velocity=95, time=954),
        Message("note_off", note=43, velocity=0, time=960),
        Message("note_on", note=45, velocity=95, time=0),
        Message("note_on", note=1, velocity=100, time=0),
        Message("note_off", note=1, velocity=0, time=6),
        Message("note_off", note=45, velocity=0, time=954),
    ])

    assert "KS_LEAD" in _codes(path)
