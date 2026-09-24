"""--humanize: конец ноты не может убить соседнюю ноту и не может порвать легато.

Корень (LESSONS.md п.15): конец ноты считается от СДВИНУТОГО начала бита, а
соседние биты сдвигаются независимо. Отсюда три симптома, все пойманы на живых
файлах 2026-09-24:
  - note_off предыдущей ноты той же высоты приходит ПОСЛЕ атаки следующей и
    гасит её: в Through the Night пропадала двухсекундная B3 (такт 2), всего 19
    нот соло; на ритме pnd — 616 из 1881 ноты;
  - hammer/pull, связанные нотами, рвутся: цель уехала позже конца источника,
    и конвертер решает, что между ними пауза (44-48 из 94 пар Spring Melody);
  - две ноты на ОДНОЙ струне звучат внахлёст, чего струна не умеет, — а Hydra
    играет легато по перекрытию.
Без --humanize экспорт не меняется: всё ниже включается только с профилем.
"""
from __future__ import annotations

import pytest
from guitarpro.models import NoteType

import gp_to_shreddage as g
import hand_cost_check as hc
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

SIXTEENTH = GPDuration(value=16, time=240)
EIGHTH = GPDuration(value=8, time=480)
HALF = GPDuration(value=2, time=1920)
KS = hc._keyswitch_notes(config_for_track_type(g.TRACK_GUITAR))
SEEDS = range(1, 9)


def _note(fret, *, string=2, **effect):
    return GPNote(string=string, value=fret, velocity=95, effect=GPNoteEffect(**effect),
                  type=NoteType.normal, realValue=0)


def _beat(notes, duration=SIXTEENTH):
    return GPBeat(start=0, duration=duration, notes=notes,
                  effect=GPBeatEffect(), status=GPBeatStatus("normal"))


def _song(bars):
    header = GPMeasureHeader(GPTimeSignature(4, GPDenominator(4)))
    track = GPTrack(
        name="Solo Guitar",
        strings=[GPString(1, 64), GPString(2, 59), GPString(3, 55),
                 GPString(4, 50), GPString(5, 45), GPString(6, 40)],
        channel=GPChannel(30),
        measures=[GPMeasure(0, header, [GPVoice(beats)]) for beats in bars],
    )
    return GPSong("fixture", "", "", 90, [track], [header for _ in bars]), track


def _notes(midi_track):
    tick, sounding, out = 0, {}, []
    for msg in midi_track:
        tick += msg.time
        if msg.type not in ("note_on", "note_off") or msg.note in KS:
            continue
        if msg.type == "note_on" and msg.velocity > 0:
            sounding.setdefault(msg.note, []).append(tick)
        elif sounding.get(msg.note):
            out.append((sounding[msg.note].pop(0), tick, msg.note))
    return sorted(out)


def _build(song, track, **kwargs):
    return g.build_instrument_midi(song, track, g.TRACK_GUITAR, **kwargs)[0]


@pytest.mark.parametrize("seed", SEEDS)
def test_humanize_never_attacks_a_pitch_that_is_still_sounding(seed):
    # восьмая, потом длинная нота той же высоты — ровно такт 2 соло Through the Night
    song, track = _song([[_beat([_note(4)], EIGHTH), _beat([_note(4)], EIGHTH),
                          _beat([_note(4)], HALF), _beat([_note(4)], EIGHTH),
                          _beat([_note(4)], EIGHTH)] for _ in range(8)])
    notes = _notes(_build(song, track, humanize=True, humanize_seed=seed))
    for prev, nxt in zip(notes, notes[1:]):
        assert prev[1] <= nxt[0], f"note_off {prev} гасит следующую ноту {nxt}"


@pytest.mark.parametrize("seed", SEEDS)
def test_humanize_keeps_notated_hammer_legato(seed):
    song, track = _song([[_beat([_note(5, hammer=True)]), _beat([_note(7)])] * 8
                         for _ in range(4)])
    notes = _notes(_build(song, track, humanize=True, humanize_seed=seed))
    for source, target in zip(notes[0::2], notes[1::2]):
        assert source[1] > target[0], f"легато {source} -> {target} порвано"


@pytest.mark.parametrize("seed", SEEDS)
def test_humanize_one_string_sounds_one_note(seed):
    song, track = _song([[_beat([_note(f)]) for f in (5, 7, 8, 7) * 4] for _ in range(4)])
    notes = _notes(_build(song, track, humanize=True, humanize_seed=seed))
    for prev, nxt in zip(notes, notes[1:]):
        assert prev[1] <= nxt[0], f"{prev} и {nxt} на одной струне внахлёст"


def test_same_pitch_hammer_does_not_swallow_its_target_under_humanize():
    # Spring Melody, такт 59: hammer с A#4 на ту же A#4. Легато-перекрытие 40 мс
    # тянуло источник поверх цели той же высоты, и цель звучала 40 мс из 240.
    song, track = _song([[_beat([_note(11, string=1, hammer=True)], EIGHTH),
                          _beat([_note(11, string=1)], EIGHTH)] * 4 for _ in range(2)])
    notes = _notes(_build(song, track, humanize=True))
    for prev, nxt in zip(notes, notes[1:]):
        assert prev[1] <= nxt[0]
