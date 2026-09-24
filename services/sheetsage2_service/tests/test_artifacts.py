from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import mido
from sheetsage2_service.artifacts import package_transcription


def _midi_bytes() -> bytes:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    tempo = mido.MidiTrack()
    tempo.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    tempo.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0))
    midi.tracks.append(tempo)
    for name, pitch in (("Vocal", 60), ("Instrumental", 67), ("Chords", 48)):
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name=name, time=0))
        track.append(mido.Message("note_on", note=pitch, velocity=80, time=0))
        track.append(mido.Message("note_off", note=pitch, velocity=0, time=480))
        midi.tracks.append(track)
    stream = BytesIO()
    midi.save(file=stream)
    return stream.getvalue()


def test_package_contains_one_importable_all_tracks_midi_and_reports(tmp_path: Path):
    model_output = tmp_path / "model"
    model_output.mkdir()
    (model_output / "transcription.mid").write_bytes(_midi_bytes())
    (model_output / "melody_vocal.mid").write_bytes(_midi_bytes())
    (model_output / "events.json").write_text(
        json.dumps([{"time": 0.0, "melody": "C4"}]), encoding="utf-8"
    )
    (model_output / "score.abc").write_text("X:1\nT:Song\nK:C\nC|", encoding="utf-8")
    (model_output / "chord.lab").write_text(
        "0.0\t1.0\tC:maj\n1.0\t2.0\tG:maj\n", encoding="utf-8"
    )
    (model_output / "key.lab").write_text("0.0\t2.0\tC:major\n", encoding="utf-8")
    (model_output / "structure.lab").write_text(
        "0.0\t1.0\tintro\n1.0\t2.0\tverse\n", encoding="utf-8"
    )
    (model_output / "beat.lab").write_text(
        "0.0\t1\t4\t4\n0.5\t2\t4\t4\n1.0\t3\t4\t4\n1.5\t4\t4\t4\n",
        encoding="utf-8",
    )
    (model_output / "downbeat.lab").write_text("0.0\n", encoding="utf-8")

    result = package_transcription(
        model_output=model_output,
        destination=tmp_path / "result",
        original_filename="my song.wav",
        model_revision="488abe28ef4db3dbb056da19cb49d80f4b14bc61",
        input_sha256="a" * 64,
        elapsed_seconds=12.5,
    )

    assert result.archive_path.name == "my_song_SheetSage2.zip"
    assert result.all_tracks_path.name == "my_song_ALL_TRACKS.mid"
    parsed = mido.MidiFile(result.all_tracks_path)
    assert parsed.type == 1
    assert [track.name for track in parsed.tracks] == [
        "Conductor", "Vocal", "Instrumental", "Chords"
    ]
    report_html = result.report_html_path.read_text(encoding="utf-8")
    assert report_html.startswith("<!doctype html>")
    assert "Оценочный BPM: 120.0" in report_html
    assert "C:maj, G:maj" in report_html
    assert "intro, verse" in report_html
    report = json.loads(result.report_json_path.read_text(encoding="utf-8"))
    assert report["track_count"] == 3
    assert report["note_count"] == 3
    assert report["input_sha256"] == "a" * 64
    assert report["musical_summary"] == {
        "estimated_bpm": 120.0,
        "beat_count": 4,
        "downbeat_count": 1,
        "chords": ["C:maj", "G:maj"],
        "keys": ["C:major"],
        "sections": ["intro", "verse"],
    }

    with zipfile.ZipFile(result.archive_path) as archive:
        names = set(archive.namelist())
        assert "my_song_ALL_TRACKS.mid" in names
        assert "report.html" in names
        assert "report.json" in names
        assert "manifest.json" in names
        assert "raw/events.json" in names
        assert "raw/score.abc" in names


def test_package_rejects_midi_without_note_tracks(tmp_path: Path):
    model_output = tmp_path / "model"
    model_output.mkdir()
    empty = mido.MidiFile(type=1)
    empty.save(model_output / "transcription.mid")

    import pytest

    with pytest.raises(ValueError, match="note-bearing"):
        package_transcription(
            model_output=model_output,
            destination=tmp_path / "result",
            original_filename="empty.wav",
            model_revision="revision",
            input_sha256="b" * 64,
            elapsed_seconds=1.0,
        )
