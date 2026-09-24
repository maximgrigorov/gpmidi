# -*- coding: utf-8 -*-
"""Оживление партий (velocity + микро-тайминг), профили в config/humanize_profiles/*.yaml.

ЗАЧЕМ. Guitar Pro хранит динамику восемью градациями (PPP..FFF, см.
APOLLOTAB_DYNAMIC_TO_VELOCITY в gp_import), а для перкуссии отдаёт вообще одну:
барабанный трек приходит плоской линией velocity=95. Микро-тайминга в нотной
записи нет по определению — нота либо на доле, либо это другая длительность.
Поэтому и то и другое приходится синтезировать, и делать это ОБЯЗАТЕЛЬНО после
Guitar Pro: любой возврат в GP схлопнет результат обратно в нотацию.

ЧТО ЭТО НЕ ДЕЛАЕТ. Не меняет ноты, их питчи, порядок и структуру — только
velocity и микро-сдвиг. Единственное исключение — ghost_notes (добавляет ноты),
выключено по умолчанию.

Порог применения — на вызывающей стороне: по умолчанию оживление ВЫКЛЮЧЕНО,
поведение экспорта без флага остаётся байт-в-байт прежним (AGENTS.md).
"""
from __future__ import annotations

import bisect
import logging
import math
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger("gpmidi.humanize")

PROFILE_DIR = Path(__file__).resolve().parent / "config" / "humanize_profiles"

PROFILE_BY_TRACK_TYPE = {
    "DRUMS": "drums_metal",
    "GUITAR": "guitar_metal",
    "BASS": "bass_metal",
}

# GM-нота -> лимб. Райд и хэт объединены в timekeeper: это одна остинатная рука,
# и статистика удара у них общая.
DRUM_CLASS = {
    35: "kick", 36: "kick",
    37: "snare", 38: "snare", 39: "snare", 40: "snare",
    41: "tom", 43: "tom", 45: "tom", 47: "tom", 48: "tom", 50: "tom",
    42: "timekeeper", 44: "timekeeper", 46: "timekeeper",
    51: "timekeeper", 53: "timekeeper", 59: "timekeeper",
    49: "crash", 52: "crash", 55: "crash", 57: "crash",
}


@lru_cache(maxsize=None)
def load_profile(name: str) -> dict:
    path = PROFILE_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Профиль оживления не найден: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    # An empty or non-mapping profile used to fail with a bare TypeError on the
    # line below; a profile missing a required section failed later with a bare
    # KeyError mid-render, naming neither the file nor the key.
    if not isinstance(data, dict):
        raise ValueError(f"Профиль оживления пуст или не является отображением: {path}")
    for section in ("velocity", "timing"):
        if not isinstance(data.get(section), dict):
            raise ValueError(f"В профиле {path} отсутствует или испорчена секция '{section}'")
    if "fret_hand_cost" in data:
        _validate_fret_hand_cost(data["fret_hand_cost"], path)
    ghost = data.get("ghost_notes")
    if isinstance(ghost, dict) and isinstance(ghost.get("velocity"), dict):
        low, high = ghost["velocity"].get("min"), ghost["velocity"].get("max")
        if isinstance(low, int) and isinstance(high, int) and low > high:
            raise ValueError(f"В профиле {path}: ghost_notes.velocity.min > max ({low} > {high})")
    data["_profile_name"] = name
    return data


def profile_for_track_type(track_type: str) -> dict | None:
    name = PROFILE_BY_TRACK_TYPE.get(track_type)
    return load_profile(name) if name else None


def profile_label(prof: dict) -> str:
    return (f"{prof.get('instrument', '?')}/{prof.get('profile', '?')} "
            f"(config_version {prof.get('config_version', '?')}, "
            f"{prof['_profile_name']}.yaml)")


def metrical_class(tick: int, tpb: int) -> str:
    """Доля / офф-бит восьмая / слабая шестнадцатая."""
    six = tpb / 4
    p = int(round((tick % (tpb * 4)) / six)) % 16
    if p % 4 == 0:
        return "beat"
    if p % 4 == 2:
        return "off8"
    return "e16"


def sixteenth_index(tick: int, tpb: int) -> int:
    return int(round((tick % (tpb * 4)) / (tpb / 4))) % 16


