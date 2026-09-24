# -*- coding: utf-8 -*-
"""Приёмка стоимости переноса левой руки (--fret-hand-cost) на готовом MIDI.

Главная проверка — ДИФФЕРЕНЦИАЛЬНАЯ: на битах, где рука переезжает на
min_fret_shift ладов и больше, атака обязана опаздывать статистически сильнее,
чем на битах без переноса. Общий разброс тут ничего не доказывает: его растит и
обычный --humanize, а фича может при этом не работать вовсе.

Классы битов считаются по партитуре независимо от конвертера (своим обходом),
опоздание — по артефакту: первая атака бита минус его нотный тик, в мс по темпу
этого места. Цели hammer/слайда из обеих групп исключены: модель их не
задерживает намеренно (LESSONS.md п.15), и смешивать их с прыжками значит
мерить правило исключения, а не перенос руки.

Проверка обязана ПАДАТЬ на выходе без фичи — и на квантованном экспорте, и на
обычном --humanize (LESSONS.md п.11: проверка, которая ни разу не срабатывала,
не проверена). Это закреплено в test_fret_hand_cost.py.

Здесь же jitter_ms (LESSONS.md п.3): отклонение от ОБЪЕДИНЁННОЙ сетки 16-я +
триоль в мс. pct_off_grid16 не используется — считает триоль живостью.

Использование:
    python hand_cost_check.py song.gp "Guitar (Solo)" out/Guitar_Solo.mid
"""
from __future__ import annotations

import argparse
import bisect
import math
import statistics
import sys
import warnings
from dataclasses import dataclass

import mido

import gp_to_shreddage as g
from articulation_config import config_for_track_type

# Порог значимости одностороннего критерия Манна-Уитни (перенос > без переноса).
ALPHA = 0.01
# Значимой, но ничтожной разницы мало: медиана опоздания на прыжках обязана
# превышать медиану на битах без переноса хотя бы на столько. Шум --humanize —
# std 7 мс, модель даёт 10-18 мс; 3 мс отсекают «значимо, но не слышно».
MIN_MEDIAN_GAIN_MS = 3.0
# Меньше битов в группе — критерию нечего сказать, проверка не пройдена.
MIN_GROUP = 20
# Общий делитель 16-й и триоли на четверть: та же сетка, что у RIGID.
GRID_DIVISIONS_PER_BEAT = 12


@dataclass
class ScoreBeat:
    grid_tick: int
    bpm: float
    pitches: tuple
    delta: float | None
    legato_target: bool


def score_beats(song, track, cfg):
    """Биты гитарной партии с переносом руки и признаком цели легато."""
    strings = {s.number: s.value for s in track.strings}
    bpm = float(song.tempo) if song.tempo else 120.0
    previous = None
    legato_into = {}
    out = []
    for _m, _v, vi, _bi, beat, _mst, tick, dur in g.iter_voice_beats_with_canonical_ticks(track):
        if beat is None:
            continue
        mtc = getattr(beat.effect, "mixTableChange", None)
        if mtc is not None and getattr(mtc, "tempo", None) and mtc.tempo.value:
            bpm = float(mtc.tempo.value)
        if not beat.notes:
            continue
        fresh = [n for n in beat.notes if n.type != g.NoteType.tie]
        position = g.beat_hand_position(beat)
        if fresh:
            pitches = []
            for note in fresh:
                pitch = g.shreddage_harmonic_pitch(note, strings, cfg)
                pitches.append(pitch if pitch is not None
                               else g.clamp_note(strings.get(note.string, 0) + note.value))
            out.append(ScoreBeat(
                grid_tick=tick, bpm=bpm, pitches=tuple(pitches),
                delta=(abs(position - previous)
                       if position is not None and previous is not None else None),
                legato_target=any(legato_into.get((vi, n.string), -1) >= tick for n in fresh),
            ))
        for note in beat.notes:
            key = (vi, note.string)
            if note.type == g.NoteType.tie:
                if key in legato_into:
                    legato_into[key] = tick + dur
            elif note.effect.hammer or any(
                    s in g.LEGATO_SLIDE_TYPES for s in (note.effect.slides or [])):
                legato_into[key] = tick + dur
            else:
                legato_into.pop(key, None)
        if position is not None:
            previous = position
    return out


def _keyswitch_notes(cfg):
    notes = {int(spec["note"]) for spec in (cfg.get("keyswitches") or {}).values()
             if isinstance(spec, dict) and "note" in spec}
    notes |= {n for n in (cfg.get("fx_keyswitches") or {}).values() if isinstance(n, int)}
    return notes


def midi_attacks(midi_track, ks_notes):
    """{pitch: sorted [attack_tick]} без keyswitch-нот."""
    tick, out = 0, {}
    for msg in midi_track:
        tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and msg.note not in ks_notes:
            out.setdefault(msg.note, []).append(tick)
    for ticks in out.values():
        ticks.sort()
    return out


