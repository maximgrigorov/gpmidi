#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gp_to_shreddage.py

Конвертер Guitar Pro (.gp5 / .gp) -> отдельные MIDI-файлы на каждый трек,
заточенный под библиотеки Shreddage 3.5 (Hydra для гитар, Darkwall для баса).

Зависимости:
    pip install pyguitarpro mido

Запуск:
    python gp_to_shreddage.py song.gp5 [--humanize] [--ghost-notes] [--seed=N]
                                       [--auto-sustain-vibrato]
                                       [--fret-noise-on-hand-shift] [--no-verify]

Результат: рядом с исходным файлом создаётся папка "<имя_файла>_midi/"
с одним MIDI-файлом (Type 0) на каждый трек (имя файла = название трека)
ПЛЮС сборный "<имя_файла>_ALL.mid" (Type 1) со всеми дорожками разом.

    --humanize     синтезировать velocity и микро-тайминг по профилю на
                   инструмент (config/humanize_profiles/*.yaml). GP отдаёт 8
                   градаций динамики, а для перкуссии одну, — переносить нечего,
                   поэтому синтез. Без флага выхлоп прежний, байт-в-байт.
    --ghost-notes  добавить гост-ноты по рабочему. ЕДИНСТВЕННАЯ опция, которая
                   МЕНЯЕТ ПАРТИЮ, а не только подачу. Только барабаны.
    --no-verify    не гонять смок-проверку выхлопа (verify_midi.py)

ВАЖНО: оживление обязано применяться ПОСЛЕ Guitar Pro и ДО сэмплера. Нотация не
умеет ни микро-тайминг, ни velocity тоньше 8 градаций, поэтому импорт результата
обратно в GP схлопнет его. См. LESSONS.md.

ОПРЕДЕЛЕНИЕ ТИПА ТРЕКА по track.name (case-insensitive):
    GUITAR -> маппинг Shreddage 3.5 Hydra   (ключевые слова GUITAR_KEYWORDS)
    BASS   -> маппинг Shreddage 3.5 Darkwall (ключевые слова BASS_KEYWORDS)
    OTHER  -> ноты копируются as is (без KS / CC1 / PB), только если имя трека
              не содержит ни одного ключевого слова. Тип НЕ угадывается.

Обе библиотеки используют TACT-систему кейсвитчей в Kontakt-нотации (C-1 = MIDI 12).
"""

import logging
import math
import os
import random
import sys

try:
    import guitarpro
except ImportError:
    sys.exit("Не найден модуль pyguitarpro. Установите: pip install pyguitarpro")

try:
    import mido
    from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo
except ImportError:
    sys.exit("Не найден модуль mido. Установите: pip install mido")

from guitarpro.models import SlideType, NoteType

from articulation_config import (
    articulation_priority,
    config_for_track_type,
    config_label,
    drum_note_out,
    keyswitch_note,
    log_config_used,
)
from verify_midi import smoke_check
from humanize import (
    pitched_beat_shift,
    pitched_strum_offsets,
    pitched_velocity,
    humanize_drums,
    profile_for_track_type,
    profile_label,
)

logger = logging.getLogger("gpmidi.convert")


# --------------------------------------------------------------------------- #
#  Определение типа трека
# --------------------------------------------------------------------------- #
TRACK_GUITAR = "GUITAR"
TRACK_BASS = "BASS"
TRACK_DRUMS = "DRUMS"
TRACK_OTHER = "OTHER"

# Ключевые слова в названии трека (case-insensitive). Проверяется СНАЧАЛА бас
# (т.к. "bass guitar" — это бас), затем гитара.
BASS_KEYWORDS = ["bass", "бас"]
GUITAR_KEYWORDS = ["guitar", "gtr", "lead", "solo", "rhythm", "acoustic", "steel"]

# Стоп-слова: если имя трека содержит любое из них -> тип OTHER, независимо от
# GUITAR/BASS-ключей (напр. "Lead Vocals" не должен стать гитарой из-за "lead").
# Проверяются ПЕРВЫМИ.
STOP_WORDS = [
    "vocal", "vox", "voice", "choir", "sing",
    "голос", "вокал",
    "drum", "kick", "snare", "hihat", "cymbal", "perc",
    "piano", "keys", "organ", "synth", "pad", "string",
    "brass", "horn", "wind", "flute",
    "contrabass", "contra", "upright",
]


def detect_track_type(name):
    """Тип трека по ключевым словам. Нет совпадений -> OTHER (не угадываем)."""
    low = (name or "").lower()
    for sw in STOP_WORDS:
        if sw in low:
            return TRACK_OTHER
    for kw in BASS_KEYWORDS:
        if kw in low:
            return TRACK_BASS
    for kw in GUITAR_KEYWORDS:
        if kw in low:
            return TRACK_GUITAR
    return TRACK_OTHER


def resolve_track_type(track):
    """Тип трека по объекту трека: перкуссия -> DRUMS (по флагу из парсера,
    надёжнее ключевых слов), иначе — по имени (detect_track_type)."""
    if getattr(track, "isPercussion", False) or getattr(track, "isPercussionTrack", False):
        return TRACK_DRUMS
    return detect_track_type(getattr(track, "name", ""))


# --------------------------------------------------------------------------- #
#  Константы конвертации
# --------------------------------------------------------------------------- #
TICKS_PER_BEAT = 960            # GP по умолчанию: 960 тиков на четверть
START_TICK = 960               # GP начинает первый такт с тика 960 -> нормализуем
GPIF_PLAYED_PPQ = 480          # Offset в GP8 score.gpif использует 480 PPQ
HIDDEN_32ND_GROUP_TOLERANCE = 12
HIDDEN_32ND_GAP_MIN = 45
HIDDEN_32ND_GAP_MAX = 75

# --- Бенды / слайды ---
# Дефолт мануалов Shreddage 3.5 (Hydra и Darkwall): +/- 2 полутона (НЕ 6!).
# Значение per-instrument живёт в config/articulation_maps/*.yaml
# (pitch_bend_range); эта константа — только фолбэк для треков без конфига.
PITCH_BEND_RANGE_ST = 2.0
PITCH_BEND_MAX = 8191           # MIDI pitch bend: -8192 .. +8191
PITCH_BEND_MIN = -8192
# В pyguitarpro значение точки бенда нормализовано: value 4 == целый тон (2 полутона),
# т.е. 1 единица == четверть тона == 0.5 полутона.
SEMITONES_PER_BEND_UNIT = 0.5
BEND_STEP_MS = 10               # шаг кривой бенда
SLIDE_LEAD_MS = 50              # за сколько до конца ноты начинать слайд
SLIDE_BEND_ST = 2.0             # глубина слайда в полутонах

# --- Вибрато (для обоих инструментов) ---
# CC1 у Shreddage — это ручка VIBRATO AMOUNT, то есть ГЛУБИНА собственного
# вибрато инструмента, а НЕ форма волны: качает высоту сама Hydra.
# Режим "sine" (историческое поведение) слал в CC1 синусоиду 5.5 Гц и тем самым
# крутил ручку глубины 5.5 раз в секунду — 206 разворотов на партию Solo Guitar,
# ручка визуально вращалась как пропеллер, а наша качалка билась с внутренней.
# Режим "envelope": плавная огибающая (нарастание -> удержание -> спад),
# колебание отдано инструменту.
VIBRATO_MODE = "envelope"       # "envelope" | "sine" (откат к прежнему поведению)
VIBRATO_FREQ_HZ = 5.5           # только для режима "sine"
VIBRATO_DELAY_MS = 80           # вибрато стартует через 80 мс после атаки
VIBRATO_RAMP_MS = 120           # нарастание глубины (envelope)
VIBRATO_RELEASE_MS = 80         # спад глубины к концу ноты (envelope)
VIBRATO_STEP_MS = 10            # шаг кривой
VIBRATO_CC = 1                  # Mod Wheel

# Глубина по ТИПУ вибрато из партитуры. GP различает Slight и Wide, но
# ApolloTab отдаёт vibrato булевым — тип достаётся сайдкаром из GPIF
# (<Vibrato>Slight|Wide</Vibrato>, см. _extract_gpif_note_extras_root).
# Раньше всё игралось на 80: у пользователя ВСЕ 53 ноты помечены Slight, то есть
# написано "слегка", а звучало на две трети глубины — на слух дрожание.
VIBRATO_AMP_BY_TYPE = {"Slight": 32, "Wide": 80}
VIBRATO_AMP = 48                # фолбэк: тип неизвестен (GP3/4/5 без сайдкара)

# Порог длины ноты. Огибающая занимает DELAY+RAMP+RELEASE = 280 мс, поэтому на
# ноте короче этого вибрато вырождается в "вжик" и физически не успевает
# прозвучать (в партии 40 из 56 вибрато-нот были короче 350 мс при пороге 300).
# Ниже порога вибрато не шлём вовсе — чистый звук лучше огрызка.
VIBRATO_MIN_NOTE_MS = 450

# --- Легато (hammer-on / pull-off) ---
# GP ставит флаг hammer на ноту-ИСТОЧНИК (с которой начинается переход).
# Источник играется с нормальной атакой и продлевается ВНАХЛЁСТ в следующую
# ноту на той же струне: Shreddage (TACT) сам играет hammer/pull по
# перекрытию (Legato vel 1-127, авто по overlap). B15: раньше семантика была
# инвертирована — velocity=1 получал сам источник (нота пропадала), а
# продлевалась нота ПЕРЕД ним (дрон поверх пауз).
LEGATO_OVERLAP_MS = 40          # перекрытие источник->цель

# --- Кейсвитчи ---
# Ноты keyswitch-ей НЕ захардкожены: они живут в версионируемых конфигах
# config/articulation_maps/*.yaml (Hydra 3.5, Darkwall 3.5, Drums) и
# резолвятся через articulation_config. Здесь — только тайминг импульса.
KS_VELOCITY = 100
KS_LEN_MS = 5                   # длительность keyswitch-импульса
KS_LEAD_MS = 15                 # keyswitch приходит на 15 мс РАНЬШЕ ноты (для обоих)

# --- Акценты (Категория A, все типы треков) ---
ACCENT_VEL_BOOST = 12           # Accent (normal): velocity +12
HEAVY_ACCENT_VEL_BOOST = 24     # Heavy accent (marcato): velocity +24

# --- Staccato gate на не-Shreddage треках (Категория A) ---
STACCATO_GATE = 0.5             # укоротить ноту до 50% номинала

# --- Hairpin -> CC11 (Expression) на не-Shreddage треках (Категория A) ---
HAIRPIN_CC = 11
HAIRPIN_MAX_LOOKAHEAD_BEATS = 32   # искать целевую динамику не дальше ~8 тактов
HAIRPIN_FALLBACK_RATIO = 0.5       # цель не найдена: уйти к 50% за один такт
HAIRPIN_MIN_CC = 16
HAIRPIN_STEP_TICKS = 240           # шаг кривой CC11 (1/16 при 960 ppq)

CHANNEL = 0                     # один канал на файл

# Порядок событий в пределах одного тика (меньше = раньше)
ORDER_META = 0
ORDER_OFF = 1
ORDER_RESET = 2                 # сброс pitch/CC в конце ноты
ORDER_KS = 3                    # keyswitch перед нотой
ORDER_CTRL = 4                  # pitch/CC во время ноты
ORDER_ON = 5


# --------------------------------------------------------------------------- #
#  Хелперы
# --------------------------------------------------------------------------- #
def ms_to_ticks(ms, bpm):
    """Перевести миллисекунды в тики при данном темпе."""
    return int(round(ms / 1000.0 * bpm / 60.0 * TICKS_PER_BEAT))


def ticks_to_ms(ticks, bpm):
    """Перевести тики в миллисекунды при данном темпе."""
    return ticks / float(TICKS_PER_BEAT) * 60.0 / bpm * 1000.0


def semitones_to_pitchwheel(semitones, pb_range=PITCH_BEND_RANGE_ST):
    """Полутона -> 14-битное значение pitch bend в пределах диапазона.

    Бенды глубже pb_range физически непредставимы и обрезаются по краю
    диапазона (при дефолтных +/-2 st у Shreddage полный бенд GP = ровно край).
    """
    val = int(round(semitones / float(pb_range) * 8192))
    return max(PITCH_BEND_MIN, min(PITCH_BEND_MAX, val))


def emit_pitch_bend_range_rpn(ev, semitones, tick=0):
    """Стандартный MIDI RPN 0,0: установить Pitch Bend Range в полутонах.

    Это делает экспорт более предсказуемым в DAW/плагинах, которые уважают RPN.
    """
    semitones = max(0, min(24, int(round(semitones))))
    ev.add(tick, ORDER_META, Message("control_change", channel=CHANNEL, control=101, value=0))
    ev.add(tick, ORDER_META, Message("control_change", channel=CHANNEL, control=100, value=0))
    ev.add(tick, ORDER_META, Message("control_change", channel=CHANNEL, control=6, value=semitones))
    ev.add(tick, ORDER_META, Message("control_change", channel=CHANNEL, control=38, value=0))
    ev.add(tick, ORDER_META, Message("control_change", channel=CHANNEL, control=101, value=127))
    ev.add(tick, ORDER_META, Message("control_change", channel=CHANNEL, control=100, value=127))


def clamp_note(n):
    return max(0, min(127, int(n)))


def clamp_vel(v):
    return max(1, min(127, int(v)))


def clamp_cc(v):
    return max(0, min(127, int(v)))


def duration_ticks(duration):
    """Длительность бита в тиках (с учётом точки и триолей)."""
    try:
        return int(duration.time)
    except Exception:
        base = TICKS_PER_BEAT * 4.0 / duration.value
        if getattr(duration, "isDotted", False):
            base *= 1.5
        tup = getattr(duration, "tuplet", None)
        if tup is not None and getattr(tup, "enters", 1):
            base = base * tup.times / tup.enters
        return int(round(base))


def measure_length_ticks(measure):
    """Каноническая длина такта по time signature.

    Не доверяем measure.start из GP8/GPIF-импорта: там встречается дрейф
    старта тактов/битов. Для MIDI-экспорта считаем сетку сами.
    """
    ts = measure.header.timeSignature
    numerator = int(getattr(ts, "numerator", 4) or 4)
    denominator = int(getattr(getattr(ts, "denominator", None), "value", 4) or 4)
    return int(round(numerator * TICKS_PER_BEAT * 4.0 / denominator))


def iter_voice_beats_with_canonical_ticks(track):
    """Итерировать биты с каноническими start_tick/duration.

    Для GP5/GPX это совпадает с исходной сеткой. Для GP8/GPIF исправляет
    кейсы, где parser отдаёт некорректные measure.start/beat.start.
    """
    measure_tick = 0
    for measure in track.measures:
        ts = measure.header.timeSignature
        measure_start_tick = measure_tick
        for vi, voice in enumerate(measure.voices):
            beat_tick = measure_start_tick
            beats = voice.beats
            for bi, beat in enumerate(beats):
                dur = duration_ticks(beat.duration)
                yield measure, voice, vi, bi, beat, measure_start_tick, beat_tick, dur
                beat_tick += dur
        measure_tick += measure_length_ticks(measure)


def safe_filename(name):
    """Очистить название трека для использования в имени файла."""
    name = (name or "").strip()
    bad = '<>:"/\\|?*\0'
    for ch in bad:
        name = name.replace(ch, "_")
    return name.strip(". ") or ""


# --------------------------------------------------------------------------- #
#  Сборщик событий одного трека
# --------------------------------------------------------------------------- #
class EventList:
    """Накапливает (tick, order, message), затем выдаёт отсортированный трек."""

    def __init__(self):
        self.events = []

    def add(self, tick, order, msg):
        if tick < 0:
            tick = 0
        rec = {"tick": tick, "order": order, "msg": msg}
        self.events.append(rec)
        return rec

    def to_miditrack(self):
        self.events.sort(key=lambda e: (e["tick"], e["order"]))
        track = MidiTrack()
        prev = 0
        for e in self.events:
            delta = e["tick"] - prev
            if delta < 0:
                delta = 0
            msg = e["msg"].copy(time=delta)
            track.append(msg)
            prev = e["tick"]
        track.append(MetaMessage("end_of_track", time=0))
        return track


# --------------------------------------------------------------------------- #
#  Конвертация эффектов одной ноты (только для GUITAR/BASS)
# --------------------------------------------------------------------------- #
def emit_bend(ev, start_tick, dur_ticks, bend, bpm, pb_range=PITCH_BEND_RANGE_ST):
    """Плавная кривая Pitch Bend по точкам бенда. Шаг 10 мс. Сброс в 0 в конце."""
    points = sorted(bend.points, key=lambda p: p.position)
    if not points:
        return
    note_ms = ticks_to_ms(dur_ticks, bpm)

    def pos_to_ms(pos):
        return pos / 12.0 * note_ms

    def val_to_st(value):
        return value * SEMITONES_PER_BEND_UNIT

    first_ms = pos_to_ms(points[0].position)
    last_ms = pos_to_ms(points[-1].position)

    last_val = None
    ms = first_ms
    while ms <= last_ms + 1e-6:
        st = val_to_st(points[-1].value)
        for i in range(len(points) - 1):
            p0, p1 = points[i], points[i + 1]
            m0, m1 = pos_to_ms(p0.position), pos_to_ms(p1.position)
            if m0 <= ms <= m1:
                frac = 1.0 if m1 == m0 else (ms - m0) / (m1 - m0)
                v = p0.value + (p1.value - p0.value) * frac
                st = val_to_st(v)
                break
        pw = semitones_to_pitchwheel(st, pb_range)
        if pw != last_val:
            # держим точки кривой строго ДО тика сброса, иначе финальная точка
            # (ORDER_CTRL > ORDER_RESET) затрёт сброс в 0 и PB утечёт в атаку.
            tick = min(start_tick + ms_to_ticks(ms, bpm), start_tick + dur_ticks - 1)
            ev.add(tick, ORDER_CTRL, Message("pitchwheel", channel=CHANNEL, pitch=pw))
            last_val = pw
        ms += BEND_STEP_MS

    ev.add(start_tick + dur_ticks, ORDER_RESET,
           Message("pitchwheel", channel=CHANNEL, pitch=0))


def _vibrato_amount_at(ms, note_ms, amp, delay_ms=VIBRATO_DELAY_MS,
                        ramp_ms=VIBRATO_RAMP_MS,
                        release_ms=VIBRATO_RELEASE_MS):
    """Глубина вибрато (0..amp) в момент ms от атаки ноты — огибающая.

    delay -> линейное нарастание за ramp_ms -> удержание -> спад к концу.
    Это ЗНАЧЕНИЕ РУЧКИ, а не позиция колебания: качает высоту сам инструмент.
    """
    if ms < delay_ms:
        return 0.0
    rel_start = max(delay_ms, note_ms - release_ms)
    if ms >= rel_start:
        frac = (note_ms - ms) / max(1.0, note_ms - rel_start)
        return amp * max(0.0, min(1.0, frac))
    ramp = ms - delay_ms
    if ramp < ramp_ms:
        return amp * (ramp / max(1.0, ramp_ms))
    return float(amp)


def emit_vibrato(ev, start_tick, dur_ticks, bpm, stats=None, vibrato_type=None,
                 amp_override=None, delay_ms=None, ramp_ms=None, release_ms=None):
    """CC1 = глубина вибрато инструмента. См. VIBRATO_MODE.

    dur_ticks — ПОЛНАЯ звучащая длительность ноты, включая лиги. Вызывается
    ПОСЛЕ основного цикла: во время цикла длина лигованной ноты ещё неизвестна,
    и кривая рисовалась по первому сегменту, а сброс CC1 в 0 приходился на
    середину звучащей ноты (нота 6.6 с — вибрато умирало на 1.3 с).

    Возвращает количество добавленных CC1-событий.
    """
    amp = (VIBRATO_AMP_BY_TYPE.get(vibrato_type or "", VIBRATO_AMP)
           if amp_override is None else clamp_cc(amp_override))
    delay_ms = VIBRATO_DELAY_MS if delay_ms is None else max(0, float(delay_ms))
    ramp_ms = VIBRATO_RAMP_MS if ramp_ms is None else max(1, float(ramp_ms))
    release_ms = VIBRATO_RELEASE_MS if release_ms is None else max(1, float(release_ms))
    note_ms = ticks_to_ms(dur_ticks, bpm)
    if note_ms < VIBRATO_MIN_NOTE_MS:
        return 0
    if note_ms <= delay_ms:
        return 0
    count = 0
    last_val = None
    ms = delay_ms
    while ms <= note_ms:
        if VIBRATO_MODE == "sine":
            t = (ms - delay_ms) / 1000.0
            val = (amp / 2.0) * (1.0 + math.sin(2.0 * math.pi * VIBRATO_FREQ_HZ * t))
        else:
            val = _vibrato_amount_at(ms, note_ms, amp, delay_ms, ramp_ms, release_ms)
        cc = clamp_cc(val)
        if cc != last_val:
            tick = min(start_tick + ms_to_ticks(ms, bpm), start_tick + dur_ticks - 1)
            ev.add(tick, ORDER_CTRL,
                   Message("control_change", channel=CHANNEL, control=VIBRATO_CC, value=cc))
            last_val = cc
            count += 1
        ms += VIBRATO_STEP_MS
    # сброс CC1 в 0 после ноты
    ev.add(start_tick + dur_ticks, ORDER_RESET,
           Message("control_change", channel=CHANNEL, control=VIBRATO_CC, value=0))
    count += 1
    if stats is not None:
        stats["cc1"] += count
    return count


def emit_slide(ev, start_tick, dur_ticks, slides, this_pitch, next_pitch, bpm,
               pb_range=PITCH_BEND_RANGE_ST):
    """Слайд: Pitch Bend в последние 50 мс ноты в сторону следующей ноты, затем сброс."""
    direction = 0
    if next_pitch is not None:
        direction = 1 if next_pitch > this_pitch else (-1 if next_pitch < this_pitch else 0)
    if direction == 0:
        for s in slides:
            if s in (SlideType.shiftSlideTo, SlideType.legatoSlideTo, SlideType.outUpwards,
                     SlideType.intoFromBelow):
                direction = 1
            elif s in (SlideType.outDownwards, SlideType.intoFromAbove):
                direction = -1
    if direction == 0:
        return

    lead_ticks = ms_to_ticks(SLIDE_LEAD_MS, bpm)
    begin_tick = max(start_tick, start_tick + dur_ticks - lead_ticks)
    target_st = direction * SLIDE_BEND_ST

    steps = max(1, int(SLIDE_LEAD_MS / BEND_STEP_MS))
    for i in range(steps + 1):
        frac = i / float(steps)
        pw = semitones_to_pitchwheel(target_st * frac, pb_range)
        tick = min(begin_tick + int((start_tick + dur_ticks - begin_tick) * frac),
                   start_tick + dur_ticks - 1)
        ev.add(tick, ORDER_CTRL, Message("pitchwheel", channel=CHANNEL, pitch=pw))
    ev.add(start_tick + dur_ticks, ORDER_RESET,
           Message("pitchwheel", channel=CHANNEL, pitch=0))


def emit_keyswitch(ev, tick, note, bpm):
    """Короткий keyswitch-импульс note_on/note_off РАНЬШЕ основной ноты на KS_LEAD_MS."""
    lead = ms_to_ticks(KS_LEAD_MS, bpm)
    ks_len = max(1, ms_to_ticks(KS_LEN_MS, bpm))
    on_tick = max(0, tick - lead)
    ev.add(on_tick, ORDER_KS, Message("note_on", channel=CHANNEL, note=note, velocity=KS_VELOCITY))
    ev.add(on_tick + ks_len, ORDER_KS, Message("note_off", channel=CHANNEL, note=note, velocity=0))


# --------------------------------------------------------------------------- #
#  Определение артикуляции ноты -> keyswitch (через конфиг инструмента)
# --------------------------------------------------------------------------- #
def _canonical_articulation(note, beat=None):
    """Каноническое имя артикуляции по GP-эффектам ноты (независимо от
    инструмента). Конфиг инструмента затем переводит имя в keyswitch
    (с учётом gp_effect_overrides и фолбэка в sustain)."""
    eff = note.effect
    htype = type(eff.harmonic).__name__ if eff.harmonic is not None else None

    if htype == "PinchHarmonic":
        return "pinch_harmonics"
    if htype == "TappedHarmonic":
        return "tapped_harmonics"
    if htype in ("NaturalHarmonic", "ArtificialHarmonic", "SemiHarmonic"):
        return "harmonics"
    if getattr(eff, "tapping", False):
        return "tapping"
    # pyguitarpro: tapping — бит-эффект (slapEffect)
    if beat is not None and getattr(getattr(beat, "effect", None), "slapEffect", None) is not None:
        if getattr(beat.effect.slapEffect, "name", "") == "tapping":
            return "tapping"
    if eff.tremoloPicking:
        return "tremolo"
    if eff.trill:
        return "trill"
    if eff.palmMute:
        return "palm_mute"
    if eff.staccato:
        return "staccato"
    return "sustain"


def resolve_articulation(name, cfg):
    """Каноническое имя -> имя артикуляции из keyswitches конфига.

    Порядок: gp_effect_overrides -> прямое совпадение -> sustain (фолбэк,
    напр. tremolo у Darkwall, которого там нет).
    """
    overrides = cfg.get("gp_effect_overrides") or {}
    name = overrides.get(name, name)
    if name in cfg["keyswitches"]:
        return name
    return "sustain"


def note_articulation(note, track_type, cfg=None, beat=None):
    """Вернуть (ks_note, is_pinch) для ноты по её эффектам и конфигу инструмента.

    is_pinch=True означает Pinch Harmonic (Hydra): KS остаётся sustain,
    но velocity ноты форсируется (velocity из конфига, у Hydra 127).
    """
    cfg = cfg or config_for_track_type(track_type)
    resolved = resolve_articulation(_canonical_articulation(note, beat), cfg)
    spec = cfg["keyswitches"][resolved]
    is_pinch = resolved == "pinch_harmonics" and "velocity" in spec
    return (int(spec["note"]), is_pinch)


def beat_keyswitch(beat, track_type, cfg=None):
    """Один KS на бит: артикуляция с наивысшим приоритетом среди нот бита."""
    cfg = cfg or config_for_track_type(track_type)
    prio = articulation_priority(cfg)
    names = [resolve_articulation(_canonical_articulation(n, beat), cfg) for n in beat.notes]
    best = min(names, key=lambda nm: prio.get(nm, 99))
    return keyswitch_note(cfg, best)


def accent_boosted_velocity(note, velocity):
    """Категория A: Accent/Marcato -> velocity ноты + буст (все типы треков)."""
    eff = note.effect
    accent = int(getattr(eff, "accent", 0) or 0)
    if not accent:                       # pyguitarpro: булевы флаги
        if getattr(eff, "heavyAccentuatedNote", False):
            accent = 2
        elif getattr(eff, "accentuatedNote", False):
            accent = 1
    if accent >= 2:
        return clamp_vel(velocity + HEAVY_ACCENT_VEL_BOOST)
    if accent == 1:
        return clamp_vel(velocity + ACCENT_VEL_BOOST)
    return velocity


# --------------------------------------------------------------------------- #
#  Натуральные / искусственные гармоники: реальная звучащая высота
# --------------------------------------------------------------------------- #
# Лад узла (относительно прижатого лада; для natural — от нулевого порожка)
# -> интервал в полутонах над базовой нотой. Стандартная флажолетная сетка.
HARMONIC_NODE_INTERVALS = [
    (12.0, 12),                 # октава
    (7.0, 19), (19.0, 19),      # октава + квинта
    (5.0, 24), (24.0, 24),      # 2 октавы
    (4.0, 28), (9.0, 28), (16.0, 28),   # 2 окт + большая терция
    (3.2, 31), (14.7, 31),      # 2 окт + квинта
    (2.7, 34),                  # 2 окт + малая септима
    (2.4, 36),                  # 3 октавы
]
HARMONIC_NODE_TOLERANCE = 0.35


def harmonic_interval_from_node(node_fret):
    """Интервал (полутона) по ладу узла гармоники, None если узел не распознан."""
    try:
        node = float(node_fret)
    except (TypeError, ValueError):
        return None
    best = None
    for fret, interval in HARMONIC_NODE_INTERVALS:
        d = abs(node - fret)
        if d <= HARMONIC_NODE_TOLERANCE and (best is None or d < best[0]):
            best = (d, interval)
    return best[1] if best else None


def harmonic_sounding_pitch(note, string_pitch):
    """Реальная звучащая высота гармоники (Natural/Artificial), None если
    пересчёт не нужен/невозможен (тогда экспортируем прижатую позицию).

    GP8/GPIF: note.harmonicFret — лад узла ОТНОСИТЕЛЬНО прижатого лада
    (для natural прижатый лад = 0, т.е. это просто лад флажолета).
    GP5 (pyguitarpro): для NaturalHarmonic узел = сам лад ноты.
    Пересчёт — обязанность экспортёра: Hydra/TACT его не делает.
    """
    eff = note.effect
    htype = type(eff.harmonic).__name__ if eff.harmonic is not None else None
    if htype not in ("NaturalHarmonic", "ArtificialHarmonic", "SemiHarmonic"):
        return None
    base = string_pitch.get(note.string, 0) + note.value

    node = float(getattr(note, "harmonicFret", 0.0) or 0.0)
    if not node and htype == "NaturalHarmonic":
        node = float(note.value)        # GP5: лад ноты и есть узел
        base = string_pitch.get(note.string, 0)
    interval = harmonic_interval_from_node(node)
    if interval is None:
        return None
    return clamp_note(base + interval)


def shreddage_harmonic_pitch(note, string_pitch, cfg):
    """Высота гармоники для Shreddage-трека с учётом конфига артикуляции.

    None = «не менять» (не гармоника / режим fretted / узел не распознан).
    У артикуляции Harmonics свой диапазон сэмплов, уже плейбл-рейнджа
    инструмента: ноты выше max_note дают ТИШИНУ в Kontakt («velocity в ноль»),
    поэтому складываем октавами вниз — питч-класс сохраняется, нота звучит.
    """
    spec = (cfg.get("keyswitches") or {}).get("harmonics") or {}
    if spec.get("pitch_mode", "sounding") == "fretted":
        return None
    sounding = harmonic_sounding_pitch(note, string_pitch)
    if sounding is None:
        return None
    max_note = spec.get("max_note")
    if max_note:
        while sounding > int(max_note):
            sounding -= 12
    return sounding


# --------------------------------------------------------------------------- #
#  Общая мета (имя, программа, темп, тактовый размер)
# --------------------------------------------------------------------------- #
def add_track_meta(ev, song, track):
    ev.add(0, ORDER_META, MetaMessage("track_name", name=track.name or "", time=0))
    ev.add(0, ORDER_META, Message("program_change", channel=CHANNEL,
                                  program=clamp_note(track.channel.instrument)))
    base_bpm = float(song.tempo) if song.tempo else 120.0
    ev.add(0, ORDER_META, MetaMessage("set_tempo", tempo=bpm2tempo(base_bpm), time=0))
    return base_bpm


# --------------------------------------------------------------------------- #
#  Сборка MIDI: GUITAR / BASS (с кейсвитчами, CC1, PB)
# --------------------------------------------------------------------------- #
def hidden_32nd_note_timing(beat, enabled=False):
    """Вернуть {id(note): (shift_ticks, duration_scale)} для скрытых GP8 32-х.

    Реальный аккорд имеет одинаковые/близкие playback Offset. Последовательность,
    визуально записанная одним Beat, имеет группы Offset с шагом около 60 тиков
    GPIF (1/32 при 480 PPQ). Группы нормализуются к началу нотного Beat, сохраняя
    внутреннюю полифонию каждой группы и GP playback Duration каждой ноты.
    """
    if not enabled:
        return {}
    notes = [note for note in beat.notes if note.type != NoteType.tie]
    if len(notes) < 2:
        return {}

    ordered = sorted(notes, key=lambda note: int(getattr(note, "playedOffset", 0) or 0))
    groups = []
    for note in ordered:
        offset = int(getattr(note, "playedOffset", 0) or 0)
        if not groups or offset - groups[-1]["last"] > HIDDEN_32ND_GROUP_TOLERANCE:
            groups.append({"offsets": [offset], "notes": [note], "last": offset})
        else:
            groups[-1]["offsets"].append(offset)
            groups[-1]["notes"].append(note)
            groups[-1]["last"] = offset
    if len(groups) < 2:
        return {}

    centers = [sum(group["offsets"]) / len(group["offsets"]) for group in groups]
    gaps = [right - left for left, right in zip(centers, centers[1:])]
    if not all(HIDDEN_32ND_GAP_MIN <= gap <= HIDDEN_32ND_GAP_MAX for gap in gaps):
        return {}

    scale = float(TICKS_PER_BEAT) / GPIF_PLAYED_PPQ
    first = centers[0]
    timing = {}
    for group, center in zip(groups, centers):
        shift = int(round((center - first) * scale))
        for note in group["notes"]:
            duration_scale = max(0.05, float(getattr(note, "playedDuration", 1.0) or 1.0))
            timing[id(note)] = (shift, duration_scale)
    return timing


def build_instrument_midi(song, track, track_type, cfg=None, humanize=False,
                          humanize_seed=7, auto_sustain_vibrato=False,
                          fret_noise_on_hand_shift=False, performance_seed=7,
                          expand_gp_hidden_32nds=False):
    """Собрать MidiTrack для гитарного/басового трека. Вернуть (track, stats).

    Маппинг keyswitch-ей и Pitch Bend Range берутся из версионируемого
    конфига инструмента (config/articulation_maps/*.yaml)."""
    cfg = cfg or config_for_track_type(track_type)
    stats = {"notes": 0, "ks": 0, "cc1": 0, "auto_vibrato_notes": 0,
             "fret_noise_events": 0, "hidden_32nd_beats": 0,
             "hidden_32nd_notes": 0,
             "config": config_label(cfg), "warnings": []}
    log_config_used(track.name, cfg)

    track_name_folded = (getattr(track, "name", "") or "").casefold()
    expand_hidden_on_track = (
        expand_gp_hidden_32nds
        and track_type == TRACK_GUITAR
        and any(word in track_name_folded for word in ("solo", "lead"))
    )

    sustain_ks = keyswitch_note(cfg, "sustain")
    sustain_spec = cfg["keyswitches"]["sustain"]
    sustain_vel_max = int(sustain_spec.get("vel_max", 127))
    pb_range = float(cfg.get("pitch_bend_range") or PITCH_BEND_RANGE_ST)
    life_cfg = cfg.get("performance_life") or {}
    fret_cfg = life_cfg.get("fret_noise_on_hand_shift") or {}
    fret_rng = random.Random(performance_seed ^ 0xFEE7)
    previous_hand_position = None

    ev = EventList()
    base_bpm = add_track_meta(ev, song, track)
    # RPN 0,0 (Pitch Bend Range): Kontakt его игнорирует, но DAW/другие
    # плагины получают правильный диапазон.
    emit_pitch_bend_range_rpn(ev, pb_range, tick=0)
    last_ts = None
    last_bpm = base_bpm
    cur_bpm = base_bpm

    string_pitch = {s.number: s.value for s in track.strings}
    pb_dirty = False                # PB оставлен ненулевым предыдущей нотой

    # Оживление: профиль на тип трека, у гитары свой (velocity здесь —
    # переключатель артикуляции, поэтому барабанный сюда не годится).
    hprof = profile_for_track_type(track_type) if humanize else None
    hrng = random.Random(humanize_seed) if humanize else None
    if hprof is not None:
        stats["humanize"] = profile_label(hprof)
        logger.info("трек %r: оживление %s", track.name, profile_label(hprof))

    # Стартовую артикуляцию УСТАНАВЛИВАЕМ явно, а не предполагаем.
    #
    # Keyswitch эмитится только когда артикуляция МЕНЯЕТСЯ, а начальное состояние
    # раньше просто постулировалось (current_ks = sustain_ks). Kontakt между
    # проигрываниями ничего не сбрасывает: он остаётся на той артикуляции, что
    # была выбрана последней. Поэтому трек, у которого начало играется обычным
    # sustain, наследовал чужое состояние: у пользователя Solo Guitar имеет ноты
    # с 8-го такта, а первый keyswitch стоял только в 58-м — и всё первое соло
    # звучало гармониками, оставшимися от предыдущего проигрывания.
    # Сброс в sustain в конце трека (ниже) от этого не спасает: он условный и не
    # срабатывает, если остановить воспроизведение посередине.
    emit_keyswitch(ev, 0, sustain_ks, base_bpm)
    stats["ks"] += 1
    current_ks = sustain_ks
    pending_vibrato = []            # (start_tick, off_rec, bpm, vibrato_type)
    pending_auto_vibrato = []       # subset элементов all_note_spans
    all_note_spans = []             # все атаки для проверки монодичности CC1
    last_palm_tick = 0
    last_end_tick = 0
    last_off_by_voice = {}
    pending_legato = {}             # (voice_idx, string) -> off_rec источника hammer/pull

    for measure, voice, vi, bi, beat, measure_start_tick, start_tick, dur in iter_voice_beats_with_canonical_ticks(track):
        # last_off keyed by voice INDEX (not id(voice)): GP8/GPIF import creates a
        # fresh voice object per measure, so id(voice) breaks tie linking across
        # barlines. Voice index is stable across measures -> ties over barlines hold.
        last_off = last_off_by_voice.setdefault(vi, {})
        ts = measure.header.timeSignature
        ts_key = (ts.numerator, ts.denominator.value)
        if ts_key != last_ts:
            ev.add(measure_start_tick, ORDER_META,
                   MetaMessage("time_signature", numerator=ts.numerator,
                               denominator=ts.denominator.value, time=0))
            last_ts = ts_key

        if beat is None:
            continue

        mtc = getattr(beat.effect, "mixTableChange", None)
        if mtc is not None and getattr(mtc, "tempo", None):
            new_bpm = mtc.tempo.value
            if new_bpm and new_bpm != last_bpm:
                ev.add(start_tick, ORDER_META,
                       MetaMessage("set_tempo", tempo=bpm2tempo(float(new_bpm)), time=0))
                last_bpm = cur_bpm = float(new_bpm)

        last_end_tick = max(last_end_tick, start_tick + dur)

        if not beat.notes:
            continue

        # --- оживление: сдвиг ВСЕГО бита ---
        # Считается ЗДЕСЬ, до эмиссии keyswitch/PB и до расчёта лиг и легато,
        # чтобы всё нижележащее унаследовало уже сдвинутый тик. Пост-проходом
        # это делать нельзя: KS приходит за KS_LEAD_MS=15 до ноты, и уехавшая
        # назад нота оказалась бы раньше собственного keyswitch-а.
        if hprof is not None:
            start_tick = pitched_beat_shift(start_tick, cur_bpm, TICKS_PER_BEAT,
                                           hprof, hrng)

        # --- скрытые 32-е GP8: playback-группы внутри одного нотного Beat ---
        hidden_timing = hidden_32nd_note_timing(beat, enabled=expand_hidden_on_track)
        if hidden_timing:
            stats["hidden_32nd_beats"] += 1
            stats["hidden_32nd_notes"] += len(hidden_timing)

        # --- strum: разброс нот аккорда по струнам (только вперёд от start_tick) ---
        # Для hidden-32nd Beat уже есть авторский GP playback-тайминг; второй
        # независимый разброс поверх него исказил бы группировку.
        strum, strum_down = {}, True
        if hprof is not None and not hidden_timing:
            strum, strum_down = pitched_strum_offsets(
                [clamp_note(string_pitch.get(n.string, 0) + n.value)
                 for n in beat.notes if n.type != NoteType.tie],
                start_tick, cur_bpm, TICKS_PER_BEAT, hprof, hrng)

        # --- fret noise: только при реальном переносе позиции левой руки ---
        frets = sorted(int(n.value) for n in beat.notes
                       if n.type != NoteType.tie and int(n.value) > 0)
        if frets:
            middle = len(frets) // 2
            hand_position = (float(frets[middle]) if len(frets) % 2
                             else (frets[middle - 1] + frets[middle]) / 2.0)
            if (fret_noise_on_hand_shift and track_type == TRACK_GUITAR and fret_cfg
                    and previous_hand_position is not None
                    and abs(hand_position - previous_hand_position)
                    >= float(fret_cfg.get("min_fret_shift", 4))
                    and fret_rng.random() < max(0.0, min(
                        1.0, float(fret_cfg.get("probability", 0.0))))):
                fret_note = (cfg.get("fx_keyswitches") or {}).get("fret_noise")
                if fret_note is not None:
                    emit_keyswitch(ev, start_tick, int(fret_note), cur_bpm)
                    stats["ks"] += 1
                    stats["fret_noise_events"] += 1
            previous_hand_position = hand_position

        # --- keyswitch для бита: переключаем артикуляцию при изменении ---
        ks_note = beat_keyswitch(beat, track_type, cfg)
        if ks_note != current_ks:
            emit_keyswitch(ev, start_tick, ks_note, cur_bpm)
            stats["ks"] += 1
            current_ks = ks_note
            last_palm_tick = start_tick

        for note in beat.notes:
            eff = note.effect
            open_v = string_pitch.get(note.string, 0)
            pitch = clamp_note(open_v + note.value)
            # натуральные/искусственные гармоники: реальная звучащая высота
            # (октавами вниз до потолка диапазона артикуляции Harmonics)
            sounding = shreddage_harmonic_pitch(note, string_pitch, cfg)
            if sounding is not None:
                pitch = sounding

            # tie-нота: продлеваем предыдущую ноту на струне, не атакуем заново
            if note.type == NoteType.tie:
                rec = last_off.get(note.string)
                if rec is not None:
                    rec["tick"] = start_tick + dur
                continue

            art_name, is_pinch = note_articulation(note, track_type, cfg, beat)

            # --- velocity ---
            velocity = accent_boosted_velocity(note, clamp_vel(note.velocity))
            if is_pinch:
                velocity = int(cfg["keyswitches"]["pinch_harmonics"]["velocity"])
            elif ks_note == sustain_ks:
                # не задеть vel-зоны на ноте sustain (Rake 120-126 / Pinch 127)
                if hprof is not None:
                    velocity = pitched_velocity(velocity, start_tick, TICKS_PER_BEAT,
                                               art_name, hprof, hrng,
                                               vel_cap=sustain_vel_max,
                                               down=strum_down)
                velocity = min(velocity, sustain_vel_max)
            elif hprof is not None:
                # прочие артикуляции (palm_mute и т.п.): своей vel-зоны нет,
                # но выше 127 всё равно нельзя
                velocity = pitched_velocity(velocity, start_tick, TICKS_PER_BEAT,
                                           art_name, hprof, hrng, vel_cap=127,
                                           down=strum_down)

            hidden_entry = hidden_timing.get(id(note))
            hidden_shift = hidden_entry[0] if hidden_entry else 0
            on_tick = start_tick + hidden_shift + strum.get(pitch, 0)

            # --- легато (B15): эта нота — ЦЕЛЬ ожидающего hammer/pull? ---
            # Источник (нота с флагом hammer) продлевается внахлёст в цель,
            # если он дозвучал до её атаки (иначе легато прервано паузой).
            origin = pending_legato.get((vi, note.string))
            if origin is not None:
                if origin["tick"] >= on_tick:
                    overlap = ms_to_ticks(LEGATO_OVERLAP_MS, cur_bpm)
                    origin["tick"] = max(origin["tick"], on_tick + overlap)
                del pending_legato[(vi, note.string)]

            # --- принудительный сброс PB перед атакой ---
            if pb_dirty:
                ev.add(on_tick, ORDER_RESET,
                       Message("pitchwheel", channel=CHANNEL, pitch=0))
                pb_dirty = False

            # --- note on / off ---
            ev.add(on_tick, ORDER_ON,
                   Message("note_on", channel=CHANNEL, note=pitch, velocity=velocity))
            if hidden_entry:
                note_duration = max(1, int(round(dur * hidden_entry[1])))
                note_end = on_tick + note_duration
            else:
                # Legacy/strum: конец остаётся на сетке Beat, как раньше.
                note_duration = dur
                note_end = start_tick + dur
            off_rec = ev.add(note_end, ORDER_OFF,
                             Message("note_off", channel=CHANNEL, note=pitch, velocity=0))
            note_span = {"start": on_tick, "off": off_rec, "bpm": cur_bpm}
            all_note_spans.append(note_span)
            last_off[note.string] = off_rec
            stats["notes"] += 1

            # эта нота — источник hammer/pull: продлить её в следующую ноту
            # на той же струне (см. блок легато выше)
            if eff.hammer:
                pending_legato[(vi, note.string)] = off_rec

            # --- PB / вибрато / слайды ---
            if eff.bend and eff.bend.points:
                emit_bend(ev, on_tick, note_duration, eff.bend, cur_bpm, pb_range)
                pb_dirty = True

            if eff.vibrato:
                # НЕ рисуем сразу: лиги продлят ноту позже по циклу, и off_rec
                # ("tick") — единственное место, где к концу цикла окажется
                # настоящая длина. Откладываем до конца (см. pending_vibrato).
                pending_vibrato.append((on_tick, off_rec, cur_bpm,
                                        getattr(eff, "vibratoType", None)))
            elif (auto_sustain_vibrato and track_type == TRACK_GUITAR
                  and ks_note == sustain_ks and not is_pinch
                  and not eff.bend and not eff.slides):
                pending_auto_vibrato.append(note_span)

            if eff.slides:
                next_pitch = _next_note_pitch(voice.beats, bi, note.string, string_pitch)
                emit_slide(ev, on_tick, note_duration, eff.slides, pitch, next_pitch, cur_bpm,
                           pb_range)
                pb_dirty = True

    # Вибрато рисуем ЗДЕСЬ, когда все лиги отработали и off_rec["tick"] у каждой
    # ноты содержит её настоящий конец.
    for v_start, v_off, v_bpm, v_type in pending_vibrato:
        emit_vibrato(ev, v_start, v_off["tick"] - v_start, v_bpm, stats,
                     vibrato_type=v_type)

    auto_cfg = life_cfg.get("auto_sustain_vibrato") or {}
    auto_rng = random.Random(performance_seed)
    role_words = [str(word).casefold() for word in auto_cfg.get("track_name_keywords", [])]
    track_role_allowed = (not role_words or any(
        word in (getattr(track, "name", "") or "").casefold() for word in role_words
    ))
    if auto_sustain_vibrato and auto_cfg and track_role_allowed:
        probability = max(0.0, min(1.0, float(auto_cfg.get("probability", 0.0))))
        min_note_ms = float(auto_cfg.get("min_note_ms", 900))
        delay_range = auto_cfg.get("delay_ms", [250, 500])
        amount_range = auto_cfg.get("amount", [18, 34])
        for item in pending_auto_vibrato:
            start = item["start"]
            end = item["off"]["tick"]
            if any(other is not item
                   and other["start"] < end
                   and start < other["off"]["tick"]
                   for other in all_note_spans):
                continue
            dur_ticks = end - start
            if ticks_to_ms(dur_ticks, item["bpm"]) < min_note_ms:
                continue
            if auto_rng.random() >= probability:
                continue
            delay = auto_rng.uniform(float(delay_range[0]), float(delay_range[-1]))
            amount = auto_rng.randint(int(amount_range[0]), int(amount_range[-1]))
            emit_vibrato(
                ev, item["start"], dur_ticks, item["bpm"], stats,
                amp_override=amount, delay_ms=delay,
                ramp_ms=float(auto_cfg.get("ramp_ms", 180)),
                release_ms=float(auto_cfg.get("release_ms", 120)),
            )
            stats["auto_vibrato_notes"] += 1

    # сброс артикуляции в sustain в конце трека
    if current_ks != sustain_ks:
        reset_tick = max(last_end_tick, last_palm_tick + 1)
        emit_keyswitch(ev, reset_tick, sustain_ks, cur_bpm)
        stats["ks"] += 1

    return ev.to_miditrack(), stats


# --------------------------------------------------------------------------- #
#  Сборка MIDI: OTHER (Logic-инструменты: synth, fx, strings, piano, vocals)
# --------------------------------------------------------------------------- #
def _hairpin_ramps(items):
    """Найти hairpin-ы (Crescendo/Decrescendo) и построить CC11-рампы.

    Возвращает список (tick_from, tick_to, cc_to). Целевой уровень берём из
    первой последующей ноты того же голоса с ИЗМЕНИВШЕЙСЯ velocity (в GP
    динамика проставлена на каждом бите). Если цель не нашлась в пределах
    HAIRPIN_MAX_LOOKAHEAD_BEATS — фолбэк-рампа на один такт к 50%.
    """
    ramps = []
    for i, (measure, voice, vi, bi, beat, mst, start_tick, dur) in enumerate(items):
        hairpin = getattr(beat.effect, "hairpin", None)
        if not hairpin or not beat.notes:
            continue
        v0 = max(clamp_vel(n.velocity) for n in beat.notes)
        target = None
        seen = 0
        for measure2, voice2, vi2, bi2, beat2, mst2, tick2, dur2 in items[i + 1:]:
            if vi2 != vi:
                continue
            seen += 1
            if seen > HAIRPIN_MAX_LOOKAHEAD_BEATS:
                break
            if not beat2.notes:
                continue
            v1 = max(clamp_vel(n.velocity) for n in beat2.notes)
            if (hairpin == "Decrescendo" and v1 < v0) or (hairpin == "Crescendo" and v1 > v0):
                target = (tick2, v1)
                break
            if v1 != v0:
                break  # динамика ушла не в ту сторону — рампу не строим
        if target is not None:
            tick_to, v1 = target
            cc_to = max(HAIRPIN_MIN_CC, min(127, int(round(127.0 * v1 / v0))))
        else:
            tick_to = start_tick + dur * 4  # фолбэк: один такт (4 бита) вперёд
            ratio = HAIRPIN_FALLBACK_RATIO if hairpin == "Decrescendo" else 1.0
            if ratio >= 1.0:
                continue  # crescendo без цели: CC11 выше 127 не бывает, пропускаем
            cc_to = max(HAIRPIN_MIN_CC, int(round(127 * ratio)))
        if tick_to > start_tick:
            ramps.append((start_tick, tick_to, cc_to))
    return ramps


def build_other_midi(song, track):
    """MIDI для не-Shreddage дорожек (Logic-инструменты).

    Категория A (универсальные эффекты, переносим ВСЕГДА):
      - Staccato -> gate time ~50% номинала;
      - Accent/Marcato -> velocity ноты + буст;
      - динамика (velocity из GP) — как есть;
      - hairpins (cresc./decresc.) -> кривая CC11 Expression со сбросом в 127
        на следующей атаке после рампы.
    Категория B (гитарная техника Shreddage: P.M., harmonics, rakes, tapping,
    hammer-on, slides) — НЕ переносится: это ошибка разметки для таких треков.
    Категория C (Pitch Bend) — не эмитим: диапазон бенда у Logic-инструментов
    не подтверждён (открытый вопрос).
    Без keyswitch-ей / CC1-вибрато / legato-хака velocity=1. Темп сохраняем.
    """
    ev = EventList()
    stats = {"notes": 0, "ks": 0, "cc1": 0, "config": None, "warnings": []}
    log_config_used(track.name, None)

    base_bpm = add_track_meta(ev, song, track)
    last_ts = None
    last_bpm = base_bpm

    string_pitch = {s.number: s.value for s in track.strings}
    last_off_by_voice = {}

    items = list(iter_voice_beats_with_canonical_ticks(track))

    # Категория A: hairpins -> CC11 (со сбросом на следующей атаке)
    ramps = _hairpin_ramps(items)
    if ramps:
        ev.add(0, ORDER_META,
               Message("control_change", channel=CHANNEL, control=HAIRPIN_CC, value=127))
    note_on_ticks = sorted(
        start_tick
        for _, _, _, _, beat, _, start_tick, _ in items
        for n in beat.notes
        if getattr(n.type, "name", "") != "tie"
    )
    for tick_from, tick_to, cc_to in ramps:
        span = tick_to - tick_from
        steps = max(1, span // HAIRPIN_STEP_TICKS)
        last_val = None
        for s in range(steps + 1):
            frac = s / float(steps)
            val = int(round(127 + (cc_to - 127) * frac))
            # держим кривую строго ДО tick_to, чтобы не спорить со сбросом
            tick = min(tick_from + int(span * frac), tick_to - 1)
            if val != last_val:
                ev.add(tick, ORDER_CTRL,
                       Message("control_change", channel=CHANNEL, control=HAIRPIN_CC, value=val))
                last_val = val
        # сброс в 127 на первой атаке ПОСЛЕ рампы (или в tick_to, если атак нет)
        reset_tick = next((t for t in note_on_ticks if t >= tick_to), tick_to)
        ev.add(reset_tick, ORDER_RESET,
               Message("control_change", channel=CHANNEL, control=HAIRPIN_CC, value=127))

    for measure, voice, vi, bi, beat, measure_start_tick, start_tick, dur in items:
        # last_off keyed by voice INDEX (not id(voice)): GP8/GPIF import creates a
        # fresh voice object per measure, so id(voice) breaks tie linking across
        # barlines. Voice index is stable across measures -> ties over barlines hold.
        last_off = last_off_by_voice.setdefault(vi, {})
        ts = measure.header.timeSignature
        ts_key = (ts.numerator, ts.denominator.value)
        if ts_key != last_ts:
            ev.add(measure_start_tick, ORDER_META,
                   MetaMessage("time_signature", numerator=ts.numerator,
                               denominator=ts.denominator.value, time=0))
            last_ts = ts_key

        mtc = getattr(beat.effect, "mixTableChange", None)
        if mtc is not None and getattr(mtc, "tempo", None):
            new_bpm = mtc.tempo.value
            if new_bpm and new_bpm != last_bpm:
                ev.add(start_tick, ORDER_META,
                       MetaMessage("set_tempo", tempo=bpm2tempo(float(new_bpm)), time=0))
                last_bpm = float(new_bpm)

        if not beat.notes:
            continue

        for note in beat.notes:
            pitch = clamp_note(string_pitch.get(note.string, 0) + note.value)
            if note.type == NoteType.tie:
                rec = last_off.get(note.string)
                if rec is not None:
                    rec["tick"] = start_tick + dur
                continue
            velocity = accent_boosted_velocity(note, clamp_vel(note.velocity))
            note_dur = dur
            if getattr(note.effect, "staccato", False):
                note_dur = max(1, int(dur * STACCATO_GATE))
            ev.add(start_tick, ORDER_ON,
                   Message("note_on", channel=CHANNEL, note=pitch, velocity=velocity))
            off_rec = ev.add(start_tick + note_dur, ORDER_OFF,
                             Message("note_off", channel=CHANNEL, note=pitch, velocity=0))
            last_off[note.string] = off_rec
            stats["notes"] += 1

    return ev.to_miditrack(), stats


# --------------------------------------------------------------------------- #
#  Сборка MIDI: DRUMS (Shreddage Drums)
# --------------------------------------------------------------------------- #
def build_drum_midi(song, track, cfg=None, humanize=False, humanize_seed=7,
                    ghost_notes=None):
    """MIDI для барабанного трека под Shreddage Drums.

    GM-нота из GP-percussion-трека переводится в ноту Shreddage Drums по
    конфигу shreddage_drums.yaml (remap: High Tom 50->48, Ride2 59->51,
    Surdo 87->41; Side Stick 37 — с предупреждением). Без KS / CC / PB.
    Категория A: акценты -> velocity.

    humanize=True дополнительно оживляет партию по профилю
    config/humanize_profiles/drums_metal.yaml: velocity синтезируется
    (на входе ApolloTab отдаёт для перкуссии плоские 95 на все ноты) и
    добавляется микро-тайминг. По умолчанию ВЫКЛЮЧЕНО — без флага выхлоп
    остаётся прежним.
    """
    cfg = cfg or config_for_track_type(TRACK_DRUMS)
    stats = {"notes": 0, "ks": 0, "cc1": 0,
             "config": config_label(cfg), "warnings": []}
    log_config_used(track.name, cfg)
    warned = set()

    ev = EventList()
    base_bpm = add_track_meta(ev, song, track)
    last_ts = None
    last_bpm = base_bpm
    pending = []

    string_pitch = {s.number: s.value for s in track.strings}

    for measure, voice, vi, bi, beat, measure_start_tick, start_tick, dur in iter_voice_beats_with_canonical_ticks(track):
        ts = measure.header.timeSignature
        ts_key = (ts.numerator, ts.denominator.value)
        if ts_key != last_ts:
            ev.add(measure_start_tick, ORDER_META,
                   MetaMessage("time_signature", numerator=ts.numerator,
                               denominator=ts.denominator.value, time=0))
            last_ts = ts_key

        mtc = getattr(beat.effect, "mixTableChange", None)
        if mtc is not None and getattr(mtc, "tempo", None):
            new_bpm = mtc.tempo.value
            if new_bpm and new_bpm != last_bpm:
                ev.add(start_tick, ORDER_META,
                       MetaMessage("set_tempo", tempo=bpm2tempo(float(new_bpm)), time=0))
                last_bpm = float(new_bpm)

        for note in beat.notes:
            if note.type == NoteType.tie:
                continue  # у барабанов лиги не имеют смысла
            gm_note = clamp_note(string_pitch.get(note.string, 0) + note.value)
            out_note, warn = drum_note_out(cfg, gm_note)
            if warn and gm_note not in warned:
                warned.add(gm_note)
                stats["warnings"].append(warn)
            if out_note is None:
                continue
            velocity = accent_boosted_velocity(note, clamp_vel(note.velocity))
            pending.append({"tick": start_tick, "note": clamp_note(out_note),
                            "vel": velocity,
                            "dur": max(1, min(dur, TICKS_PER_BEAT // 4))})
            stats["notes"] += 1

    if humanize:
        prof = profile_for_track_type(TRACK_DRUMS)
        if prof:
            rng = random.Random(humanize_seed)
            hstats = humanize_drums(pending, last_bpm, TICKS_PER_BEAT, prof, rng,
                                    ghost_override=ghost_notes)
            stats["humanize"] = hstats
            stats["notes"] += hstats.get("ghosts", 0)
            logger.info("трек %r: оживление %s, тайминг x%.2f по темпу %.1f, "
                        "гост-нот +%d", track.name, hstats["profile"],
                        hstats["tempo_k"], last_bpm, hstats.get("ghosts", 0))

    for p in pending:
        ev.add(p["tick"], ORDER_ON,
               Message("note_on", channel=CHANNEL, note=p["note"], velocity=p["vel"]))
        ev.add(p["tick"] + p["dur"], ORDER_OFF,
               Message("note_off", channel=CHANNEL, note=p["note"], velocity=0))

    return ev.to_miditrack(), stats


def build_combined_midi(midi_tracks, ticks_per_beat=TICKS_PER_BEAT):
    """Один Type 1 MIDI со ВСЕМИ дорожками разом (в дополнение к пофайловым).

    Смысл: перетащить в Logic один файл вместо тринадцати и получить сразу всю
    аранжировку по дорожкам. Содержимое дорожек — те же самые объекты, что идут
    в пофайловый экспорт, поэтому расхождения между "скачал всё" и "скачал по
    одной" быть не может по построению.

    Канал у всех дорожек остаётся 0, как и в пофайловом экспорте: Logic при
    импорте раскладывает Type 1 ПО ДОРОЖКАМ, а не по каналам, а инструменты тут
    Kontakt-овые — GM-канал 10 для барабанов им не нужен. Менять каналы значило
    бы разойтись с уже проверенными пользователем одиночными файлами.

    track_name у каждой дорожки проставлен в add_track_meta, поэтому в Logic они
    приезжают подписанными.
    """
    mf = MidiFile(type=1, ticks_per_beat=ticks_per_beat)
    for tr in midi_tracks:
        mf.tracks.append(tr)
    return mf


def _next_note_pitch(beats, bi, string, string_pitch):
    """Питч следующей ноты на той же струне (для определения направления слайда)."""
    for b in beats[bi + 1:]:
        for n in b.notes:
            if n.string == string:
                return clamp_note(string_pitch.get(n.string, 0) + n.value)
    return None


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def parse_cli_options(argv):
    """Разобрать простые CLI-флаги без побочных эффектов (удобно тестировать)."""
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    seed = 7
    for flag in flags:
        if flag.startswith("--seed="):
            seed = int(flag.split("=", 1)[1])
    return {
        "source": args[0] if len(args) == 1 else None,
        "humanize": "--humanize" in flags,
        "ghost_notes": True if "--ghost-notes" in flags else None,
        "no_verify": "--no-verify" in flags,
        "seed": seed,
        "auto_sustain_vibrato": "--auto-sustain-vibrato" in flags,
        "fret_noise_on_hand_shift": "--fret-noise-on-hand-shift" in flags,
        "expand_gp_hidden_32nds": "--expand-gp-hidden-32nds" in flags,
    }


def main(argv):
    options = parse_cli_options(argv)
    humanize = options["humanize"]
    ghost_notes = options["ghost_notes"]
    no_verify = options["no_verify"]
    seed = options["seed"]
    auto_sustain_vibrato = options["auto_sustain_vibrato"]
    fret_noise_on_hand_shift = options["fret_noise_on_hand_shift"]
    expand_gp_hidden_32nds = options["expand_gp_hidden_32nds"]

    if options["source"] is None:
        sys.exit("Использование: python gp_to_shreddage.py song.gp5 "
                 "[--humanize] [--ghost-notes] [--auto-sustain-vibrato] "
                 "[--fret-noise-on-hand-shift] [--expand-gp-hidden-32nds] [--seed=N]\n"
                 "  --humanize    velocity + микро-тайминг; без флага выхлоп прежний\n"
                 "  --ghost-notes добавить гост-ноты по рабочему (МЕНЯЕТ партию)\n"
                 "  --auto-sustain-vibrato мягкий CC1 на длинных монодических solo sustain\n"
                 "  --fret-noise-on-hand-shift C#0 при заметном переносе позиции руки\n"
                 "  --expand-gp-hidden-32nds разнести GP playback-пары на solo/lead guitar\n"
                 "  --no-verify   не гонять смок-проверку выхлопа")

    src = options["source"]
    if not os.path.isfile(src):
        sys.exit("Файл не найден: %s" % src)

    print("Читаю %s ..." % src)
    # Единый путь парсинга с веб-интерфейсом: parse_song сам выбирает
    # ApolloTab-адаптер для GP7/GP8 (.gp) и guitarpro.parse для GP3/4/5/GPX.
    from gp_import import parse_song
    song = parse_song(src)

    base = os.path.splitext(os.path.basename(src))[0]
    out_dir = os.path.join(os.path.dirname(os.path.abspath(src)), base + "_midi")
    os.makedirs(out_dir, exist_ok=True)

    used = {}
    summary = []
    warnings = []
    smoke_errors = []
    all_tracks = []

    for idx, track in enumerate(song.tracks, start=1):
        track_type = resolve_track_type(track)

        if track_type == TRACK_DRUMS:
            midi_track, stats = build_drum_midi(song, track, humanize=humanize,
                                                humanize_seed=seed,
                                                ghost_notes=ghost_notes)
        elif track_type == TRACK_OTHER:
            midi_track, stats = build_other_midi(song, track)
        else:
            midi_track, stats = build_instrument_midi(
                song, track, track_type,
                humanize=humanize, humanize_seed=seed,
                auto_sustain_vibrato=auto_sustain_vibrato,
                fret_noise_on_hand_shift=fret_noise_on_hand_shift,
                performance_seed=seed,
                expand_gp_hidden_32nds=expand_gp_hidden_32nds,
            )

        name = safe_filename(track.name) or ("Track_%d" % idx)
        fname = name
        if fname in used:
            used[fname] += 1
            fname = "%s_%d" % (name, used[fname])
        else:
            used[fname] = 1

        mf = MidiFile(type=0, ticks_per_beat=TICKS_PER_BEAT)
        mf.tracks.append(midi_track)
        out_path = os.path.join(out_dir, fname + ".mid")
        mf.save(out_path)
        all_tracks.append(midi_track)

        summary.append((track.name or "(без имени)", track_type, stats, os.path.basename(out_path)))
        if track_type == TRACK_OTHER:
            warnings.append(track.name or "(без имени)")
        for w in stats.get("warnings", []):
            print('WARNING [%s]: %s' % (track.name or "(без имени)", w))

        # смок-проверка готового артефакта: инварианты, а не музыка
        if not no_verify:
            found = smoke_check(out_path, track_type)
            if found:
                print('SMOKE [%s]:' % (track.name or "(без имени)"))
                for sev, code, msg in found:
                    if sev == "INFO" and not humanize:
                        continue      # неоживлённый экспорт квантован by design
                    mark = {"ERROR": "!!", "WARN": " !", "INFO": "  "}[sev]
                    print('  %s [%s] %s' % (mark, code, msg))
                    if sev == "ERROR":
                        smoke_errors.append((track.name, code))

    # --- вывод в консоль ---
    print("\n%-30s %-7s %6s %6s %6s" % ("ТРЕК", "ТИП", "НОТ", "KS", "CC1"))
    print("-" * 60)
    for tname, ttype, stats, _ in summary:
        print("%-30s %-7s %6d %6d %6d"
              % (tname[:30], ttype, stats["notes"], stats["ks"], stats["cc1"]))

    if warnings:
        print()
        for tname in warnings:
            print('WARNING: трек "%s" не распознан (нет ключевых слов) -> тип OTHER, '
                  "ноты скопированы as is. Добавьте слово в GUITAR_KEYWORDS/BASS_KEYWORDS "
                  "вручную, если нужен маппинг Shreddage." % tname)

    if smoke_errors:
        print("\nСМОК-ПРОВЕРКА: ОШИБКИ (%d)" % len(smoke_errors))
        for tname, code in smoke_errors:
            print('  %s: %s' % (tname, code))
    elif not no_verify:
        print("\nСмок-проверка: чисто")

    # сборный Type 1 со всеми дорожками — рядом с пофайловыми
    combined_path = None
    if all_tracks:
        combined_path = os.path.join(out_dir, "%s_ALL.mid" % safe_filename(base))
        build_combined_midi(all_tracks).save(combined_path)
        print("\nСборный файл (все дорожки): %s" % os.path.basename(combined_path))

    print("\nГотово. Файлы: %s" % out_dir)


if __name__ == "__main__":
    main(sys.argv)
