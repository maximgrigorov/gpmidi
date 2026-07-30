"""Opt-in разворачивание GP8 playback-offset пар в скрытые 32-е."""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET

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
    _extract_gpif_note_extras_root,
)


def _note(string: int, fret: int, offset: int, duration: float) -> GPNote:
    note = GPNote(
        string=string,
        value=fret,
        velocity=95,
        effect=GPNoteEffect(),
        type=NoteType.normal,
        realValue=0,
    )
    # Желаемый публичный sidecar-контракт GP8; до реализации поля отсутствуют.
    note.playedOffset = offset
    note.playedDuration = duration
    return note


def _song(track_name: str, notes: list[GPNote]) -> tuple[GPSong, GPTrack]:
    header = GPMeasureHeader(GPTimeSignature(4, GPDenominator(4)))
    beat = GPBeat(
        start=960,
        duration=GPDuration(value=16, tuplet=GPTuplet(), time=240),
        notes=notes,
        effect=GPBeatEffect(),
        status=GPBeatStatus("normal"),
    )
    track = GPTrack(
        name=track_name,
        strings=[GPString(1, 64), GPString(2, 59)],
        channel=GPChannel(30),
        measures=[GPMeasure(960, header, [GPVoice([beat])])],
    )
    song = GPSong("fixture", "", "", 120, [track], [header])
    return song, track


def _note_intervals(midi_track) -> dict[int, tuple[int, int]]:
    tick = 0
    starts: dict[int, int] = {}
    intervals: dict[int, tuple[int, int]] = {}
    for msg in midi_track:
        tick += msg.time
        if not hasattr(msg, "note") or msg.note < 30:
            continue
        if msg.type == "note_on" and msg.velocity > 0:
            starts[msg.note] = tick
        elif msg.type == "note_off" and msg.note in starts:
            intervals[msg.note] = (starts.pop(msg.note), tick)
    return intervals


def test_gpif_sidecar_preserves_per_note_played_offset_and_duration():
    root = ET.fromstring("""
    <GPIF>
      <MasterBars><MasterBar id="0"><Bars>0</Bars></MasterBar></MasterBars>
      <Bars><Bar id="0"><Voices>0 -1 -1 -1</Voices></Bar></Bars>
      <Voices><Voice id="0"><Beats>0</Beats></Voice></Voices>
      <Beats><Beat id="0"><Notes>10 11</Notes></Beat></Beats>
      <Notes>
        <Note id="10"><Offset>-60</Offset><Duration>0.508333</Duration></Note>
        <Note id="11"><Offset>-6</Offset><Duration>1.01667</Duration></Note>
      </Notes>
    </GPIF>
    """)

    extras = _extract_gpif_note_extras_root(root)

    assert extras[(0, 0, 0, 0)]["played_offset"] == -60
    assert extras[(0, 0, 0, 0)]["played_duration"] == 0.508333
    assert extras[(0, 0, 0, 1)]["played_offset"] == -6
    assert extras[(0, 0, 0, 1)]["played_duration"] == 1.01667


def test_hidden_32nds_are_opt_in_and_preserve_gp_attack_and_duration_relationships():
    # Точный паттерн со скриншота: две ноты одного 16th Beat, разница offset
    # 54 GPIF-тика ~= 1/32; первая длится половину beat, вторая — полный beat.
    later = _note(1, 10, offset=-6, duration=1.01667)   # pitch 74
    earlier = _note(2, 13, offset=-60, duration=0.508333)  # pitch 72
    song, track = _song("Solo Guitar", [later, earlier])

    legacy, legacy_stats = g.build_instrument_midi(song, track, "GUITAR")
    expanded, stats = g.build_instrument_midi(
        song, track, "GUITAR", expand_gp_hidden_32nds=True,
    )

    assert _note_intervals(legacy)[72][0] == _note_intervals(legacy)[74][0] == 0
    # GPIF offsets имеют 480 PPQ, выход — 960 PPQ: 54 * 2 = 108 тиков.
    assert _note_intervals(expanded)[72] == (0, 122)
    assert _note_intervals(expanded)[74] == (108, 352)
    assert legacy_stats.get("hidden_32nd_beats", 0) == 0
    assert stats["hidden_32nd_beats"] == 1
    assert stats["hidden_32nd_notes"] == 2


def test_real_chord_with_nearly_equal_offsets_stays_polyphonic():
    song, track = _song("Solo Guitar", [
        _note(1, 10, offset=-6, duration=1.0),
        _note(2, 13, offset=-4, duration=1.0),
    ])

    midi, stats = g.build_instrument_midi(
        song, track, "GUITAR", expand_gp_hidden_32nds=True,
    )

    intervals = _note_intervals(midi)
    assert intervals[72][0] == intervals[74][0] == 0
    assert stats["hidden_32nd_beats"] == 0


def test_hidden_32nds_do_not_change_non_solo_guitar():
    song, track = _song("Rhytm Guitar", [
        _note(1, 10, offset=-6, duration=1.01667),
        _note(2, 13, offset=-60, duration=0.508333),
    ])

    midi, stats = g.build_instrument_midi(
        song, track, "GUITAR", expand_gp_hidden_32nds=True,
    )

    intervals = _note_intervals(midi)
    assert intervals[72][0] == intervals[74][0] == 0
    assert stats["hidden_32nd_beats"] == 0


def test_cli_parses_hidden_32nds_as_independent_opt_in():
    options = g.parse_cli_options([
        "gp_to_shreddage.py", "song.gp", "--expand-gp-hidden-32nds",
    ])

    assert options["expand_gp_hidden_32nds"] is True


def test_web_upload_forwards_hidden_32nds_opt_in(monkeypatch):
    import app as web

    captured = {}

    def fake_create_job(uploaded_file, **kwargs):
        captured.update(kwargs)
        return "job123"

    monkeypatch.setattr(web, "create_job", fake_create_job)
    web.app.config.update(TESTING=True)
    response = web.app.test_client().post(
        "/upload",
        data={
            "file": (io.BytesIO(b"fixture"), "song.gp"),
            "expand_gp_hidden_32nds": "on",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert captured["expand_gp_hidden_32nds"] is True


def test_index_exposes_hidden_32nds_checkbox(monkeypatch):
    import app as web

    monkeypatch.setattr(web, "load_manifest", lambda: {"jobs": [], "current_job_id": None})
    web.app.config.update(TESTING=True)
    response = web.app.test_client().get("/")

    assert response.status_code == 200
    assert b'name="expand_gp_hidden_32nds"' in response.data
