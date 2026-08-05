"""Hairpin -> CC11 на OTHER-треках: crescendo обязан давать слышимый подъём.

CC11 не бывает выше 127, поэтому crescendo якорится концом: старт
предослаблен до 127*v0/v1 и поднимается к 127. Раньше ветка с найденной
целью строила кривую 127 -> min(127, 127*v1/v0) = 127 -> 127: плоскую.
"""
from __future__ import annotations

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
    GPVoice,
)

QUARTER = GPDuration(value=4, time=960)


def _beat(velocity: int, hairpin: str | None = None) -> GPBeat:
    note = GPNote(string=1, value=0, velocity=velocity,
                  effect=GPNoteEffect(), type=None, realValue=0)
    return GPBeat(start=0, duration=QUARTER, notes=[note],
                  effect=GPBeatEffect(hairpin=hairpin), status=GPBeatStatus("normal"))


def _other_track(beats: list[GPBeat]) -> tuple[GPSong, GPTrack]:
    header = GPMeasureHeader(GPTimeSignature(4, GPDenominator(4)))
    track = GPTrack(
        name="Strings Pad",
        strings=[GPString(1, 60)],
        channel=GPChannel(48),
        measures=[GPMeasure(0, header, [GPVoice(beats)])],
    )
    return GPSong("fixture", "", "", 120, [track], [header]), track


def _cc11(midi_track) -> list[tuple[int, int]]:
    tick = 0
    out = []
    for message in midi_track:
        tick += message.time
        if message.type == "control_change" and message.control == g.HAIRPIN_CC:
            out.append((tick, message.value))
    return out


def test_crescendo_ramps_up_from_preattenuated_start():
    song, track = _other_track([
        _beat(79, hairpin="Crescendo"),   # v0 = 79 (MF)
        _beat(95),                        # v1 = 95 (F) — цель
    ])

    midi_track, _stats = g.build_other_midi(song, track)
    curve = [value for tick, value in _cc11(midi_track) if not (tick == 0 and value == 127)]

    assert curve, "crescendo не эмитнул ни одного CC11-события кривой"
    assert curve[0] == int(round(127.0 * 79 / 95))       # предослабленный старт
    assert curve[-1] == 127                              # подъём заканчивается на цели
    assert curve == sorted(curve)                        # монотонный подъём
    assert len(set(curve)) >= 3, f"кривая плоская: {curve}"


def test_decrescendo_ramp_is_unchanged_and_resets_on_target_attack():
    song, track = _other_track([
        _beat(95, hairpin="Decrescendo"),
        _beat(79),
    ])

    midi_track, _stats = g.build_other_midi(song, track)
    events = _cc11(midi_track)

    curve = [value for tick, value in events if 0 < tick < 960]
    assert curve and curve == sorted(curve, reverse=True)
    assert curve[-1] == int(round(127.0 * 79 / 95))
    # сброс в 127 на атаке целевой ноты
    assert (960, 127) in events


def test_crescendo_without_target_still_emits_nothing():
    song, track = _other_track([
        _beat(79, hairpin="Crescendo"),
        _beat(79),  # динамика не меняется — цели нет
    ])

    midi_track, _stats = g.build_other_midi(song, track)

    assert _cc11(midi_track) == []
