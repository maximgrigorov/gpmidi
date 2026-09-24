"""Ритм-секция вместе (--lock-to-drums) и второй дубль ритм-гитары.

Синтетика: барабаны с бочкой на каждой восьмой и гитара восьмыми в унисон.
Главное — test_unison_check_fails_without_the_lock: проверка, которая не падает
без фичи, ничего не проверяет (LESSONS.md п.11).
"""
from __future__ import annotations

import pytest
from guitarpro.models import NoteType

import gp_to_shreddage as g
import humanize as h
import rhythm_lock_check as rl
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

EIGHTH = GPDuration(value=8, time=480)
WHOLE = GPDuration(value=1, time=3840)
HEADER = GPMeasureHeader(GPTimeSignature(4, GPDenominator(4)))
GUITAR_STRINGS = [GPString(1, 64), GPString(2, 59), GPString(3, 55),
                  GPString(4, 50), GPString(5, 45), GPString(6, 40)]


def _note(value, string=6):
    return GPNote(string=string, value=value, velocity=95, effect=GPNoteEffect(),
                  type=NoteType.normal, realValue=0)


def _beat(notes, duration=EIGHTH):
    return GPBeat(start=0, duration=duration, notes=notes,
                  effect=GPBeatEffect(), status=GPBeatStatus("normal"))


def _rest(duration=WHOLE):
    return _beat([], duration)


def _song(bars_with_drums=16, bars_without=0):
    """Бочка на каждой восьмой; гитара и бас восьмыми (E2/E1). Потом такты без барабанов."""
    drum_bars = [[_beat([_note(36, string=1)]) for _ in range(8)] for _ in range(bars_with_drums)]
    drum_bars += [[_rest()] for _ in range(bars_without)]
    guitar_bars = [[_beat([_note(0), _note(2, string=5)]) for _ in range(8)]
                   for _ in range(bars_with_drums + bars_without)]
    bass_bars = [[_beat([_note(0, string=4)]) for _ in range(8)]
                 for _ in range(bars_with_drums + bars_without)]
    drums = GPTrack(name="Drums", strings=[GPString(1, 0)], channel=GPChannel(0),
                    measures=[GPMeasure(0, HEADER, [GPVoice(b)]) for b in drum_bars],
                    isPercussion=True)
    guitar = GPTrack(name="Rhythm Guitar", strings=GUITAR_STRINGS, channel=GPChannel(30),
                     measures=[GPMeasure(0, HEADER, [GPVoice(b)]) for b in guitar_bars])
    bass = GPTrack(name="Bass", strings=[GPString(1, 43), GPString(2, 38), GPString(3, 33),
                                         GPString(4, 28)], channel=GPChannel(33),
                   measures=[GPMeasure(0, HEADER, [GPVoice(b)]) for b in bass_bars])
    song = GPSong("fixture", "", "", 120, [drums, guitar, bass], [HEADER] * len(guitar_bars))
    return song, drums, guitar, bass


def _timeline(song, drums, seed=7):
    timeline = {}
    g.build_drum_midi(song, drums, humanize=True, humanize_seed=seed, timeline=timeline)
    return timeline


# --------------------------------------------------------------------------- #
#  Модель
# --------------------------------------------------------------------------- #
def test_pulse_prefers_kick_then_snare_and_interpolates_only_between_close_hits():
    pulse = h.DrumPulse({0: {"kick": 10, "snare": -5}, 480: {"snare": 20}, 10000: {"kick": 0}})
    assert pulse.anchor(0, 3840) == (10.0, "kick")
    assert pulse.anchor(480, 3840) == (20.0, "snare")
    shift, kind = pulse.anchor(240, 3840)
    assert kind == "between" and shift == pytest.approx(15.0)
    assert pulse.anchor(5000, 3840) is None, "между опорами больше такта — свободно"
    assert pulse.anchor(20000, 3840) is None, "за последней опорой — свободно"


def test_profiles_carry_the_lock_section_without_code_defaults():
    for name in ("guitar_metal", "bass_metal"):
        section = h.load_profile(name)["lock_to_drums"]
        assert set(h.LOCK_TO_DRUMS_KEYS) <= set(section)
    with pytest.raises(ValueError, match="kick_residual_ms"):
        h._validate_section({"snare_residual_ms": 1}, h.LOCK_TO_DRUMS_KEYS, "lock_to_drums", "x.yaml")


def test_timeline_does_not_change_the_drum_midi():
    song, drums, _guitar, _bass = _song(4)
    plain, _ = g.build_drum_midi(song, drums, humanize=True)
    with_timeline, _ = g.build_drum_midi(song, drums, humanize=True, timeline={})
    assert list(plain) == list(with_timeline)


