"""Правая рука: направление медиатора (--pick-direction) и глушение как движение
(--palm-mute-motion). Главное — test_pm_coherence_fails_without_motion: проверка,
которая не падает без фичи, ничего не доказывает (LESSONS.md п.11)."""
from __future__ import annotations

import copy

import pytest
from guitarpro.models import NoteType
from mido import MidiFile

import gp_to_shreddage as g
import picking_hand_check as ph
from articulation_config import config_for_track_type
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
    GPVoice,
)
from verify_midi import smoke_check

EIGHTH = GPDuration(value=8, time=480)
SIXTEENTH = GPDuration(value=16, time=240)
QUARTER = GPDuration(value=4, time=960)
HEADER = GPMeasureHeader(GPTimeSignature(4, GPDenominator(4)))
CFG = config_for_track_type("GUITAR")


def _note(fret, string=6, **effect):
    return GPNote(string=string, value=fret, velocity=95, effect=GPNoteEffect(**effect),
                  type=NoteType.normal, realValue=0)


def _beat(notes, duration):
    return GPBeat(start=0, duration=duration, notes=notes,
                  effect=GPBeatEffect(), status=GPBeatStatus("normal"))


def _song(name, bars):
    track = GPTrack(name=name, channel=GPChannel(30),
                    strings=[GPString(1, 64), GPString(2, 59), GPString(3, 55),
                             GPString(4, 50), GPString(5, 45), GPString(6, 40)],
                    measures=[GPMeasure(0, HEADER, [GPVoice(b)]) for b in bars])
    return GPSong("fixture", "", "", 120, [track], [HEADER] * len(bars)), track


def _directions(song, track, **kwargs):
    midi, stats = g.build_instrument_midi(song, track, "GUITAR", pick_direction=True, **kwargs)
    return [d for _t, _p, d in ph.pick_directions(midi, CFG)], stats, midi


# --------------------------------------------------------------------------- #
#  Направление медиатора
# --------------------------------------------------------------------------- #
def test_rhythm_eighths_are_all_downstrokes_and_sixteenths_alternate():
    song, track = _song("Rhythm Guitar", [[_beat([_note(3)], EIGHTH) for _ in range(8)]] * 2)
    directions, stats, _ = _directions(song, track)
    assert set(directions) == {"down"} and stats["pick_direction"]["ks"] == 1
    song, track = _song("Rhythm Guitar", [[_beat([_note(3)], SIXTEENTH) for _ in range(16)]])
    directions, _, _ = _directions(song, track)
    assert directions == ["down", "up"] * 8


def test_rhythm_downpicks_sixteenths_while_the_hand_keeps_up():
    """«Чёс вниз больше»: шестнадцатые на медленном темпе — сплошь вниз."""
    bars = [[_beat([_note(3)], SIXTEENTH) for _ in range(16)]]
    song, track = _song("Rhythm Guitar", bars)
    song.tempo = 90                                   # 167 мс между ударами
    directions, _, _ = _directions(song, track)
    assert set(directions) == {"down"}


def test_solo_alternates_picked_notes_and_skips_legato_targets():
    # 3 (hammer) -> 5 — цель hammer не берётся медиатором и не сбивает чередование
    beats = [_beat([_note(3, hammer=True)], EIGHTH), _beat([_note(5)], EIGHTH),
             _beat([_note(7)], EIGHTH), _beat([_note(8)], EIGHTH)] * 2
    song, track = _song("Solo Guitar", [beats])
    directions, _, _ = _directions(song, track)
    #          3h     5(tgt)  7     8     3h    5(tgt)  7      8
    assert directions == ["down", "down", "up", "down", "up", "up", "down", "up"]


def test_solo_resets_to_downstroke_after_a_rest():
    beats = [_beat([_note(3)], EIGHTH), _beat([], QUARTER), _beat([], EIGHTH),
             _beat([_note(5)], EIGHTH), _beat([_note(7)], EIGHTH),
             _beat([_note(8)], QUARTER)]
    song, track = _song("Solo Guitar", [beats])
    directions, _, _ = _directions(song, track)
    assert directions == ["down", "down", "up", "down"]


def test_pick_keyswitches_arrive_before_their_notes(tmp_path):
    song, track = _song("Rhythm Guitar", [[_beat([_note(3)], SIXTEENTH) for _ in range(16)]] * 2)
    _, _, midi = _directions(song, track, humanize=True)
    path = tmp_path / "pick.mid"
    f = MidiFile(type=0, ticks_per_beat=g.TICKS_PER_BEAT)
    f.tracks.append(midi)
    f.save(path)
    codes = {code for _s, code, _m in smoke_check(path, "GUITAR")}
    assert not codes & {"KS_LEAD", "KS_NO_INIT", "STUCK", "KEY_RETRIGGER"}


