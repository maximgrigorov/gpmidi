"""Отчёт об артикуляциях (--articulation-manifest): контекст для post-processing.

MIDI он не меняет; по каждой ноте Guitar/Bass говорит, какой артикуляцией она
сыграна и почему на неё нельзя класть автоматический finger vibrato.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

from guitarpro.models import SlideType

import gp_to_shreddage as g
from gp_import import GPBend, GPBendPoint, GPNoteEffect, NaturalHarmonic, PinchHarmonic
from test_bend_slide_timing import _beat, _note, _song

FIXTURE = Path("tests/fixtures/wizard_fixture.gp5")


def _report(song, track, **kwargs):
    report = {}
    midi_track, _stats = g.build_instrument_midi(
        song, track, "GUITAR", articulation_report=report, **kwargs)
    return midi_track, report


def _by_pitch(report):
    return {note["pitch"]: note for note in report["notes"]}


def test_report_does_not_change_the_midi():
    song, track = _song([[_beat([_note(5)]), _beat([_note(7), _note(5, string=2)])]])
    plain, _ = g.build_instrument_midi(song, track, "GUITAR")
    reported, report = _report(song, track)

    assert list(plain) == list(reported)
    assert report["summary"]["notes"] == 3


def test_report_records_articulation_position_and_polyphony():
    bend = GPBend(points=[GPBendPoint(position=0, value=0), GPBendPoint(position=12, value=4)])
    beats = [
        _beat([_note(5)]),                                              # чистая
        _beat([_note(7, effect=GPNoteEffect(bend=bend))]),               # бенд
        _beat([_note(12, string=2, effect=GPNoteEffect(harmonic=NaturalHarmonic()))]),
        _beat([_note(3, string=1), _note(5, string=3)]),                 # double-stop
    ]
    song, track = _song([beats])
    _midi, report = _report(song, track)
    notes = report["notes"]

    assert report["name"] == "Solo Guitar"
    assert report["pitch_bend_range"] == 7
    assert report["slide_mode"] == "legacy_fixed_depth"
    plain = notes[0]
    assert plain["source"] == "gp"
    assert (plain["bar"], plain["string"], plain["fret"], plain["pitch"]) == (1, 1, 5, 69)
    assert plain["articulation"] == "sustain" and plain["keyswitch"] == 12
    assert plain["polyphony"] == "mono"
    assert plain["finger_vibrato"] == {"eligible": True, "skip_reasons": []}

    assert notes[1]["bend"] is True
    assert notes[1]["finger_vibrato"]["skip_reasons"] == ["authored_bend"]
    assert notes[2]["harmonic"] == "NaturalHarmonic"
    assert "harmonic" in notes[2]["finger_vibrato"]["skip_reasons"]
    chord = [note for note in notes if note["grid_tick"] == 2880]
    assert [note["polyphony"] for note in chord] == ["double_stop", "double_stop"]
    assert all("polyphony" in note["finger_vibrato"]["skip_reasons"] for note in chord)


def test_report_flags_pinch_vibrato_legato_and_slide_resolution():
    beats = [
        _beat([_note(5, effect=GPNoteEffect(harmonic=PinchHarmonic()))]),
        _beat([_note(7, effect=GPNoteEffect(vibrato=True, vibratoType="Slight"))]),
        _beat([_note(8, effect=GPNoteEffect(hammer=True))]),
        _beat([_note(10, effect=GPNoteEffect(slides=[SlideType.legatoSlideTo]))]),
    ]
    song, track = _song([beats, [_beat([_note(12)])]])
    _midi, report = _report(song, track, slide_to_target=True)
    notes = _by_pitch(report)

    assert notes[69]["pinch_harmonic"] is True
    assert "pinch_harmonic" in notes[69]["finger_vibrato"]["skip_reasons"]
    assert notes[71]["vibrato"] == "Slight"
    assert "gp_vibrato_cc1" in notes[71]["finger_vibrato"]["skip_reasons"]
    assert notes[72]["legato_source"] is True
    slide = notes[74]["slide"]
    assert slide == {"type": "legatoSlideTo", "resolution": "pitch_bend_to_target",
                     "interval": 2, "reason": None}
    assert "slide_source" in notes[74]["finger_vibrato"]["skip_reasons"]
    assert notes[76]["slide_target"] is True
    assert notes[76]["finger_vibrato"]["eligible"] is True
    assert report["slide_mode"] == "target"


def test_report_marks_auto_cc1_vibrato_notes():
    song, track = _song([[_beat([_note(5)]), _beat([_note(5, note_type=g.NoteType.tie)])],
                         [_beat([_note(5, note_type=g.NoteType.tie)])]])
    cfg = copy.deepcopy(g.config_for_track_type("GUITAR"))
    cfg["performance_life"]["auto_sustain_vibrato"]["probability"] = 1.0
    _midi, report = _report(song, track, cfg=cfg, auto_sustain_vibrato=True)

    note = report["notes"][0]
    assert note["auto_cc1_vibrato"] is True
    assert note["finger_vibrato"]["skip_reasons"] == ["auto_cc1_vibrato"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cli_writes_sidecar_without_touching_midi(tmp_path):
    plain_dir, manifest_dir = tmp_path / "plain", tmp_path / "manifest"
    for folder in (plain_dir, manifest_dir):
        folder.mkdir()
        shutil.copy(FIXTURE, folder / "song.gp5")
    g.main(["gp_to_shreddage.py", str(plain_dir / "song.gp5"), "--no-verify"])
    g.main(["gp_to_shreddage.py", str(manifest_dir / "song.gp5"), "--no-verify",
            "--articulation-manifest"])

    plain_out, manifest_out = plain_dir / "song_midi", manifest_dir / "song_midi"
    midis = sorted(path.name for path in plain_out.glob("*.mid"))
    assert midis == sorted(path.name for path in manifest_out.glob("*.mid"))
    for name in midis:
        assert _sha(plain_out / name) == _sha(manifest_out / name), name

    payload = json.loads((manifest_out / "song_articulations.json").read_text())
    assert payload["kind"] == "gpmidi_articulation_manifest"
    assert payload["schema_version"] == g.ARTICULATION_MANIFEST_SCHEMA
    assert payload["ticks_per_beat"] == g.TICKS_PER_BEAT
    assert payload["combined_file"] == "song_ALL.mid"
    assert payload["tracks"], "в фикстуре есть гитары/бас"
    for entry in payload["tracks"]:
        assert entry["track_type"] in ("GUITAR", "BASS")
        assert (manifest_out / entry["midi_file"]).exists()
        assert entry["summary"]["notes"] == len(entry["notes"])