def _nearest(sorted_ticks, target):
    i = bisect.bisect_left(sorted_ticks, target)
    candidates = sorted_ticks[max(0, i - 1):i + 1]
    return min(candidates, key=lambda t: abs(t - target)) if candidates else None


def beat_offsets_ms(song, track, midi_track, cfg, tpb=g.TICKS_PER_BEAT):
    """[(ScoreBeat, опоздание первой атаки бита в мс)] по артефакту."""
    attacks = midi_attacks(midi_track, _keyswitch_notes(cfg))
    rows = []
    for beat in score_beats(song, track, cfg):
        found = [_nearest(attacks.get(p, []), beat.grid_tick) for p in beat.pitches]
        found = [t for t in found if t is not None]
        if not found:
            continue
        rows.append((beat, g.ticks_to_ms(min(found) - beat.grid_tick, beat.bpm)))
    return rows


def mann_whitney_greater(xs, ys):
    """Односторонний U-критерий (xs > ys), нормальное приближение с поправкой на
    связки и непрерывность. Возвращает p; при нулевой дисперсии — 1.0."""
    n1, n2 = len(xs), len(ys)
    if not n1 or not n2:
        return 1.0
    pooled = sorted([(v, 0) for v in xs] + [(v, 1) for v in ys])
    ranks, ties, i = [0.0] * len(pooled), [], 0
    while i < len(pooled):
        j = i
        while j + 1 < len(pooled) and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2.0 + 1
        ties.append(j - i + 1)
        i = j + 1
    r1 = sum(r for r, (_v, grp) in zip(ranks, pooled) if grp == 0)
    u = r1 - n1 * (n1 + 1) / 2.0
    n = n1 + n2
    variance = n1 * n2 / 12.0 * ((n + 1) - sum(t ** 3 - t for t in ties) / (n * (n - 1)))
    if variance <= 0:
        return 1.0
    z = (u - n1 * n2 / 2.0 - 0.5) / math.sqrt(variance)
    return 0.5 * math.erfc(z / math.sqrt(2))


def differential(song, track, midi_track, cfg=None):
    """Опаздывают ли прыжки сильнее битов без переноса. -> dict с 'passed'."""
    cfg = cfg or config_for_track_type(g.TRACK_GUITAR)
    shift_frets = float(cfg["performance_life"]["fret_noise_on_hand_shift"]["min_fret_shift"])
    rows = beat_offsets_ms(song, track, midi_track, cfg)
    shift = [ms for b, ms in rows
             if b.delta is not None and b.delta >= shift_frets and not b.legato_target]
    zero = [ms for b, ms in rows if b.delta == 0 and not b.legato_target]
    p = mann_whitney_greater(shift, zero)
    gain = (statistics.median(shift) - statistics.median(zero)) if shift and zero else 0.0
    passed = (len(shift) >= MIN_GROUP and len(zero) >= MIN_GROUP
              and p < ALPHA and gain >= MIN_MEDIAN_GAIN_MS)
    return {
        "passed": passed, "p_value": p, "median_gain_ms": round(gain, 2),
        "shift_beats": len(shift), "zero_beats": len(zero),
        "shift_median_ms": round(statistics.median(shift), 2) if shift else None,
        "zero_median_ms": round(statistics.median(zero), 2) if zero else None,
        "legato_targets_excluded": sum(1 for b, _ms in rows if b.legato_target),
        "onset_std_ms": round(statistics.pstdev([ms for _b, ms in rows]), 2) if rows else None,
    }


def jitter_ms(midi_track, cfg, tpb=g.TICKS_PER_BEAT):
    """std отклонения атак от объединённой сетки 16-я + триоль, мс (LESSONS.md п.3)."""
    ks_notes = _keyswitch_notes(cfg)
    grid = tpb / GRID_DIVISIONS_PER_BEAT
    tempo = mido.bpm2tempo(120)
    tick, devs = 0, []
    for msg in midi_track:
        tick += msg.time
        if msg.type == "set_tempo":
            tempo = msg.tempo
        elif msg.type == "note_on" and msg.velocity > 0 and msg.note not in ks_notes:
            d = tick % grid
            d = d - grid if d > grid / 2 else d
            devs.append(d * tempo / 1000.0 / tpb)
    return round(statistics.pstdev(devs), 2) if devs else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Дифференциальная проверка --fret-hand-cost")
    ap.add_argument("score")
    ap.add_argument("track")
    ap.add_argument("midi")
    args = ap.parse_args(argv)
    from gp_import import parse_song
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        song = parse_song(args.score)
    track = next(t for t in song.tracks if t.name == args.track)
    midi_track = mido.MidiFile(args.midi).tracks[0]
    cfg = config_for_track_type(g.TRACK_GUITAR)
    result = differential(song, track, midi_track, cfg)
    result["jitter_ms"] = jitter_ms(midi_track, cfg)
    for key, value in result.items():
        print(f"{key:24} {value}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