class _BarGrid:
    """Геометрия тактов для оживления барабанов.

    bar_starts — тики начал тактов ПЛЮС финальный конец (n+1 значений),
    посчитанные вызывающим по РЕАЛЬНОМУ треку. Без них — равномерная сетка
    4/4 (историческое поведение; для 4/4-треков результат байт-в-байт
    совпадает). В 3/4 и 6/8 равномерная сетка перекрывала реальные тактовые
    черты: empty_bars определялись неверно, гост-ноты уезжали за черту, а
    фильтр филлов считал удары по чужому окну.
    """

    def __init__(self, tpb, bar_starts=None):
        self._uniform = tpb * 4
        self._starts = (
            list(bar_starts)
            if bar_starts is not None and len(bar_starts) >= 2 else None
        )

    def index(self, tick: int) -> int:
        if self._starts is None:
            return tick // self._uniform
        if tick >= self._starts[-1]:
            # сдвинутая за последний такт нота: экстраполируем последним тактом
            last_len = max(1, self._starts[-1] - self._starts[-2])
            return (len(self._starts) - 1) + (tick - self._starts[-1]) // last_len
        return max(0, bisect.bisect_right(self._starts, tick) - 1)

    def start(self, index: int) -> int:
        if self._starts is None:
            return index * self._uniform
        if index >= len(self._starts) - 1:
            last_len = max(1, self._starts[-1] - self._starts[-2])
            return self._starts[-1] + (index - (len(self._starts) - 1)) * last_len
        return self._starts[max(0, index)]

    def end(self, index: int) -> int:
        return self.start(index + 1)


def humanize_drums(notes, bpm, tpb, prof, rng, ghost_override=None,
                   tempo_map=None, bar_starts=None):
    """
    Оживляет барабанные ноты НА МЕСТЕ.

    notes: список dict с ключами tick, note, vel, dur (тики в шкале tpb).
    Возвращает статистику для отчёта.

    tempo_map — [(tick, bpm), ...] по треку: миллисекундный джиттер каждой
    ноты считается по темпу, действующему на ЕЁ тике. Раньше один bpm
    (последний в треке) масштабировал все ноты: в многотемповой песне финал
    на 180 BPM вдвое урезал задуманный ms-джиттер куплета на 90.
    bar_starts — реальные границы тактов (см. _BarGrid). Без обоих аргументов
    поведение прежнее: один bpm и сетка 4/4.

    Velocity синтезируется целиком (на входе плоские 95): уровень лимба +
    метрический акцент + человеческий остаток. Тайминг — гауссов сдвиг с
    систематическим bias по лимбу, зажатый долей шестнадцатой, поэтому на
    быстрых треках рисунок не может размазаться.
    """
    v = prof["velocity"]
    t = prof["timing"]
    ref = float(v.get("reference_level", 95))
    floor, ceil = int(v.get("floor", 30)), int(v.get("ceil", 127))
    six_ticks = tpb / 4
    max_shift = float(t.get("max_shift_frac16", 0.2)) * six_ticks

    tempo_points = sorted(tempo_map) if tempo_map else [(0, float(bpm))]
    tempo_ticks = [tick for tick, _bpm in tempo_points]
    tempo_bpms = [float(b) for _tick, b in tempo_points]
    ref_bpm = float(t.get("tempo_ref_bpm", 90.0))
    tempo_exponent = float(t.get("tempo_exponent", 0.5))

    def bpm_at(tick):
        return tempo_bpms[max(0, bisect.bisect_right(tempo_ticks, tick) - 1)]

    # быстрые треки: барабанщик собирается
    base_tempo_k = (ref_bpm / tempo_bpms[0]) ** tempo_exponent

    stats = {"velocity": 0, "timing": 0, "ghosts": 0, "unknown_class": 0,
             "profile": profile_label(prof), "tempo_k": round(base_tempo_k, 3),
             "tempo_segments": len(tempo_points)}

    # ПОРЯДОК ВАЖЕН. Госты добавляются ПЕРВЫМИ, по ещё чистой сетке:
    #  - иначе счёт ударов снейра в такте считается по уже сдвинутым тикам,
    #    ноты у тактовой черты перескакивают в соседний такт, и фильтр филлов
    #    врёт (ловилось на тактах 41/49);
    #  - и, главное, добавленные после тайминга госты остались бы стоять
    #    РОВНО на сетке — то есть мы бы сами воткнули в оживлённый трек сотню
    #    механических нот.
    # Сдвиг ниже применяется ко всем нотам, включая гостов; velocity гостам
    # не трогаем — у них свой уровень.
    # Осознанно пустые такты (оркестровые вставки, сухие куплеты) обязаны
    # остаться пустыми: запоминаем их ДО любых сдвигов.
    grid = _BarGrid(tpb, bar_starts)
    used_bars = {grid.index(n["tick"]) for n in notes}
    last_bar = max(used_bars) if used_bars else 0
    empty_bars = {b for b in range(last_bar + 1) if b not in used_bars}

    g = dict(prof.get("ghost_notes") or {})
    if ghost_override is not None:
        g["enabled"] = bool(ghost_override)
    if g.get("enabled"):
        stats["ghosts"] = _add_ghost_notes(notes, tpb, g, rng, grid)

    original_bars = {id(n): grid.index(n["tick"]) for n in notes}
    for n in notes:
        cls = DRUM_CLASS.get(n["note"])
        if cls is None:
            stats["unknown_class"] += 1
            continue
        mc = metrical_class(n["tick"], tpb)

        vc = v["classes"].get(cls)
        if vc and not n.get("_ghost"):
            val = (ref + float(vc.get("level", 0))
                   + float((vc.get("accent") or {}).get(mc, 0))
                   + rng.gauss(0.0, float(vc.get("std", 0))))
            n["vel"] = max(floor, min(ceil, int(round(val))))
            stats["velocity"] += 1

        tc = t["classes"].get(cls)
        if tc:
            note_bpm = bpm_at(n["tick"])
            tempo_k = (ref_bpm / note_bpm) ** tempo_exponent
            ms_per_tick = 60000.0 / (note_bpm * tpb)
            shift_ms = rng.gauss(float(tc.get("bias_ms", 0)),
                                 float(tc.get("std_ms", 0)) * tempo_k)
            shift = shift_ms / ms_per_tick
            shift = max(-max_shift, min(max_shift, shift))
            n["tick"] = max(0, int(round(n["tick"] + shift)))
            stats["timing"] += 1

    # Микро-сдвиг мог перебросить ноту через тактовую черту в пустой такт.
    # Сохраняем музыкальный такт исходной атаки: раннюю ноту прижимаем к его
    # началу, позднюю — к его концу. Иначе ранняя атака уезжала ещё на целый
    # такт назад (например, начало такта 5 оказывалось в конце такта 3).
    pulled = 0
    for n in notes:
        current_bar = grid.index(n["tick"])
        if current_bar in empty_bars:
            original_bar = original_bars[id(n)]
            if current_bar < original_bar:
                n["tick"] = grid.start(original_bar)
            else:
                n["tick"] = grid.end(original_bar) - 1
            pulled += 1
    stats["pulled_from_silence"] = pulled

    return stats


