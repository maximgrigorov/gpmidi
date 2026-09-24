# -*- coding: utf-8 -*-
"""Смок-проверка экспортированного MIDI: "не насрали ли мы в мидюху".

Это НЕ музыкальная экспертиза и не тесты конвертера — это быстрая проверка
инвариантов на готовом артефакте. Каждая проверка здесь — окаменевший баг,
который реально случился, а не гипотетический:

  BEND_CEILING   бенды на 4 и 6 полутонов упирались в потолок 8191 при шкале 2:
                 подъём -> ПОЛКА -> скачок. Это и были "лесенки", которые
                 приходилось дорисовывать руками в Logic. 14 событий в потолке.
  CC1_PROPELLER  в CC1 слалась синусоида 5.5 Гц, хотя CC1 у Shreddage — это
                 ручка Vibrato Amount (ГЛУБИНА), а не форма волны. 206 смен
                 направления на партию: ручка вращалась как пропеллер.
  KS_LEAD        keyswitch приходит за 15 мс до ноты. Микро-сдвиг тайминга мог
                 увести ноту НАЗАД за её собственный KS -> артикуляция
                 срабатывала не на ту ноту.
  VEL_ZONE       у Hydra на sustain velocity 120-126 = Rake, 127 = Pinch. Это
                 переключатель артикуляции, а не громкость: перебор velocity
                 даёт не "громче", а посторонний визг.
  RIGID          "фальшивая гуманизация": группа нот, уехавшая на ОДИН и тот же
                 сдвиг, формально вне сетки, но друг относительно друга
                 квантована намертво. Агрегатный off-grid% при этом врёт, что
                 всё хорошо (ловилось дважды: у пака грувов и у нас самих).
  STUCK          note_on без note_off = висящая нота = бесконечный гул.
  PB_LEAK        pitch bend не сброшен в 0 к атаке следующей ноты -> она звучит
                 расстроенной.
  DRUM_UNMAPPED  барабанная нота вне kit_layout -> в Kontakt тишина.
  KEY_RETRIGGER  нота атакует клавишу, которая ещё звучит: note_off прежней
                 ноты придёт позже и погасит НОВУЮ (сэмплер отпускает клавишу, а
                 не «свою» ноту). В списке нот всё на месте, в звуке — дыра.
                 --humanize так терял 1909 нот гитар и баса (двухсекундная B3 в
                 соло Through the Night), авторские сдвиги GP — ещё 216.
                 Барабаны не проверяются: их звук однократный. ERROR на всех
                 тональных дорожках, включая OTHER (клавиши, синты, вокал).

Использование:
    python verify_midi.py song_midi/                     # все .mid в папке
    python verify_midi.py song_midi/Drumkit.mid --type DRUMS
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

import mido

from articulation_config import config_for_track_type

# Порог: сдвиг ноты относительно ближайшего узла объединённой сетки
# (16-я + триоль). std ниже этого = группа нот стоит намертво.
RIGID_STD_TICKS = 1.0
RIGID_MIN_NOTES = 8
# CC1: столько смен направления на одну ноту с вибрато — уже пропеллер.
# Здоровая огибающая даёт 2 (подъём + спад).
CC1_REVERSALS_PER_NOTE = 4


def _load(path):
    mid = mido.MidiFile(path)
    tpb = mid.ticks_per_beat
    tempo = 500000
    events = []
    for track in mid.tracks:
        ab = 0
        for msg in track:
            ab += msg.time
            if msg.type == "set_tempo":
                tempo = msg.tempo
            events.append((ab, msg))
    events.sort(key=lambda e: e[0])
    return mid, tpb, tempo, events


def smoke_check(path, track_type=None, cfg=None):
    """Возвращает список (severity, code, message). Пустой список = чисто."""
    out = []
    try:
        mid, tpb, tempo, events = _load(path)
    except Exception as e:
        return [("ERROR", "UNREADABLE", f"файл не читается: {e}")]

    if cfg is None and track_type:
        try:
            cfg = config_for_track_type(track_type)
        except Exception:
            cfg = None

    ks_notes = set()
    if cfg:
        for spec in (cfg.get("keyswitches") or {}).values():
            if isinstance(spec, dict) and "note" in spec:
                ks_notes.add(int(spec["note"]))
        for n in (cfg.get("fx_keyswitches") or {}).values():
            if isinstance(n, int):
                ks_notes.add(n)

    notes = []          # мелодические/барабанные ноты
    ks_hits = []        # keyswitch-импульсы
    active = defaultdict(list)
    stuck = 0
    retriggers = 0      # атака клавиши, которая ещё звучит с более раннего тика
    for event_index, (tick, msg) in enumerate(events):
        if msg.type == "note_on" and msg.velocity > 0:
            if msg.note in ks_notes:
                ks_hits.append((tick, msg.note, event_index))
            else:
                if any(t0 < tick for t0, _v, _i in active[msg.note]):
                    retriggers += 1
                active[msg.note].append((tick, msg.velocity, event_index))
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            if active[msg.note]:
                t0, v, on_index = active[msg.note].pop(0)
                notes.append({
                    "tick": t0, "note": msg.note, "vel": v,
                    "dur": tick - t0, "event_index": on_index,
                })
    stuck = sum(len(v) for v in active.values())

    if not notes:
        return [("WARN", "EMPTY", "в файле нет нот")]
    notes.sort(key=lambda n: n["tick"])

    if stuck:
        out.append(("ERROR", "STUCK", f"{stuck} нот без note_off — будут гудеть бесконечно"))

    if retriggers and track_type != "DRUMS":
        # Исправлено в экспортёре для всех тональных дорожек — обязано быть нулём.
        out.append(("ERROR", "KEY_RETRIGGER",
                    f"{retriggers} нот атакуют клавишу, которая ещё звучит: note_off "
                    f"прежней ноты погасит новую — в звуке будет дыра"))

    # --- pitch bend: потолок и утечка ---
    pw = [(t, m.pitch) for t, m in events if m.type == "pitchwheel"]
    ceil_hits = [t for t, p in pw if p >= 8191 or p <= -8192]
    if ceil_hits:
        out.append(("WARN", "BEND_CEILING",
                    f"{len(ceil_hits)} pitch bend-событий упёрлись в потолок: "
                    f"бенд обрезан, звучит как подъём + ПОЛКА + скачок — это "
                    f"'лесенка'. Шкала мала для глубины бенда в партитуре "
                    f"(в конфиге сейчас pitch_bend_range={_pbr(cfg)}; оно обязано "
                    f"совпадать с ручкой PITCH BEND RANGE в инструменте)"))
    if pw:
        # Утечку бенда НЕЛЬЗЯ ловить просто по "PB != 0 на атаке": ровно так же
        # выглядит ПРЕД-БЕНД (гитарист подтягивает струну до щипка) — у него
        # первая точка кривой стоит на тике самой ноты и уже ненулевая.
        # Первая версия этой проверки на этом и обожглась: 4 "утечки" в Solo
        # Guitar оказались законными пред-бендами (PB +2341 = +2.00 полутона
        # ровно на тике ноты, дальше кривая росла).
        # Утечка — это УСТАРЕВШЕЕ значение: последнее PB-событие далеко позади,
        # своей кривой у ноты нет, а строй уже уехал.
        stale_window = tpb / 4        # шестнадцатая
        last_val, last_tick = 0, -10 ** 9
        leaks = 0
        pi = 0
        for n in notes:
            while pi < len(pw) and pw[pi][0] <= n["tick"]:
                last_val, last_tick = pw[pi][1], pw[pi][0]
                pi += 1
            if abs(last_val) > 200 and (n["tick"] - last_tick) > stale_window:
                leaks += 1
        if leaks:
            out.append(("WARN", "PB_LEAK",
                        f"{leaks} нот атакуются при устаревшем ненулевом pitch bend "
                        f"(последняя точка кривой давно позади) — прозвучат "
                        f"расстроенными"))

    # --- CC1: пропеллер ---
    cc1 = [(t, m.value) for t, m in events
           if m.type == "control_change" and m.control == 1]
    if len(cc1) > 4:
        # Считаем развороты ВНУТРИ КАЖДОЙ НОТЫ, а не глобально по треку.
        # Две предыдущие версии этой проверки молчали на файле, ради которого
        # написаны: глобальные 206 разворотов делились то на все ноты длиннее
        # 16-й, то на все ноты, пересекающиеся с любым CC1-событием (а их 1638
        # на трек, то есть пересекаются почти все). Осмысленна только локальная
        # величина: сколько раз качнулась глубина В ПРЕДЕЛАХ ОДНОЙ ноты.
        # Здоровая огибающая (нарастание -> удержание -> спад) даёт 1 разворот.
        worst = 0
        bad_notes = 0
        for n in notes:
            lo, hi = n["tick"], n["tick"] + n["dur"]
            vals = [v for t, v in cc1 if lo <= t <= hi]
            if len(vals) < 4:
                continue
            d = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
            r = sum(1 for i in range(len(d) - 1) if d[i] * d[i + 1] < 0)
            worst = max(worst, r)
            if r >= CC1_REVERSALS_PER_NOTE:
                bad_notes += 1
        if bad_notes:
            out.append(("WARN", "CC1_PROPELLER",
                        f"на {bad_notes} нотах CC1 качается внутри одной ноты "
                        f"(до {worst} разворотов; здоровая огибающая даёт 1). "
                        f"CC1 у Shreddage — это ГЛУБИНА вибрато, а не форма волны: "
                        f"похоже, шлём качалку вместо огибающей"))

    # --- keyswitch: лид ---
    if ks_hits:
        bad = 0
        min_lead = None
        first_music = min(notes, key=lambda n: (n["tick"], n["event_index"]))
        for kt, _note, ks_index in ks_hits:
            nxt = next((n for n in notes if n["tick"] >= kt), None)
            if nxt is None:
                continue
            lead = nxt["tick"] - kt
            min_lead = lead if min_lead is None else min(min_lead, lead)
            # Project start has no negative tick. An explicit initial state at
            # tick 0 is valid when its MIDI event precedes the first musical
            # note at the same tick; Kontakt receives the KS first. Same-tick
            # articulation changes anywhere else remain invalid.
            initial_same_tick = (
                kt == 0
                and first_music["tick"] == 0
                and ks_index < first_music["event_index"]
            )
            if lead <= 0 and not initial_same_tick:
                bad += 1
        if bad:
            out.append(("ERROR", "KS_LEAD",
                        f"{bad} keyswitch-ей не раньше своей ноты — артикуляция "
                        f"сработает не на ту ноту"))

    # --- keyswitch: установлено ли НАЧАЛЬНОЕ состояние ---
    # Kontakt не сбрасывает артикуляцию между проигрываниями. Если первый
    # keyswitch приходит сильно позже первой ноты, начало трека звучит тем, что
    # осталось включённым от прошлого раза (ловилось живьём: Solo Guitar с
    # нотами от 8-го такта и первым KS в 58-м играл первое соло гармониками).
    if ks_notes and notes:
        first_note = notes[0]["tick"]
        first_ks = min((t for t, _note, _index in ks_hits), default=None)
        if first_ks is None or first_ks > first_note:
            out.append(("ERROR", "KS_NO_INIT",
                        "начальная артикуляция не установлена: первая нота на тике "
                        f"{first_note}, а keyswitch-а до неё нет. Инструмент сыграет "
                        f"тем, что осталось включённым от прошлого проигрывания"))

    # --- velocity-зоны (только там, где они есть: Hydra sustain) ---
    # Зоны существуют ТОЛЬКО на sustain-KS: на palm mute и прочих артикуляциях
    # экспортёр легально разрешает 120-127 (vel_cap=127). Проверка без учёта
    # активной артикуляции давала ложный ERROR на корректном артефакте:
    # palm-muted FF-нота (111) с обычным акцентом (+12) уезжает в 123.
    zones = _vel_zones(cfg)
    if zones:
        lo, hi, name = zones
        sustain_note = int(((cfg.get("keyswitches") or {}).get("sustain") or {}).get("note", -1))
        ordered_ks = sorted(ks_hits, key=lambda hit: (hit[0], hit[2]))
        ordered_notes = sorted(notes, key=lambda n: (n["tick"], n["event_index"]))
        hits = []
        ks_index = 0
        active_ks = None
        for n in ordered_notes:
            while (ks_index < len(ordered_ks)
                   and (ordered_ks[ks_index][0], ordered_ks[ks_index][2])
                   < (n["tick"], n["event_index"])):
                active_ks = ordered_ks[ks_index][1]
                ks_index += 1
            if active_ks == sustain_note and lo <= n["vel"] <= hi:
                hits.append(n)
        if hits:
            out.append(("ERROR", "VEL_ZONE",
                        f"{len(hits)} нот атакованы на sustain с velocity в зоне "
                        f"{lo}-{hi} ({name}): инструмент сыграет не ноту, а {name}"))

    # --- барабаны: ноты вне раскладки кита ---
    if track_type == "DRUMS" and cfg:
        kit = set((cfg.get("kit_layout") or {}).keys())
        flams = cfg.get("flams") or {}
        offset = int(flams.get("offset", 24))
        toms = set(flams.get("toms") or [])
        kit |= {t + offset for t in toms}
        if kit:
            bad = sorted({n["note"] for n in notes if n["note"] not in kit})
            if bad:
                out.append(("WARN", "DRUM_UNMAPPED",
                            f"ноты вне раскладки кита: {bad} — в Kontakt промолчат"))

    # --- жёсткая квантованность относительно друг друга ---
    grid = tpb / 12.0          # общий делитель 16-й и триоли
    per_pitch = defaultdict(list)
    for n in notes:
        d = n["tick"] % grid
        per_pitch[n["note"]].append(d - grid if d > grid / 2 else d)
    rigid = []
    for p, ds in per_pitch.items():
        if len(ds) < RIGID_MIN_NOTES:
            continue
        m = sum(ds) / len(ds)
        sd = (sum((x - m) ** 2 for x in ds) / len(ds)) ** 0.5
        if sd < RIGID_STD_TICKS:
            rigid.append((p, len(ds)))
    if rigid:
        det = ", ".join(f"{p}({c})" for p, c in sorted(rigid))
        out.append(("INFO", "RIGID",
                    f"питчи стоят намертво относительно друг друга: {det}. "
                    f"Норма для неоживлённого экспорта; в оживлённом = баг"))

    return out


def _pbr(cfg):
    return (cfg or {}).get("pitch_bend_range", "?")


def _vel_zones(cfg):
    """(lo, hi, имя) первой vel-зоны, если у инструмента они есть."""
    if not cfg:
        return None
    ks = cfg.get("keyswitches") or {}
    sus = ks.get("sustain") or {}
    cap = sus.get("vel_max")
    if cap is None:
        return None                      # у Darkwall зон нет вовсе
    for name in ("rake", "pinch_harmonics"):
        spec = ks.get(name) or {}
        lo = spec.get("vel_min", spec.get("velocity"))
        if lo is not None and int(lo) > int(cap):
            hi = int(spec.get("vel_max", spec.get("velocity", 127)))
            return int(lo), hi, name
    return None


def report(path, track_type=None, cfg=None, prefix="  "):
    """Печатает находки. Возвращает True, если ошибок нет."""
    found = smoke_check(path, track_type, cfg)
    errs = [f for f in found if f[0] == "ERROR"]
    for sev, code, msg in found:
        mark = {"ERROR": "!!", "WARN": " !", "INFO": "  "}[sev]
        print(f"{prefix}{mark} [{code}] {msg}")
    return not errs


def main(argv):
    ap = argparse.ArgumentParser(description="Смок-проверка экспортированного MIDI")
    ap.add_argument("paths", nargs="+", help="файлы .mid или папки")
    ap.add_argument("--type", dest="track_type", default=None,
                    choices=["GUITAR", "BASS", "DRUMS", "OTHER"],
                    help="тип трека (иначе — угадывается по имени файла)")
    a = ap.parse_args(argv[1:])

    files = []
    for p in a.paths:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p))
                      if f.lower().endswith(".mid")]
        else:
            files.append(p)

    ok = True
    for f in files:
        tt = a.track_type
        if tt is None:
            from gp_to_shreddage import detect_track_type
            base = os.path.splitext(os.path.basename(f))[0]
            tt = "DRUMS" if "drum" in base.lower() or "percussion" in base.lower() \
                else detect_track_type(base)
        print(f"{os.path.basename(f)}  [{tt}]")
        if not report(f, tt):
            ok = False
    print("\nИтог:", "чисто" if ok else "ЕСТЬ ОШИБКИ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
