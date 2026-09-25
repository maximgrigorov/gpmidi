# -*- coding: utf-8 -*-
"""Приёмка --lock-to-drums: сходятся ли бас и гитара с бочкой на общих ударах.

Метрика — разброс (std, мс) между первой атакой бита инструмента и ударом
бочки на том же нотном тике. Первой, потому что strum сдвигает верхние струны
аккорда вперёд намеренно. Ожидаемое место удара инструмента — атака той же
ноты в экспорте без --humanize (эталон несёт авторские сдвиги GP, если они
включены) плюс сдвиг бочки.

Без привязки гитара (7 мс) и бочка (9 мс) дрожат независимо и расходятся на
~11 мс; проверка обязана на этом падать (test_rhythm_lock.py), иначе она ничего
не доказывает (LESSONS.md п.11).
"""
from __future__ import annotations

import statistics

import gp_to_shreddage as g
import hand_cost_check as hc

# Допуск «собранной» ритм-секции: std расхождения с бочкой на унисонах. Больше —
# на слух «плывёт». Не измерение исполнителя, а договорённость сведения.
TIGHT_KICK_STD_MS = 4.0
MIN_UNISONS = 20


def kick_unison_spread(song, track, midi_track, reference_track, drum_timeline, cfg=None):
    """-> dict: n, std_ms, mean_ms, passed."""
    cfg = cfg or g.config_for_track_type(g.resolve_track_type(track))
    ks = hc._keyswitch_notes(cfg)
    attacks = hc.midi_attacks(midi_track, ks)
    reference = hc.midi_attacks(reference_track, ks)
    deltas = []
    for beat in hc.score_beats(song, track, cfg):
        hits = drum_timeline.get(beat.grid_tick) or {}
        if "kick" not in hits:
            continue
        pairs = []
        for pitch in beat.pitches:
            anchor = hc._nearest(reference.get(pitch, []), beat.grid_tick)
            found = hc._nearest(attacks.get(pitch, []), anchor) if anchor is not None else None
            if found is not None:
                pairs.append((found, anchor))
        if not pairs:
            continue
        attack, anchor = min(pairs)
        deltas.append(g.ticks_to_ms(attack - (anchor + hits["kick"]), beat.bpm))
    std = statistics.pstdev(deltas) if deltas else None
    return {"n": len(deltas),
            "std_ms": round(std, 2) if std is not None else None,
            "mean_ms": round(statistics.mean(deltas), 2) if deltas else None,
            "passed": len(deltas) >= MIN_UNISONS and std <= TIGHT_KICK_STD_MS}


def drum_timeline_for(song, humanize, seed):
    """Тайминг барабанщика так же, как его строит экспорт: все барабанные дорожки."""
    timeline = {}
    for track in song.tracks:
        if g.resolve_track_type(track) == g.TRACK_DRUMS:
            g.build_drum_midi(song, track, humanize=humanize, humanize_seed=seed, timeline=timeline)
    return timeline