# --------------------------------------------------------------------------- #
#  Струнно-щипковые (GUITAR/BASS). Не пост-проход: вызывается ИЗ цикла нот, потому что от сдвинутого
#  тика должны наследоваться keyswitch, pitch bend, лиги и легато.
# --------------------------------------------------------------------------- #
def pitched_beat_shift(start_tick, bpm, tpb, prof, rng):
    """
    Сдвиг ВСЕГО бита (общий для его нот). Возвращает новый start_tick.

    Общий, а не по-нотный, потому что от start_tick считаются лиги
    (rec["tick"] = start_tick + dur) и легато-перекрытие: разъедься ноты бита
    независимо — перекрытие порвётся. Разброс внутри аккорда даёт strum,
    у него своя, направленная логика.
    """
    t = prof.get("timing") or {}
    six = tpb / 4
    ms_per_tick = 60000.0 / (bpm * tpb)
    tempo_k = (float(t.get("tempo_ref_bpm", 90.0)) / bpm) ** float(t.get("tempo_exponent", 0.5))
    shift_ms = rng.gauss(float(t.get("bias_ms", 0.0)), float(t.get("std_ms", 7.0)) * tempo_k)
    shift = shift_ms / ms_per_tick
    limit = float(t.get("max_shift_frac16", 0.12)) * six
    shift = max(-limit, min(limit, shift))
    return max(0, int(round(start_tick + shift)))


FRET_HAND_COST_KEYS = (
    "in_position_ms_per_fret", "shift_ms", "saturation_ms", "saturation_frets",
    "max_delay_frac16", "max_delay_note_frac",
)


