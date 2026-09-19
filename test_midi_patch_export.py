from __future__ import annotations

import copy
from pathlib import Path

import pytest
from mido import Message, MetaMessage, MidiFile, MidiTrack

from midi_patch_export import export_patch


def _conductor() -> MidiTrack:
    return MidiTrack([
        MetaMessage("track_name", name="Conductor", time=0),
        MetaMessage("set_tempo", tempo=800_000, time=0),
        MetaMessage("time_signature", numerator=4, denominator=4, time=0),
        MetaMessage("end_of_track", time=3840),
    ])


def _musical_track(name: str, note: int) -> MidiTrack:
    return MidiTrack([
        MetaMessage("track_name", name=name, time=0),
        Message("note_on", note=note, velocity=95, time=0),
        Message("note_off", note=note, velocity=0, time=960),
        MetaMessage("end_of_track", time=0),
    ])


def _write(path: Path, tracks: list[MidiTrack]) -> None:
    midi = MidiFile(type=1, ticks_per_beat=960)
    midi.tracks.extend(tracks)
    midi.save(path)


def _names(midi: MidiFile) -> list[str]:
    return [
        next((str(message.name) for message in track if message.type == "track_name"), "")
        for track in midi.tracks
    ]


def test_export_patch_keeps_conductor_and_only_changed_tracks(tmp_path: Path):
    baseline_path = tmp_path / "baseline.mid"
    candidate_path = tmp_path / "candidate.mid"
    output_path = tmp_path / "patch.mid"
    baseline_tracks = [_conductor(), _musical_track("Lead Guitar", 64), _musical_track("Drums", 36)]
    candidate_tracks = copy.deepcopy(baseline_tracks)
    candidate_tracks[1].insert(2, Message("pitchwheel", pitch=180, time=240))
    candidate_tracks[1][3].time -= 240
    _write(baseline_path, baseline_tracks)
    _write(candidate_path, candidate_tracks)

    manifest = export_patch(baseline_path, candidate_path, output_path)
    output = MidiFile(output_path)

    assert _names(output) == ["Conductor", "Lead Guitar"]
    assert manifest["changed_source_track_indices"] == [1]
    assert manifest["output_source_track_indices"] == [0, 1]
    assert manifest["tracks"][0]["note_identity_changed"] is False
    assert manifest["tracks"][0]["after"]["pitchwheel"] == 1


def test_export_patch_reports_multiple_changed_tracks(tmp_path: Path):
    baseline_path = tmp_path / "baseline.mid"
    candidate_path = tmp_path / "candidate.mid"
    output_path = tmp_path / "patch.mid"
    baseline_tracks = [_conductor(), _musical_track("Lead Guitar", 64), _musical_track("Drums", 36)]
    candidate_tracks = copy.deepcopy(baseline_tracks)
    candidate_tracks[1][1] = candidate_tracks[1][1].copy(velocity=91)
    candidate_tracks[2][1] = candidate_tracks[2][1].copy(velocity=101)
    _write(baseline_path, baseline_tracks)
    _write(candidate_path, candidate_tracks)

    manifest = export_patch(baseline_path, candidate_path, output_path)

    assert _names(MidiFile(output_path)) == ["Conductor", "Lead Guitar", "Drums"]
    assert manifest["changed_source_track_indices"] == [1, 2]
    assert all(not row["note_identity_changed"] for row in manifest["tracks"])


def test_require_note_identity_fails_closed_on_added_note(tmp_path: Path):
    baseline_path = tmp_path / "baseline.mid"
    candidate_path = tmp_path / "candidate.mid"
    output_path = tmp_path / "patch.mid"
    baseline_tracks = [_conductor(), _musical_track("Rhythm Guitar", 52)]
    candidate_tracks = copy.deepcopy(baseline_tracks)
    candidate_tracks[1].insert(-1, Message("note_on", note=55, velocity=90, time=0))
    candidate_tracks[1].insert(-1, Message("note_off", note=55, velocity=0, time=240))
    _write(baseline_path, baseline_tracks)
    _write(candidate_path, candidate_tracks)

    with pytest.raises(RuntimeError, match="track.*1"):
        export_patch(
            baseline_path,
            candidate_path,
            output_path,
            require_note_identity=True,
        )
    assert not output_path.exists()


def test_export_patch_rejects_track_reordering_and_noop(tmp_path: Path):
    baseline_path = tmp_path / "baseline.mid"
    candidate_path = tmp_path / "candidate.mid"
    output_path = tmp_path / "patch.mid"
    tracks = [_conductor(), _musical_track("Lead Guitar", 64)]
    _write(baseline_path, tracks)
    _write(candidate_path, copy.deepcopy(tracks))

    with pytest.raises(ValueError, match="no semantic track changes"):
        export_patch(baseline_path, candidate_path, output_path)

    reordered_path = tmp_path / "reordered.mid"
    _write(reordered_path, [tracks[0], _musical_track("Other", 64)])
    with pytest.raises(ValueError, match="name changed"):
        export_patch(baseline_path, reordered_path, output_path)
