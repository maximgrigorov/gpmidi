"""Opt-in preservation of GP8 per-note playback attack offsets."""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from guitarpro.models import NoteType

import gp_to_shreddage as g
from gp_import import (
    GPBeat,
    GPBeatEffect,
    GPBeatStatus,
    GPChannel,
    GPDenominator,
    GPDuration,
    GPMeasure,
    GPMeasureHeader,
    GPNote,
    GPNoteEffect,
    GPSong,
    GPString,
    GPTimeSignature,
    GPTrack,
    GPTuplet,
    GPVoice,
    parse_song,
)

KS_NOTES = {
    12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23,
}
SPRING_MELODY = Path.home() / "Spring_Melody_v2.gp"
BAR_TICKS = 4 * g.TICKS_PER_BEAT


def _note(fret: int, *, offset: int = 0) -> GPNote:
    return GPNote(
        string=1,
        value=fret,
        velocity=95,
        effect=GPNoteEffect(),
        type=NoteType.normal,
        realValue=0,
        playedOffset=offset,
        playedDuration=1.0,
    )


def _two_beat_song(offset: int = -25) -> tuple[GPSong, GPTrack]:
    header = GPMeasureHeader(GPTimeSignature(4, GPDenominator(4)))
    rest = GPBeat(
        start=960,
        duration=GPDuration(value=16, tuplet=GPTuplet(), time=240),
        notes=[],
        effect=GPBeatEffect(),
        status=GPBeatStatus("rest"),
    )
    played = GPBeat(
        start=1200,
        duration=GPDuration(value=16, tuplet=GPTuplet(), time=240),
        notes=[_note(10, offset=offset)],
        effect=GPBeatEffect(),
        status=GPBeatStatus("normal"),
    )
    track = GPTrack(
        name="Solo Guitar",
        strings=[GPString(1, 64)],
        channel=GPChannel(30),
        measures=[GPMeasure(960, header, [GPVoice([rest, played])])],
    )
    return GPSong("fixture", "", "", 120, [track], [header]), track


def _note_ons(midi_track) -> list[tuple[int, int]]:
    tick = 0
    result = []
    for msg in midi_track:
        tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and msg.note not in KS_NOTES:
            result.append((tick, msg.note))
    return result


def test_played_offsets_are_opt_in_and_convert_gpif_480_ppq_to_midi_960_ppq():
    song, track = _two_beat_song(offset=-25)

    legacy, legacy_stats = g.build_instrument_midi(song, track, "GUITAR")
    preserved, stats = g.build_instrument_midi(
        song, track, "GUITAR", preserve_gp_played_offsets=True,
    )

    assert _note_ons(legacy) == [(240, 74)]
    assert _note_ons(preserved) == [(190, 74)]
    assert legacy_stats.get("played_offset_notes", 0) == 0
    assert stats["played_offset_notes"] == 1


def test_other_tracks_preserve_played_offsets_without_keyswitches():
    song, track = _two_beat_song(offset=-25)
    track.name = "Keyboard"

    legacy, legacy_stats = g.build_other_midi(song, track)
    preserved, stats = g.build_other_midi(
        song, track, preserve_gp_played_offsets=True,
    )

    assert _note_ons(legacy) == [(240, 74)]
    assert _note_ons(preserved) == [(190, 74)]
    assert legacy_stats.get("played_offset_notes", 0) == 0
    assert stats["played_offset_notes"] == 1
    assert not any(
        msg.type == "note_on" and msg.velocity > 0 and msg.note in KS_NOTES
        for msg in preserved
    )


def test_played_offsets_are_not_applied_twice_with_hidden_32nds_option():
    song, track = _two_beat_song(offset=-60)
    track.measures[0].voices[0].beats[1].notes.append(_note(8, offset=-6))

    preserved, _ = g.build_instrument_midi(
        song, track, "GUITAR", preserve_gp_played_offsets=True,
    )
    combined, stats = g.build_instrument_midi(
        song,
        track,
        "GUITAR",
        preserve_gp_played_offsets=True,
        expand_gp_hidden_32nds=True,
    )

    assert _note_ons(combined) == _note_ons(preserved)
    assert stats["hidden_32nd_beats"] == 0


def test_cli_parses_played_offsets_as_independent_opt_in():
    options = g.parse_cli_options([
        "gp_to_shreddage.py", "song.gp", "--preserve-gp-played-offsets",
    ])

    assert options["preserve_gp_played_offsets"] is True
    assert options["humanize"] is False
    assert options["expand_gp_hidden_32nds"] is False


def test_processing_step_forwards_played_offsets_per_track(monkeypatch):
    import app as web

    captured = {}
    job = {
        "id": "job123", "stage": "parsed", "original_name": "song.gp",
        "stored_name": "song.gp",
        "detected_tracks": [{
            "index": 1, "track_type": "GUITAR",
            "available_effects": ["preserve_gp_played_offsets"],
        }],
    }
    monkeypatch.setattr(
        web, "load_manifest",
        lambda: {"jobs": [job], "current_job_id": "job123"},
    )

    def fake_create_job(uploaded_file, **kwargs):
        captured.update(kwargs)
        return "job123"

    monkeypatch.setattr(web, "create_job", fake_create_job)
    web.app.config.update(TESTING=True)
    response = web.app.test_client().post(
        "/jobs/job123/process",
        data={
            "track_indices": "1",
            "track_1_preserve_gp_played_offsets": "on",
        },
    )

    assert response.status_code == 302
    assert captured["track_options"][1]["preserve_gp_played_offsets"] is True


def test_track_selection_template_exposes_gp_played_offsets_checkbox():
    source = Path("templates/index.html").read_text(encoding="utf-8")

    assert 'name="track_{{ track.index }}_preserve_gp_played_offsets"' in source
    assert "preserve GP played offsets" in source


def test_spring_melody_measures_66_67_preserve_relative_gp_attack_offsets():
    if not SPRING_MELODY.exists():
        pytest.skip(f"образец не найден: {SPRING_MELODY}")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        song = parse_song(SPRING_MELODY)

    solo = next(track for track in song.tracks if track.name == "Guitar (Solo)")
    bass = next(track for track in song.tracks if track.name == "Acoustic Bass")
    drums = next(track for track in song.tracks if track.name == "Drums")
    solo_midi, _ = g.build_instrument_midi(
        song, solo, "GUITAR", preserve_gp_played_offsets=True,
    )
    bass_midi, _ = g.build_instrument_midi(
        song, bass, "BASS", preserve_gp_played_offsets=True,
    )
    drums_midi, _ = g.build_drum_midi(song, drums)

    solo_ons = set(_note_ons(solo_midi))
    bass_ons = set(_note_ons(bass_midi))
    drum_ons = set(_note_ons(drums_midi))
    bar66 = 65 * BAR_TICKS
    bar67 = 66 * BAR_TICKS

    # GPIF Offset uses 480 PPQ; the exported files use 960 PPQ.
    assert (bar66 - 50, 70) in solo_ons       # Solo m66 first note: Offset -25
    assert (bar66 - 12, 41) in bass_ons       # Bass m66 first note: Offset -6
    assert (bar66, 36) in drum_ons            # Drums have no playback offset
    assert (bar67 + 240 - 24, 84) in solo_ons # Solo m67 first attack: Offset -12