def _validate_fret_hand_cost(section, path):
    # Без дефолтов в коде: у каждого числа модели одно место жительства —
    # профиль. Пропущенный ключ падает здесь, с именем файла, а не посреди
    # рендера безымянным KeyError.
    if not isinstance(section, dict):
        raise ValueError(f"В профиле {path}: секция 'fret_hand_cost' не является отображением")
    missing = [key for key in FRET_HAND_COST_KEYS if key not in section]
    if missing:
        raise ValueError(f"В профиле {path}: в 'fret_hand_cost' нет ключей {missing}")
    values = {key: float(section[key]) for key in FRET_HAND_COST_KEYS}
    if min(values.values()) < 0 or values["saturation_frets"] <= 0:
        raise ValueError(f"В профиле {path}: 'fret_hand_cost' содержит недопустимые значения")
    if not values["shift_ms"] <= values["saturation_ms"]:
        raise ValueError(f"В профиле {path}: fret_hand_cost.shift_ms > saturation_ms")


def fret_hand_cost_ms(delta_frets, shift_frets, prof):
    """Время переноса левой руки на |delta_frets| ладов, мс.

    В пределах позиции (delta < shift_frets, порог берётся из articulation_maps:
    тот же min_fret_shift, что у fret_noise) пальцы достают ноту без переноса
    кисти — стоимость мала и линейна. Смена позиции — другое движение: сразу
    shift_ms, дальше рост с насыщением к saturation_ms (рука не тормозит
    бесконечно, прыжок на октаву стоит немногим дороже прыжка на пять ладов).
    Время физическое, поэтому в мс и без темповой поправки.
    """
    h = prof["fret_hand_cost"]
    if delta_frets <= 0:
        return 0.0
    if delta_frets < shift_frets:
        return float(h["in_position_ms_per_fret"]) * delta_frets
    base, ceiling = float(h["shift_ms"]), float(h["saturation_ms"])
    over = delta_frets - shift_frets
    return ceiling - (ceiling - base) * math.exp(-over / float(h["saturation_frets"]))


def fret_hand_delay(delta_frets, shift_frets, gap_ms, dur_ticks, bpm, tpb, prof):
    """Задержка атаки бита из-за переноса руки, в тиках, всегда >= 0.

    Переносу засчитывается тишина перед атакой (gap_ms): рука, отпустившая
    струну заранее, успевает переехать и не опаздывает. Потолок — меньшее из
    max_delay_frac16 шестнадцатой и max_delay_note_frac собственной длительности
    бита, чтобы задержанная нота не съела саму себя.
    """
    h = prof["fret_hand_cost"]
    cost_ms = max(0.0, fret_hand_cost_ms(delta_frets, shift_frets, prof) - gap_ms)
    ms_per_tick = 60000.0 / (bpm * tpb)
    limit = min(float(h["max_delay_frac16"]) * tpb / 4,
                float(h["max_delay_note_frac"]) * dur_ticks)
    return max(0, int(round(min(cost_ms / ms_per_tick, limit))))


def pitched_strum_offsets(pitches, start_tick, bpm, tpb, prof, rng):
    """
    Разброс нот аккорда по струнам -> {pitch: сдвиг в тиках}.

    Ноты аккорда не одновременны: медиатор идёт по струнам за 10-25 мс.
    Направление чередуется (вниз на доле, вверх между) — как у живой руки.
    Сдвиг ТОЛЬКО ВПЕРЁД: назад увело бы ноту за её же keyswitch (KS_LEAD_MS=15).
    """
    s = prof.get("strum") or {}
    if not s.get("enabled") or len(pitches) < 2:
        return {}, True
    ms_per_tick = 60000.0 / (bpm * tpb)
    per = float(s.get("ms_per_string", 5.0))
    cap = float(s.get("max_spread_ms", 22.0))

    # Направление: вниз на ЧЁТНОЙ шестнадцатой, вверх на нечётной.
    # Первая версия смотрела на metrical_class ("доля -> вниз, иначе вверх") —
    # это неверно для шестнадцатых: подряд шли три удара вверх, чего рука не
    # делает. Чётность 16-й даёт правильное и там, и там: партия восьмыми
    # попадает только на чётные, то есть играется СПЛОШЬ ВНИЗ (классический
    # метал-даунпикинг), а шестнадцатые честно чередуются вниз-вверх.
    down = True
    if s.get("alternate", True):
        down = sixteenth_index(start_tick, tpb) % 2 == 0

    # вниз = от низкой струны к высокой, вверх = наоборот
    order = sorted(pitches) if down else sorted(pitches, reverse=True)
    if not down:
        per *= float(s.get("upstroke_spread_k", 0.8))   # ход вверх короче

    span = min(per * (len(order) - 1), cap)
    step = span / max(1, len(order) - 1)
    return {p: int(round(i * step / ms_per_tick)) for i, p in enumerate(order)}, down


