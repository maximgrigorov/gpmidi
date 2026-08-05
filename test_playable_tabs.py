from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import mido
import pytest
from guitarpro.models import NoteType

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
)


def _note(string: int, fret: int, kind=NoteType.normal, velocity: int = 95) -> GPNote:
    opens = {1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40}
    return GPNote(
        string=string,
        value=fret,
        velocity=velocity,
        effect=GPNoteEffect(),
        type=kind,
        realValue=opens[string] + fret,
    )


def _song_with_tie() -> tuple[GPSong, GPTrack]:
    header = GPMeasureHeader(GPTimeSignature(4, GPDenominator(4)))
    duration = GPDuration(4, time=960, tuplet=GPTuplet())
    beats = [
        GPBeat(0, duration, [_note(1, 3)], GPBeatEffect(), GPBeatStatus("normal")),
        GPBeat(960, duration, [_note(1, 3, NoteType.tie)], GPBeatEffect(), GPBeatStatus("normal")),
    ]
    track = GPTrack(
        "Solo Guitar",
        [GPString(number=i, value=v) for i, v in enumerate([64, 59, 55, 50, 45, 40], start=1)],
        GPChannel(25),
        [GPMeasure(0, header, [GPVoice(beats)])],
    )
    return GPSong("Test", "", "", 120, [track], [header]), track


def test_tie_extends_one_score_attack():
    from playable_tabs import build_score_events

    _song, track = _song_with_tie()
    events = build_score_events(track)
    assert [(event.start_tick, event.dur_ticks, event.pitch) for event in events] == [(0, 1920, 67)]
    assert len(events[0].source_notes) == 2


def test_score_midi_contains_only_score_messages(tmp_path: Path):
    from playable_tabs import build_score_events, write_score_midi

    song, track = _song_with_tie()
    path = write_score_midi(build_score_events(track), tmp_path / "score.mid", song, track_name=track.name)
    midi = mido.MidiFile(path)
    allowed = {"note_on", "note_off", "set_tempo", "time_signature", "track_name", "end_of_track"}
    assert {message.type for message in midi.tracks[0]} <= allowed
    assert sum(message.type == "note_on" and message.velocity > 0 for message in midi.tracks[0]) == 1


def test_tuttut_headless_smoke(tmp_path: Path):
    """The vendored runtime dependencies must support tuttut without its GUI."""
    from playable_tabs import build_score_events, run_tuttut, write_score_midi

    song, track = _song_with_tie()
    score = write_score_midi(build_score_events(track), tmp_path / "score.mid", song, track_name=track.name)
    output = run_tuttut(score, tmp_path / "tuttut.txt", "GUITAR")
    assert output.read_text(encoding="utf-8").startswith("Такты ")


def test_tuttut_expands_zero_width_measures_and_labels_measure_range():
    from playable_tabs import _wrap_tuttut_ascii

    raw = "\n".join([
        "E ||0---|||2--|",
        "B ||----|||---|",
        "G ||----|||---|",
        "D ||----|||---|",
        "A ||----|||---|",
        "E ||----|||---|",
    ])
    wrapped = _wrap_tuttut_ascii(raw, total_measures=6)

    assert wrapped.startswith("Такты 1–6\n")
    assert "E ||0---|----------------|----------------|2--|----------------|----------------|" in wrapped


def test_tuttut_wraps_full_song_without_losing_notes_or_clipping_pdf(tmp_path: Path):
    import re

    from playable_tabs import ScoreNote, run_tuttut, write_score_midi
    from tab_print import ascii_to_pdf

    song, track = _song_with_tie()
    events = [ScoreNote(i * 240, 240, 64 + i % 8, 95) for i in range(120)]
    score = write_score_midi(events, tmp_path / "score.mid", song, track_name=track.name)
    output = run_tuttut(score, tmp_path / "tuttut.txt", "GUITAR")
    text = output.read_text(encoding="utf-8")

    assert len(text.split("\n\n")) > 1
    assert max(map(len, text.splitlines())) <= 100
    string_lines = [line for line in text.splitlines() if re.match(r"^[EBGDA] ", line)]
    assert sum(len(re.findall(r"(?<!\d)\d+(?!\d)", line[3:])) for line in string_lines) == len(events)
    pdf = ascii_to_pdf(output, track.name, preset="solo", version=1)
    assert pdf.warning is None


@pytest.mark.parametrize(("track_type", "pitches", "strings", "max_fret"), [
    ("GUITAR", range(64, 72), 6, 22),
    ("BASS", range(40, 48), 4, 20),
])
def test_gtrsnipe_mapper_respects_instrument(track_type, pitches, strings, max_fret):
    from playable_tabs import ScoreNote, map_with_gtrsnipe, resolve_params

    events = [ScoreNote(i * 240, 240, pitch, 90) for i, pitch in enumerate(pitches)]
    preset = "bass" if track_type == "BASS" else "rhythm"
    params = resolve_params(preset, {}, track_type=track_type)
    mapped, text = map_with_gtrsnipe(events, track_type, "Phrase", params, tempo=91.0, time_signature="3/4")
    assert "// Tempo: 91 BPM" in text
    assert "// Time: 3/4" in text
    assert len(mapped) == len(events)
    assert all(0 <= event.string < strings for event in mapped)
    assert all(0 <= event.fret <= max_fret for event in mapped)


