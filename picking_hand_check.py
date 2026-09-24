# -*- coding: utf-8 -*-
"""Приёмка правой руки: --palm-mute-motion и --pick-direction на готовом MIDI.

Глушение как движение: velocity соседних P.M.-ударов обязана быть связанной.
Метрика — автокорреляция с шагом 1 того, что ДОБАВИЛО оживление (velocity минус
velocity того же удара в экспорте без --humanize), после вычитания среднего по
метрическому классу (доля / офф-бит восьмая / слабая шестнадцатая): акценты
доли и слабый ход вверх — узор, а не движение. Без эталона проверку обманывает
нотная динамика: на ритме pnd участки PP/MP/F дали связность 0.89 без всякого
движения. У независимого разброса оживления она около нуля или отрицательная;
проверка обязана на нём падать (test_picking_hand.py, LESSONS.md п.11).

Направление медиатора: по keyswitch-ам Picking Mode восстанавливается ход
каждой ноты — для тестов и отчёта.
"""
from __future__ import annotations

import bisect
import statistics

import gp_to_shreddage as g
import hand_cost_check as hc
from humanize import metrical_class

# Связь соседних ударов, начиная с которой глушение «движется», а не дрожит.
PM_COHERENCE_MIN = 0.3
MIN_PM_PAIRS = 30
# Ноты ближе этого к первой — тот же удар (strum раскладывает аккорд <= 22 мс).
HIT_WINDOW_FRAC16 = 0.5


def _events(midi_track):
    tick, out = 0, []
    for msg in midi_track:
        tick += msg.time
        out.append((tick, msg))
    return out


def _pm_hits(midi_track, cfg, tpb):
    """[(tick, средняя velocity)] P.M.-ударов. Удар — ноты в пределах
    HIT_WINDOW_FRAC16 от первой: strum раскладывает аккорд по струнам."""
    ks = cfg["keyswitches"]
    pm_notes = {int(ks[name]["note"]) for name in ("palm_mute", "power_chord_mute") if name in ks}
    articulation_notes = {int(v["note"]) for v in ks.values() if isinstance(v, dict) and "note" in v}
    all_ks = hc._keyswitch_notes(cfg)
    window = HIT_WINDOW_FRAC16 * tpb / 4
    current, hits = None, []
    for tick, msg in _events(midi_track):
        if msg.type != "note_on" or not msg.velocity:
            continue
        if msg.note in all_ks:
            if msg.note in articulation_notes:
                current = msg.note
            continue
        if current not in pm_notes:
            continue
        if hits and tick - hits[-1][0] <= window:
            hits[-1][1].append(msg.velocity)
        else:
            hits.append((tick, [msg.velocity]))
    return [(t, statistics.mean(v)) for t, v in hits]


def pm_velocity_coherence(midi_track, cfg, reference_track=None, tpb=g.TICKS_PER_BEAT):
    """-> dict: pairs, lag1, passed. Фраза рвётся на паузе длиннее доли.

    reference_track — тот же экспорт без --humanize: вычитается нотная
    velocity того же удара, остаётся только добавленное оживлением.
    """
    played = _pm_hits(midi_track, cfg, tpb)
    hits = dict(played)
    if reference_track is not None:
        reference = _pm_hits(reference_track, cfg, tpb)
        ref_ticks = [t for t, _v in reference]
        for t, v in played:
            i = bisect.bisect_left(ref_ticks, t)
            near = [reference[j] for j in (i - 1, i) if 0 <= j < len(reference)]
            if near:
                hits[t] = v - min(near, key=lambda item: abs(item[0] - t))[1]
    ticks = sorted(hits)
    by_class = {}
    for t in ticks:
        by_class.setdefault(metrical_class(t, tpb), []).append(hits[t])
    means = {k: statistics.mean(v) for k, v in by_class.items()}
    residual = {t: hits[t] - means[metrical_class(t, tpb)] for t in ticks}
    pairs = [(residual[a], residual[b]) for a, b in zip(ticks, ticks[1:]) if b - a <= tpb]
    if len(pairs) < 2:
        return {"pairs": len(pairs), "lag1": None, "passed": False}
    xs, ys = zip(*pairs)
    sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
    lag1 = (statistics.mean(x * y for x, y in pairs) - statistics.mean(xs) * statistics.mean(ys)) / (sx * sy) \
        if sx and sy else 0.0
    return {"pairs": len(pairs), "lag1": round(lag1, 3),
            "passed": len(pairs) >= MIN_PM_PAIRS and lag1 >= PM_COHERENCE_MIN}


def pick_directions(midi_track, cfg):
    """[(tick, pitch, "down"|"up")] для каждой ноты по состоянию Picking Mode."""
    fx = cfg["fx_keyswitches"]
    up, down = int(fx["picking_mode_up"]), int(fx["picking_mode_down"])
    all_ks = hc._keyswitch_notes(cfg)
    state, out = None, []
    for tick, msg in _events(midi_track):
        if msg.type != "note_on" or not msg.velocity:
            continue
        if msg.note == up:
            state = "up"
        elif msg.note == down:
            state = "down"
        elif msg.note not in all_ks:
            out.append((tick, msg.note, state))
    return out