# --------------------------------------------------------------------------- #
#  Проверка слаженности — и то, что она умеет падать
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("part", ["guitar", "bass"])
def test_lock_puts_the_part_on_the_kick(part):
    song, drums, guitar, bass = _song()
    track, track_type = (guitar, "GUITAR") if part == "guitar" else (bass, "BASS")
    timeline = _timeline(song, drums)
    reference, _ = g.build_instrument_midi(song, track, track_type)
    locked, stats = g.build_instrument_midi(song, track, track_type, humanize=True,
                                            lock_to_drums=True, drum_timeline=timeline)
    result = rl.kick_unison_spread(song, track, locked, reference, timeline)
    assert result["passed"], result
    assert stats["drum_lock"]["kick"] >= rl.MIN_UNISONS


@pytest.mark.parametrize("part", ["guitar", "bass"])
def test_unison_check_fails_without_the_lock(part):
    song, drums, guitar, bass = _song()
    track, track_type = (guitar, "GUITAR") if part == "guitar" else (bass, "BASS")
    timeline = _timeline(song, drums)
    reference, _ = g.build_instrument_midi(song, track, track_type)
    independent, _ = g.build_instrument_midi(song, track, track_type, humanize=True)
    result = rl.kick_unison_spread(song, track, independent, reference, timeline)
    assert result["n"] >= rl.MIN_UNISONS, "падать обязана по существу, а не из-за нехватки ударов"
    assert not result["passed"], result


def test_bars_without_drums_play_free():
    song, drums, guitar, _bass = _song(bars_with_drums=4, bars_without=4)
    _, stats = g.build_instrument_midi(song, guitar, "GUITAR", humanize=True,
                                       lock_to_drums=True, drum_timeline=_timeline(song, drums))
    assert stats["drum_lock"]["free"] >= 3 * 8, stats["drum_lock"]


def test_lock_needs_humanize_and_a_drummer():
    song, drums, guitar, _bass = _song(2)
    plain, _ = g.build_instrument_midi(song, guitar, "GUITAR")
    assert list(g.build_instrument_midi(song, guitar, "GUITAR", lock_to_drums=True,
                                        drum_timeline=_timeline(song, drums))[0]) == list(plain)
    _, stats = g.build_instrument_midi(song, guitar, "GUITAR", humanize=True,
                                       lock_to_drums=True, drum_timeline={})
    assert "drum_lock" not in stats


def test_hand_delay_waits_off_the_kick():
    """На унисоне с бочкой перенос руки не опаздывает (в метале такие места
    подтягивают к барабану); между ударами — опаздывает как обычно."""
    song, drums, guitar, _bass = _song()
    jumps = [[_beat([_note(f)]) for f in (2, 9) * 4] for _ in range(16)]
    guitar.measures = [GPMeasure(0, HEADER, [GPVoice(b)]) for b in jumps]
    _, stats = g.build_instrument_midi(song, guitar, "GUITAR", humanize=True, fret_hand_cost=True,
                                       lock_to_drums=True, drum_timeline=_timeline(song, drums))
    assert stats["fret_hand_cost_locked_skipped"] > 0
    assert stats.get("fret_hand_cost_beats", 0) < stats["fret_hand_cost_locked_skipped"]


# --------------------------------------------------------------------------- #
#  Дубль и CLI
# --------------------------------------------------------------------------- #
def test_double_is_the_same_part_with_its_own_feel():
    song, drums, guitar, _bass = _song(4)
    first, _ = g.build_instrument_midi(song, guitar, "GUITAR", humanize=True, humanize_seed=7)
    double, _ = g.build_instrument_midi(song, guitar, "GUITAR", humanize=True,
                                        humanize_seed=7 + g.DOUBLE_TRACK_SEED_OFFSET)
    g.rename_midi_track(double, "Rhythm Guitar" + g.DOUBLE_TRACK_SUFFIX)
    notes = lambda t: [m.note for m in t if m.type == "note_on" and m.velocity and m.note >= 28]  # noqa: E731
    assert notes(first) == notes(double), "дубль играет ту же партию"
    assert list(first) != list(double), "но со своим временем и velocity"
    assert any(m.type == "track_name" and m.name.endswith("(double)") for m in double)


def test_cli_flags_need_humanize():
    for flag in ("--lock-to-drums", "--double-rhythm-guitars"):
        with pytest.raises(ValueError, match="--humanize"):
            g.parse_cli_options(["gp_to_shreddage.py", "song.gp", flag])
    options = g.parse_cli_options(["gp_to_shreddage.py", "song.gp", "--humanize",
                                   "--lock-to-drums", "--double-rhythm-guitars"])
    assert options["lock_to_drums"] is True and options["double_rhythm_guitars"] is True