def test_pipeline_keeps_gtrsnipe_when_tuttut_fails(tmp_path: Path, monkeypatch):
    import playable_tabs

    song, track = _song_with_tie()
    monkeypatch.setattr(playable_tabs, "run_tuttut", Mock(side_effect=RuntimeError("boom")))
    result = playable_tabs.generate_track(song, track, tmp_path, preset="auto")
    assert result.status["gtrsnipe"] == "ok"
    assert result.status["tuttut"].startswith("failed:")
    assert result.note_positions[0] is not None
    assert result.note_positions[0] == result.note_positions[1]
    assert (tmp_path / "Solo Guitar.playable.gtrsnipe.txt").exists()
    assert (tmp_path / "Solo Guitar.score.mid").exists()


def test_refinger_pitch_formula_is_invariant():
    from playable_tabs import fret_for_pitch

    for opens, max_fret in [([64, 59, 55, 50, 45, 40], 22), ([43, 38, 33, 28], 20)]:
        for string, open_pitch in enumerate(opens):
            for fret in (0, 1, max_fret):
                pitch = open_pitch + fret
                assert opens[string] + fret_for_pitch(pitch, string, opens, max_fret) == pitch
    with pytest.raises(ValueError):
        fret_for_pitch(20, 0, [64, 59, 55, 50, 45, 40], 22)


def test_regen_param_validation_is_whitelisted():
    from playable_tabs import validate_params

    params = validate_params({"preset": "solo", "sweet_spot_low": "5", "sweet_spot_high": "14", "prefer_open": "on"})
    assert params["sweet_spot_low"] == 5
    assert params["sweet_spot_high"] == 14
    assert params["prefer_open"] is True
    for bad in (
        {"preset": "solo", "sweet_spot_low": "8", "sweet_spot_high": "8"},
        {"preset": "solo", "movement_penalty": "-1"},
        {"preset": "solo", "evil": "1"},
    ):
        with pytest.raises(ValueError):
            validate_params(bad)


def test_ascii_pdf_is_a4_and_handles_long_lines(tmp_path: Path):
    from pypdf import PdfReader

    from tab_print import ascii_to_pdf

    system = "\n".join(["e|" + "-" * 198] + [f"S{i}|" + "-" * 30 for i in range(5)])
    source = tmp_path / "tab.txt"
    source.write_text("\n\n".join([system] * 3), encoding="utf-8")
    result = ascii_to_pdf(source, "Long tab", preset="solo", version=1)
    assert result.path.read_bytes().startswith(b"%PDF")
    page = PdfReader(str(result.path)).pages[0]
    assert float(page.mediabox.width) == pytest.approx(595.28, abs=1.0)
    assert float(page.mediabox.height) == pytest.approx(841.89, abs=1.0)



def test_gpif_reused_note_definition_is_cloned_for_different_fingering():
    import xml.etree.ElementTree as ET

    from playable_tabs import iter_gpif_note_elements, patch_gpif_track_positions

    root = ET.fromstring("""
    <GPIF><Tracks><Track><Name>G</Name><Staves><Staff/></Staves></Track></Tracks>
    <MasterBars><MasterBar><Bars>b1</Bars></MasterBar><MasterBar><Bars>b2</Bars></MasterBar></MasterBars>
    <Bars><Bar id="b1"><Voices>v1</Voices></Bar><Bar id="b2"><Voices>v2</Voices></Bar></Bars>
    <Voices><Voice id="v1"><Beats>x1</Beats></Voice><Voice id="v2"><Beats>x2</Beats></Voice></Voices>
    <Beats><Beat id="x1"><Notes>1</Notes></Beat><Beat id="x2"><Notes>1</Notes></Beat></Beats>
    <Notes><Note id="1"><Properties><Property name="String"><String>0</String></Property><Property name="Fret"><Fret>0</Fret></Property></Properties></Note></Notes>
    </GPIF>""")
    patch_gpif_track_positions(root, 0, [(0, 3), (1, 7)], num_strings=6)
    refs = list(iter_gpif_note_elements(root, 0))
    assert refs[0].get("id") != refs[1].get("id")
    values = []
    for note in refs:
        props = {item.get("name"): next(iter(item)).text for item in note.findall("Properties/Property")}
        values.append((props["String"], props["Fret"]))
    assert values == [("5", "3"), ("4", "7")]


def _web_client_for_job(tmp_path: Path, monkeypatch, job: dict):
    import app as webapp

    monkeypatch.setattr(webapp, "SESSIONS_ROOT", tmp_path / "sessions")
    webapp.SESSIONS_ROOT.mkdir(parents=True)
    webapp.app.config.update(TESTING=True, SECRET_KEY="test")
    client = webapp.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["sid"] = "sid"
    session_dir = webapp.SESSIONS_ROOT / "sid"
    session_dir.mkdir(parents=True)
    (session_dir / "manifest.json").write_text(
        __import__("json").dumps({"jobs": [job], "current_job_id": job["id"]}), encoding="utf-8"
    )
    return client, session_dir