def test_pick_direction_is_opt_in_and_needs_its_keyswitches():
    song, track = _song("Rhythm Guitar", [[_beat([_note(3)], EIGHTH) for _ in range(8)]])
    plain, _ = g.build_instrument_midi(song, track, "GUITAR")
    assert list(g.build_instrument_midi(song, track, "GUITAR", pick_direction=False)[0]) == list(plain)
    cfg = copy.deepcopy(CFG)
    del cfg["fx_keyswitches"]["picking_mode_up"]
    with pytest.raises(ValueError, match="picking_mode_up"):
        g.build_instrument_midi(song, track, "GUITAR", cfg=cfg, pick_direction=True)


# --------------------------------------------------------------------------- #
#  Глушение ладонью как движение
# --------------------------------------------------------------------------- #
def _chug_song(bars=16):
    return _song("Rhythm Guitar", [[_beat([_note(0, palmMute=True), _note(2, string=5, palmMute=True)],
                                          SIXTEENTH) for _ in range(16)] for _ in range(bars)])


def test_pm_coherence_passes_with_motion():
    song, track = _chug_song()
    reference, _ = g.build_instrument_midi(song, track, "GUITAR")
    midi, stats = g.build_instrument_midi(song, track, "GUITAR", humanize=True, palm_mute_motion=True)
    result = ph.pm_velocity_coherence(midi, CFG, reference)
    assert result["passed"], result
    assert stats["palm_mute_motion_beats"] == 16 * 16


def test_pm_coherence_fails_without_motion():
    song, track = _chug_song()
    reference, _ = g.build_instrument_midi(song, track, "GUITAR")
    midi, _ = g.build_instrument_midi(song, track, "GUITAR", humanize=True)
    result = ph.pm_velocity_coherence(midi, CFG, reference)
    assert result["pairs"] >= ph.MIN_PM_PAIRS, "падать обязана по существу"
    assert not result["passed"], result


def test_pm_coherence_is_not_fooled_by_notated_dynamics():
    """Участки PP и F в нотах связывают velocity сами по себе; без эталона
    проверка приняла бы это за движение руки (ритм pnd: 0.89 без фичи)."""
    bars = []
    for level in (31, 95) * 8:
        bar = []
        for _ in range(16):
            note = _note(0, palmMute=True)
            note.velocity = level
            bar.append(_beat([note], SIXTEENTH))
        bars.append(bar)
    song, track = _song("Rhythm Guitar", bars)
    reference, _ = g.build_instrument_midi(song, track, "GUITAR")
    midi, _ = g.build_instrument_midi(song, track, "GUITAR", humanize=True)
    assert ph.pm_velocity_coherence(midi, CFG)["passed"], "без эталона динамика обманывает проверку"
    assert not ph.pm_velocity_coherence(midi, CFG, reference)["passed"]


def test_pm_motion_touches_only_palm_muted_notes_and_stays_in_range():
    open_bar = [_beat([_note(3)], SIXTEENTH) for _ in range(16)]
    chug_bar = [_beat([_note(0, palmMute=True)], SIXTEENTH) for _ in range(16)]
    song, track = _song("Rhythm Guitar", [open_bar, chug_bar] * 4)

    def velocities(midi):
        return [m.velocity for m in midi if m.type == "note_on" and m.velocity and m.note >= 28]

    base = velocities(g.build_instrument_midi(song, track, "GUITAR", humanize=True)[0])
    moved = velocities(g.build_instrument_midi(song, track, "GUITAR", humanize=True,
                                               palm_mute_motion=True)[0])
    open_idx = [i for i in range(len(base)) if (i // 16) % 2 == 0]
    assert [base[i] for i in open_idx] == [moved[i] for i in open_idx], "открытые ноты не трогаются"
    assert base != moved
    assert all(1 <= v <= 127 for v in moved)


def test_cli_flags():
    with pytest.raises(ValueError, match="--humanize"):
        g.parse_cli_options(["gp_to_shreddage.py", "song.gp", "--palm-mute-motion"])
    options = g.parse_cli_options(["gp_to_shreddage.py", "song.gp", "--pick-direction"])
    assert options["pick_direction"] is True and options["palm_mute_motion"] is False