def pitched_velocity(velocity, start_tick, tpb, articulation, prof, rng,
                    vel_cap=None, down=True):
    """
    Акцент по метрической позиции + артикуляции + ход медиатора + разброс.

    vel_cap — потолок ИЗ КАРТЫ АРТИКУЛЯЦИЙ (sustain.vel_max=119 у Hydra), а не
    из профиля: выше него начинаются vel-зоны rake (120-126) и pinch (127), то
    есть перебор даст не громкость, а посторонний звук. Клампим ЖЁСТКО.
    """
    v = prof.get("velocity") or {}
    val = float(velocity)
    val += float((v.get("accent") or {}).get(metrical_class(start_tick, tpb), 0))
    if articulation:
        val += float((v.get("articulation") or {}).get(articulation, 0))
    if not down:
        # ход вверх у живой руки слабее хода вниз — гравитация и замах
        val += float((prof.get("strum") or {}).get("upstroke_vel", -4))
    val += rng.gauss(0.0, float(v.get("std", 4)))
    lo = int(v.get("floor", 20))
    hi = int(vel_cap) if vel_cap is not None else 127
    return max(lo, min(hi, int(round(val))))


def _add_ghost_notes(notes, tpb, g, rng, grid=None):
    """
    Досочиняет тихие гост-ноты по рабочему ВОКРУГ БЭКБИТА.

    Единственное место, где партия МЕНЯЕТСЯ, а не только подаётся иначе,
    поэтому осторожность здесь важнее «живости»:
      - только в тактах, где снейр уже играет (не оживляем тишину и сухие
        секции вроде кросс-стик куплета);
      - не в ролловых филлах (skip_bar_if_snare_hits_gte);
      - позиции — только 16-е, соседние с РЕАЛЬНЫМ ударом снейра, и только
        слабые; равномерная россыпь по такту звучит как раздолбай (проверено
        на слух: доля перестаёт читаться);
      - не ближе min_gap_frac16 к существующему удару — иначе это флэм, не гост;
      - не более max_per_bar за такт;
      - кандидат за пределами такта отбрасывается.

    grid — реальные границы тактов (_BarGrid); без него — сетка 4/4.
    """
    note_id = int(g.get("note", 38))
    vmin = int((g.get("velocity") or {}).get("min", 28))
    vmax = int((g.get("velocity") or {}).get("max", 46))
    # зазор в долях шестнадцатой: tpb у разных вызывающих разный (960 в
    # конвертере, 480 в standalone), абсолютные тики означали бы разное
    six = tpb / 4
    min_gap = float(g.get("min_gap_frac16", 1.0)) * six
    p_before = float(g.get("prob_before", 0.45))
    p_after = float(g.get("prob_after", 0.20))
    max_per_bar = int(g.get("max_per_bar", 2))
    dense_gte = int(g.get("skip_bar_if_snare_hits_gte", 5))
    grid = grid or _BarGrid(tpb)

    by_bar = {}
    for n in notes:
        by_bar.setdefault(grid.index(n["tick"]), []).append(n)

    added = []
    for bar, bar_notes in by_bar.items():
        snare = [n for n in bar_notes if n["note"] == note_id]
        if g.get("require_backbeat", True) and not snare:
            continue
        if dense_gte and len(snare) >= dense_gte:
            continue    # это ролловый филл, а не грув — госты сделают из него кашу

        bar_start = grid.start(bar)
        # шестнадцатых в ЭТОМ такте: 16 в 4/4, 12 в 3/4 и 6/8
        bar_sixteenths = max(1, int(round((grid.end(bar) - bar_start) / six)))
        occupied = [n["tick"] for n in bar_notes if n["note"] == note_id]
        cands = []
        for s in snare:
            p = int(round((s["tick"] - bar_start) / six))
            for delta, prob in ((-1, p_before), (+1, p_after)):
                q = p + delta
                if q < 0 or q >= bar_sixteenths:
                    continue        # за тактовую черту не вылезаем
                if q % 2 == 0:
                    continue        # гост живёт только на слабой шестнадцатой
                cands.append((q, prob))
        rng.shuffle(cands)

        n_added = 0
        for q, prob in cands:
            if n_added >= max_per_bar:
                break
            if rng.random() > prob:
                continue
            tick = int(bar_start + q * six)
            if any(abs(tick - o) < min_gap for o in occupied):
                continue
            added.append({"tick": tick, "note": note_id,
                          "vel": rng.randint(vmin, vmax),
                          "dur": max(1, int(tpb / 8)), "_ghost": True})
            occupied.append(tick)
            n_added += 1
    notes.extend(added)
    return len(added)