def test_print_endpoint_rejects_file_outside_manifest(tmp_path: Path, monkeypatch):
    job = {"id": "job", "tracks": [], "playable_tabs": True, "refingered_name": None}
    client, job_root = _web_client_for_job(tmp_path, monkeypatch, job)
    output = job_root / "uploads" / "job" / "output"
    output.mkdir(parents=True)
    (output / "evil.pdf").write_bytes(b"%PDF")
    response = client.post("/jobs/job/print", data={"artifact": "evil.pdf"})
    assert response.status_code == 400


def test_regen_rejects_invalid_params_without_touching_artifact(tmp_path: Path, monkeypatch):
    job = {
        "id": "job",
        "stored_name": "song.gp",
        "tracks": [{
            "track_name": "Solo Guitar", "basename": "Solo Guitar",
            "playable": {"preset": "solo", "params": {}, "files": ["Solo Guitar.playable.gtrsnipe.txt"], "version": 1},
        }],
        "playable_tabs": True,
    }
    client, job_root = _web_client_for_job(tmp_path, monkeypatch, job)
    artifact = job_root / "uploads" / "job" / "output" / "Solo Guitar.playable.gtrsnipe.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("old", encoding="utf-8")
    before = artifact.stat().st_mtime_ns
    response = client.post("/jobs/job/playable/regen", data={
        "track": "Solo Guitar", "preset": "solo", "sweet_spot_low": "10", "sweet_spot_high": "5",
    })
    assert response.status_code == 400
    assert artifact.read_text(encoding="utf-8") == "old"
    assert artifact.stat().st_mtime_ns == before


def test_print_pdf_builds_lp_command_and_returns_failures(tmp_path: Path, monkeypatch):
    import tab_print

    pdf = tmp_path / "tab.pdf"
    pdf.write_bytes(b"%PDF-test")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="request id is HP-42 (1 file(s))\n", stderr=""))
    monkeypatch.setattr(tab_print.subprocess, "run", run)
    result = tab_print.print_pdf(pdf, server="192.168.20.64", printer="HP_P1102")
    assert result.ok and result.request_id == "HP-42"
    assert run.call_args.args[0] == [
        "lp", "-h", "192.168.20.64", "-d", "HP_P1102",
        "-o", "media=A4", "-o", "sides=one-sided", str(pdf.resolve()),
    ]

    run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="offline")
    failed = tab_print.print_pdf(pdf, server="x", printer="y")
    assert not failed.ok and "offline" in failed.error


def _synthetic_gp5(path: Path, string: int = 1, fret: int = 5) -> None:
    """Write a minimal single-note .gp5 through pyguitarpro itself."""
    import guitarpro
    from guitarpro import Beat, GuitarString, Measure, Note, Song, Track

    song = Song()
    header = song.measureHeaders[0]
    track = Track(song, number=1, name="Guitar")
    track.strings = [GuitarString(i + 1, p) for i, p in enumerate([64, 59, 55, 50, 45, 40])]
    measure = Measure(track, header)
    voice = measure.voices[0]
    beat = Beat(voice)
    note = Note(beat)
    note.string = string
    note.value = fret
    beat.notes = [note]
    voice.beats = [beat]
    measure.voices = [voice] + measure.voices[1:]
    track.measures = [measure]
    song.tracks = [track]
    guitarpro.write(song, str(path))


def _generation(note_positions):
    from playable_tabs import TrackGeneration

    return TrackGeneration(
        track_name="Guitar", track_type="GUITAR", basename="Guitar", preset="balanced",
        params={}, files=[], status={}, mapped=[], note_positions=note_positions,
    )


def test_gp5_refinger_accepts_its_own_correct_output(tmp_path: Path):
    """`note_positions` is 0-based (gtrsnipe); pyguitarpro reparses 1-based.

    Comparing the two bases directly made every .gp5 re-fingering fail its own
    invariant and delete the correct file it had just written.
    """
    from playable_tabs import refinger_gp

    src = tmp_path / "song.gp5"
    _synthetic_gp5(src, string=1, fret=5)  # highest string, pitch 69
    out = refinger_gp(src, {"Guitar": _generation([(0, 5)])}, tmp_path / "refingered.gp5")
    assert out.is_file()

    # A genuine alternative fingering of the same pitch (B string, fret 10) is
    # what re-fingering exists to produce, so it must also survive.
    alt = refinger_gp(src, {"Guitar": _generation([(1, 10)])}, tmp_path / "alt.gp5")
    assert alt.is_file()


def test_gp5_refinger_still_rejects_a_pitch_change(tmp_path: Path):
    from playable_tabs import refinger_gp

    src = tmp_path / "song.gp5"
    _synthetic_gp5(src, string=1, fret=5)
    out = tmp_path / "bad.gp5"
    with pytest.raises(ValueError, match="invariant failed"):
        refinger_gp(src, {"Guitar": _generation([(0, 7)])}, out)
    assert not out.exists()
