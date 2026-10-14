"""Finger vibrato for lead guitar: manifest gating, CC1 conflicts, phrase placement.

The default ("uniform") pass of solo_expression_humanize.py stays as it was;
these options only narrow or reweight where pitch-wheel finger vibrato goes.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from mido import Message, MetaMessage, MidiFile, MidiTrack

import gp_to_shreddage as g
import solo_expression_humanize as solo

TEMPO = 800_000          # 75 BPM: 960 ticks = 800 ms
LONG = 1920              # 1.6 s, long enough for every style


def _track(notes, *, cc1=(), name="Lead Guitar"):
    """notes: (start, duration, pitch); cc1: (tick, value)."""
    rows = [(start, 1, Message("note_on", note=pitch, velocity=95)) for start, _d, pitch in notes]
    rows += [(start + duration, 0, Message("note_off", note=pitch, velocity=0))
             for start, duration, pitch in notes]
    rows += [(tick, 0, Message("control_change", control=1, value=value)) for tick, value in cc1]
    rows.sort(key=lambda row: (row[0], row[1]))
    track = MidiTrack([MetaMessage("track_name", name=name, time=0),
                       MetaMessage("set_tempo", tempo=TEMPO, time=0)])
    previous = 0
    for tick, _order, message in rows:
        track.append(message.copy(time=tick - previous))
        previous = tick
    track.append(MetaMessage("end_of_track", time=0))
    return track


def _bent_spans(enriched):
    """Start ticks of notes that received non-zero pitch-wheel motion."""
    tick, starts, bent = 0, {}, set()
    for message in enriched:
        tick += message.time
        if message.type == "note_on" and message.velocity:
            starts[message.note] = tick
        if message.type == "pitchwheel" and message.pitch:
            bent.update(start for start in starts.values() if start < tick)
        if message.type == "note_off":
            starts.pop(message.note, None)
    return bent


def _run(track, **kwargs):
    kwargs.setdefault("seed", 23)
    kwargs.setdefault("finger_vibrato_style", "expressive")
    return solo.humanize_solo_track(track, ticks_per_beat=960, finger_vibrato=True, **kwargs)


def _record(on_tick, pitch, reasons=()):
    return [{"on_tick": on_tick, "pitch": pitch,
             "finger_vibrato": {"eligible": not reasons, "skip_reasons": list(reasons)}}]


def test_manifest_allows_only_notes_the_converter_marked_eligible():
    notes = [(0, LONG, 64), (2400, LONG, 67), (4800, LONG, 69), (7200, LONG, 71)]
    track = _track(notes)
    manifest = {
        (0, 64): _record(0, 64),
        (2400, 67): _record(2400, 67, ["harmonic"]),
        (4800, 69): _record(4800, 69, ["polyphony", "authored_bend"]),
        # 7200/71 is missing on purpose: an unknown note is not eligible
    }
    for seed in range(1, 11):
        enriched, stats = _run(track, seed=seed, articulation_notes=manifest)
        assert _bent_spans(enriched) <= {0}
        assert stats["finger_vibrato_skipped"]["manifest:harmonic"] == 1
        assert stats["finger_vibrato_skipped"]["manifest:authored_bend+polyphony"] == 1
        assert stats["finger_vibrato_skipped"]["not_in_articulation_manifest"] == 1

    without, _ = _run(track, seed=3)
    assert len(_bent_spans(without)) > 1, "без отчёта те же ноты получают PB"


def test_notes_with_cc1_vibrato_are_skipped_when_asked_and_kept_by_default():
    notes = [(0, LONG, 64), (2400, LONG, 67)]
    cc1 = [(100, 20), (900, 32), (1900, 0)]                    # GP/auto CC1 vibrato
    track = _track(notes, cc1=cc1)

    default_bent = 0
    for seed in range(1, 11):
        guarded, stats = _run(track, skip_cc1_vibrato_notes=True, seed=seed)
        assert 0 not in _bent_spans(guarded)
        assert stats["finger_vibrato_skipped"]["cc1_vibrato"] == 1

        default, default_stats = _run(track, seed=seed)
        default_bent += 0 in _bent_spans(default)
        assert "cc1_vibrato" not in default_stats["finger_vibrato_skipped"]
    assert default_bent, "по умолчанию CC1-ноты не исключались — и не исключаются"


@pytest.mark.parametrize("kind", ["chord", "harmonic", "authored_bend"])
def test_converter_manifest_excludes_chords_harmonics_and_bent_notes(kind):
    eligible = (0, LONG, 64)
    other = (2400, LONG, 67)
    notes = [eligible, other]
    reasons = {"chord": ["polyphony"], "harmonic": ["harmonic"],
               "authored_bend": ["authored_bend"]}[kind]
    if kind == "chord":
        notes.append((2400, LONG, 71))
    manifest = {(0, 64): _record(0, 64), (2400, 67): _record(2400, 67, reasons),
                (2400, 71): _record(2400, 71, reasons)}
    for seed in range(1, 8):
        enriched, _stats = _run(_track(notes), seed=seed, articulation_notes=manifest)
        assert 2400 not in _bent_spans(enriched)


def test_phrase_placement_prefers_the_note_that_ends_the_phrase():
    # Пять одинаково долгих нот подряд (внутри фразы), затем пауза.
    run = [(index * LONG, LONG, 60 + index) for index in range(5)]
    track = _track(run)
    last = run[-1][0]
    interior = {start for start, _d, _p in run[:-1]}
    ends = solo._phrase_ends(solo._musical_note_spans(solo._absolute_events(track)),
                             solo._absolute_events(track), 960)
    assert len(ends) == 1

    counts = {"uniform": [0, 0], "phrase": [0, 0]}
    for placement in counts:
        for seed in range(1, 41):
            enriched, _stats = _run(track, seed=seed, finger_vibrato_style="natural",
                                    finger_vibrato_placement=placement)
            bent = _bent_spans(enriched)
            counts[placement][0] += last in bent
            counts[placement][1] += len(bent & interior)
    assert counts["phrase"][0] >= 36, counts                 # конец фразы почти всегда
    assert counts["phrase"][0] > counts["uniform"][0]
    assert counts["phrase"][1] < counts["uniform"][1] / 2   # внутри фразы — редко


def test_arrival_note_before_a_faster_run_counts_as_phrase_end():
    notes = [(0, LONG, 64)] + [(LONG + k * 240, 240, 66 + k % 3) for k in range(6)]
    track = _track(notes)
    events = solo._absolute_events(track)
    spans = solo._musical_note_spans(events)
    ends = solo._phrase_ends(spans, events, 960)
    held = next(span for span in spans if span["start"] == 0)
    assert id(held) in ends


def test_lead_options_require_finger_vibrato(tmp_path: Path):
    source = tmp_path / "in.mid"
    midi = MidiFile(type=1, ticks_per_beat=960)
    midi.tracks.append(_track([(0, LONG, 64)]))
    midi.save(source)
    with pytest.raises(ValueError, match="need --finger-vibrato"):
        solo.process_file(source, tmp_path / "out.mid", seed=1, velocity=True,
                          finger_vibrato_placement="phrase")


def test_manifest_must_match_kind_ppq_and_track(tmp_path: Path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"kind": "other"}))
    with pytest.raises(ValueError, match="not a gpmidi"):
        solo.load_articulation_manifest(path, "Lead Guitar", 960)
    path.write_text(json.dumps({"kind": solo.ARTICULATION_MANIFEST_KIND,
                                "ticks_per_beat": 480, "tracks": []}))
    with pytest.raises(ValueError, match="PPQ"):
        solo.load_articulation_manifest(path, "Lead Guitar", 960)
    path.write_text(json.dumps({"kind": solo.ARTICULATION_MANIFEST_KIND,
                                "ticks_per_beat": 960, "tracks": []}))
    with pytest.raises(ValueError, match="expected one track"):
        solo.load_articulation_manifest(path, "Lead Guitar", 960)


def test_converter_manifest_drives_the_post_pass_end_to_end(tmp_path: Path):
    shutil.copy("tests/fixtures/wizard_fixture.gp5", tmp_path / "song.gp5")
    g.main(["gp_to_shreddage.py", str(tmp_path / "song.gp5"), "--no-verify",
            "--slide-to-target", "--articulation-manifest"])
    out = tmp_path / "song_midi"

    manifest = solo.process_file(
        out / "song_ALL.mid", tmp_path / "lead.mid", seed=23,
        finger_vibrato=True, finger_vibrato_style="expressive",
        finger_vibrato_placement="phrase",
        articulation_manifest=out / "song_articulations.json",
        track_name="Guitar (Solo)", changed_tracks_only=True,
    )
    context = manifest["finger_vibrato_context"]
    assert context["placement"] == "phrase"
    assert context["skip_cc1_vibrato_notes"] is True
    assert context["articulation_manifest"]["track"] == "Guitar (Solo)"
    assert context["articulation_manifest"]["slide_mode"] == "target"
    assert "not_in_articulation_manifest" not in manifest["stats"]["finger_vibrato_skipped"]
    assert manifest["safety"]["note_timing_preserved"] is True


def test_every_hydra_keyswitch_is_a_service_note():
    """The post-pass must never treat a Hydra keyswitch as a musical note."""
    from articulation_config import config_for_track_type
    cfg = config_for_track_type("GUITAR")
    notes = {int(spec["note"]) for spec in cfg["keyswitches"].values()}
    notes |= {int(value) for value in cfg["fx_keyswitches"].values()}
    assert notes <= solo.HYDRA_SERVICE_NOTES, sorted(notes - solo.HYDRA_SERVICE_NOTES)


def test_pick_direction_keyswitches_do_not_block_vibrato_or_change():
    # --pick-direction: C7/C#7 pulses inside a long Lead note (gp_to_shreddage.py)
    track = _track([(0, LONG, 64), (900, 5, 108), (1500, 5, 109)])
    bent = 0
    for seed in range(1, 11):
        enriched, stats = solo.humanize_solo_track(
            track, ticks_per_beat=960, seed=seed, velocity=True,
            finger_vibrato=True, finger_vibrato_style="expressive")
        bent += 0 in _bent_spans(enriched)
        assert "polyphony" not in stats["finger_vibrato_skipped"]
        assert [m.velocity for m in enriched if m.type == "note_on" and m.note in (108, 109)] \
            == [95, 95]
    assert bent
