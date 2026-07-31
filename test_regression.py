"""Regression-тесты конвертера GP -> MIDI, привязанные к реальным образцам.

Образцы (в домашней директории пользователя):
  pnd_full.gp / pnd_full_no_audio.gp  — один постоянный темп (90.7996 BPM)
  ttn_full.gp / ttn_no_audio.gp       — ПЕРЕМЕННЫЙ темп (122.3 -> 125 -> 126.2)

Покрываемые баги (см. отчёты B1..B14):
  B1  лиги через тактовую черту рвались (id(voice) вместо индекса голоса) — ФИКС
  B2  CLI не поддерживал GP8 (guitarpro.parse вместо parse_song) — ФИКС
  B3  ApolloTab midi_pitch = string+fret + 24; экспорт обязан брать string+fret
  B4  мультиголос: второй голос склеивается последовательно (тайминг) — WARN + xfail
  B6  фантомные хвостовые паузы на каждый неиспользуемый голос (-1) — ФИКС
  B8  переменный темп: проверяем кусочно-постоянную темп-карту в MIDI
  B9  округление длительностей в триолях даёт +40 тиков в 2 тактах ttn — допуск
  B10 percussion: string/fret=-1 -> все ноты барабанов уходили в pitch 0 — ФИКС
      (GM-нота из таблиц Articulation + маппинг Shreddage Drums)
  B11 Darkwall: keyswitch-и были в октаве Hydra (12..21) вместо C-2 (0..7) — ФИКС
  B12 Категория A (staccato/accent/hairpin) терялась на не-Shreddage треках — ФИКС
  B13 tapping (GPIF Property "Tapped") не парсился ApolloTab — ФИКС (sidecar)
  B14 мульти-стафф треки: колонки Bars идут по нотоносцам, ApolloTab смещал
      все последующие треки на чужие партии — ФИКС (разворачивание в Staff N)
  B15 легато: флаг hammer стоит на ИСТОЧНИКЕ, а конвертер глушил его
      (velocity=1) и продлевал ноту ПЕРЕД ним — ФИКС (источник с нормальной
      атакой продлевается внахлёст в цель; через паузу легато не мостится)

  Слой «как сыграно» (<Offset>/<Duration> на нотах GP8) по умолчанию
  игнорируется: эталон экспорта — нотная запись. Сдвиги атак можно сохранить
  отдельным opt-in preserve_gp_played_offsets; parse_song предупреждает о нотах
  с |Offset| > 1/32.
"""
from __future__ import annotations

import copy
import re
import shutil
import warnings
import zipfile
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

import gp_import
import gp_to_shreddage as g

HOME = Path.home()
PND_AUDIO = HOME / "pnd_full.gp"
PND_NOAUDIO = HOME / "pnd_full_no_audio.gp"
TTN_AUDIO = HOME / "ttn_full.gp"
TTN_NOAUDIO = HOME / "ttn_no_audio.gp"

ALL_SAMPLES = [PND_AUDIO, PND_NOAUDIO, TTN_AUDIO, TTN_NOAUDIO]
NOAUDIO_SAMPLES = [PND_NOAUDIO, TTN_NOAUDIO]

from articulation_config import config_for_track_type

# Все keyswitch-ноты обоих инструментов (Hydra: 12..23, Darkwall: 0..7)
KS_NOTES = {
    int(spec["note"])
    for tt in ("GUITAR", "BASS")
    for spec in config_for_track_type(tt)["keyswitches"].values()
}
TICKS_PER_BAR_4_4 = g.TICKS_PER_BEAT * 4  # 3840


def _require(path: Path):
    if not path.exists():
        pytest.skip(f"образец не найден: {path}")


@lru_cache(maxsize=8)
def parse_cached(path_str: str):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return gp_import.parse_song(path_str)


def build_track(song, track):
    tt = g.resolve_track_type(track)
    if tt == "DRUMS":
        return g.build_drum_midi(song, track)
    if tt == "OTHER":
        return g.build_other_midi(song, track)
    return g.build_instrument_midi(song, track, tt)


def note_on_pitches(midi_track):
    """Питчи всех note_on (vel>0, без keyswitch) — для подсчёта/сравнения питчей.

    Считаем именно note_on (а не пары on/off): в унисонах/дублях одного питча
    в одном бите парность on/off теряется, но количество note_on корректно.
    """
    return [msg.note for msg in midi_track
            if msg.type == "note_on" and msg.velocity > 0 and msg.note not in KS_NOTES]


def note_events(midi_track):
    """Список нот (pitch, on_tick, off_tick) из MidiTrack, без keyswitch-нот."""
    abs_tick = 0
    active: dict[int, list[int]] = {}
    out = []
    for msg in midi_track:
        abs_tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and msg.note not in KS_NOTES:
            active.setdefault(msg.note, []).append(abs_tick)
        elif msg.type in ("note_off", "note_on") and (msg.type == "note_off" or msg.velocity == 0):
            if msg.note in KS_NOTES:
                continue
            stack = active.get(msg.note)
            if stack:
                on = stack.pop(0)
                out.append((msg.note, on, abs_tick))
    return out


def source_nontie_note_count(track):
    return sum(
        1
        for m in track.measures
        for v in m.voices
        for b in v.beats
        for n in b.notes
        if getattr(n.type, "name", "") != "tie"
    )


# --------------------------------------------------------------------------- #
#  Тест 1 — B1: лиги через тактовую черту держатся
# --------------------------------------------------------------------------- #
def test_tie_across_barline_pnd_solo_guitar():
    """Golden-кейс: Solo Guitar, D4 (pitch 62) тянется из такта 64 в такт 65.

    Источник: бар64 (normal 480т) -> бар65 (tie 480т) -> бар65 (tie 720т).
    Экспортированная нота обязана длиться сумму всех трёх (не обрываться
    на тактовой черте 64*3840=245760).
    """
    _require(PND_NOAUDIO)
    song = parse_cached(str(PND_NOAUDIO))
    track = next(t for t in song.tracks if t.name == "Solo Guitar")
    sp = {s.number: s.value for s in track.strings}

    # ожидаемую длительность считаем ДИНАМИЧЕСКИ из модели (не хардкод тиков)
    bar64 = track.measures[63].voices[0].beats
    d4_start_beat = next(b for b in bar64 if any(sp[n.string] + n.value == 62
                         and getattr(n.type, "name", "") != "tie" for n in b.notes))
    expected = d4_start_beat.duration.time
    for b in track.measures[64].voices[0].beats:  # бар 65 — хвост лиги
        if any(sp[n.string] + n.value == 62 and getattr(n.type, "name", "") == "tie"
               for n in b.notes):
            expected += b.duration.time
        else:
            break
    assert expected == 480 + 480 + 720  # sanity: как в расследовании

    mt, _ = build_track(song, track)
    barline = 64 * TICKS_PER_BAR_4_4  # 245760
    crossing = [(on, off) for (p, on, off) in note_events(mt)
                if p == 62 and on < barline <= off]
    assert crossing, "не найдена нота D4, начинающаяся до черты и звучащая через неё"
    on, off = crossing[0]
    assert off - on == expected, f"лига оборвана: длительность {off-on} != {expected}"
    assert off > barline, "нота обрывается ровно на тактовой черте (баг B1 вернулся)"


# --------------------------------------------------------------------------- #
#  Тест 2 — инвариант количества нот (на всех образцах, на каждый трек)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ALL_SAMPLES, ids=lambda p: p.name)
def test_note_count_invariant(path):
    _require(path)
    song = parse_cached(str(path))
    for track in song.tracks:
        # stats['notes'] = число реально эмитированных нот (без keyswitch/ties).
        # Не фильтруем по питчу: реальные ноты OTHER-треков попадают в 12..21.
        _, stats = build_track(song, track)
        expected = source_nontie_note_count(track)
        assert stats["notes"] == expected, (
            f"{path.name}/{track.name}: экспорт {stats['notes']} != источник {expected}"
        )


# --------------------------------------------------------------------------- #
#  Тест 3 — B3: питч берётся из string+fret, а НЕ из realValue (+24)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", NOAUDIO_SAMPLES, ids=lambda p: p.name)
def test_pitch_from_string_fret_not_realvalue(path):
    """Экспорт обязан считать питч как string_pitch+fret, а НЕ брать ApolloTab
    realValue (midi_pitch), который смещён на целое число октав (в pnd +24,
    в ttn +12 — зависит от октавного транспонирования трека)."""
    _require(path)
    song = parse_cached(str(path))
    guitar = next((t for t in song.tracks
                   if g.detect_track_type(t.name) == "GUITAR"), None)
    assert guitar is not None
    sp = {s.number: s.value for s in guitar.strings}

    computed = Counter()
    realvals = Counter()
    offsets = set()
    for m in guitar.measures:
        for v in m.voices:
            for b in v.beats:
                for n in b.notes:
                    if getattr(n.type, "name", "") == "tie":
                        continue
                    # эталон = string+fret; для гармоник — звучащая высота,
                    # сложенная октавами в диапазон артикуляции Harmonics
                    from articulation_config import config_for_track_type
                    c = g.shreddage_harmonic_pitch(n, sp, config_for_track_type("GUITAR"))
                    if c is None:
                        c = g.clamp_note(sp[n.string] + n.value)
                    computed[c] += 1
                    if n.realValue:
                        realvals[g.clamp_note(n.realValue)] += 1
                        offsets.add(n.realValue - (sp[n.string] + n.value))

    # realValue действительно смещён на целые октавы (12/24), и НЕ равен string+fret
    assert offsets and all(o % 12 == 0 and o != 0 for o in offsets)

    exported = Counter(note_on_pitches(build_track(song, guitar)[0]))
    assert exported == computed, "экспорт не совпал с string+fret"
    assert exported != realvals, "экспорт совпал с realValue — берётся не тот питч"


# --------------------------------------------------------------------------- #
#  Тест 4 — B6/B9: длина такта == каноническая (кроме округления триолей)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", NOAUDIO_SAMPLES, ids=lambda p: p.name)
def test_measure_length_canonical(path):
    _require(path)
    song = parse_cached(str(path))
    TUPLET_TOL = 40  # B9: смешанные 32/16 триоли дают до +40 тиков округления
    phantom_like = 0
    for track in song.tracks:
        for mi, m in enumerate(track.measures, 1):
            canon = g.measure_length_ticks(m)
            for v in m.voices:
                s = sum(b.duration.time for b in v.beats)
                # фантомные паузы (-1 голоса) давали +2880 — их быть не должно
                assert s - canon <= TUPLET_TOL, (
                    f"{path.name}/{track.name} бар {mi}: сумма {s} >> канон {canon} "
                    "(вернулись фантомные паузы B6?)"
                )
                assert s >= canon - TUPLET_TOL
                if s != canon:
                    phantom_like += 1
    # для pnd — идеально ровно; для ttn допускаем ровно 2 триольных такта (B9)
    if path == PND_NOAUDIO:
        assert phantom_like == 0


# --------------------------------------------------------------------------- #
#  Тест 5 — B2: CLI (parse_song) даёт тот же результат, что прямой билд (web)
# --------------------------------------------------------------------------- #
def test_cli_matches_web_path(tmp_path):
    """main() теперь идёт через parse_song -> те же ноты, что и веб-путь.

    Проверяем на GP8-файле (ttn), который старый CLI (guitarpro.parse) не осилил бы.
    """
    _require(TTN_NOAUDIO)
    src = tmp_path / "ttn_no_audio.gp"
    shutil.copy(TTN_NOAUDIO, src)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        g.main(["prog", str(src)])  # B2: не должен падать на GP8

    out_dir = tmp_path / "ttn_no_audio_midi"
    assert out_dir.is_dir()
    mids = list(out_dir.glob("*.mid"))
    assert mids, "CLI не создал ни одного .mid"

    # ноты из CLI-файла == ноты из прямого билда того же трека (веб-путь)
    from mido import MidiFile
    song = parse_cached(str(TTN_NOAUDIO))
    track = next(t for t in song.tracks if t.name == "Solo Guitar")
    web_events = note_events(build_track(song, track)[0])
    cli_mid = out_dir / "Solo Guitar.mid"
    assert cli_mid.exists()
    cli_events = note_events(MidiFile(str(cli_mid)).tracks[0])
    assert cli_events == web_events


# --------------------------------------------------------------------------- #
#  Тест 6/7 — B8: переменный темп -> кусочно-постоянная темп-карта в MIDI
# --------------------------------------------------------------------------- #
def _gpif_tempo_automations(path: Path):
    with zipfile.ZipFile(path) as zf:
        gpif = zf.read("Content/score.gpif").decode("utf-8", "replace")
    autos = re.findall(r"<Automation>(.*?)</Automation>", gpif, re.S)
    out = []
    for a in autos:
        if "<Type>Tempo</Type>" not in a:
            continue
        bar = int(re.search(r"<Bar>(\d+)</Bar>", a).group(1))
        val = float(re.search(r"<Value>([\d.]+)", a).group(1))
        out.append((bar, val))
    return out


def _set_tempo_events(midi_track):
    from mido import tempo2bpm
    abs_tick, out = 0, []
    for msg in midi_track:
        abs_tick += msg.time
        if msg.type == "set_tempo":
            out.append((abs_tick, round(tempo2bpm(msg.tempo), 3)))
    return out


def test_multi_tempo_set_tempo_events_ttn():
    """ttn: 3 темповые автоматизации (0:122.3, 41:125, 99:126.2) должны стать
    3 Set Tempo в MIDI на канонических тиках 0 / 41*3840 / 99*3840."""
    _require(TTN_NOAUDIO)
    autos = _gpif_tempo_automations(TTN_NOAUDIO)
    assert [b for b, _ in autos] == [0, 41, 99]

    song = parse_cached(str(TTN_NOAUDIO))
    track = song.tracks[0]
    setts = _set_tempo_events(build_track(song, track)[0])

    # дедупликация: бар-0 mixTableChange совпадает с базовым темпом -> один тик 0
    assert len(setts) == len(autos)
    for (tick, bpm), (bar, val) in zip(setts, autos):
        assert tick == bar * TICKS_PER_BAR_4_4
        assert abs(bpm - val) < 0.05


def test_multi_tempo_absolute_time_is_piecewise_ttn():
    """Абсолютное время старта такта 99 (тик 380160) считается ПОКУСОЧНО
    (по каждому темповому участку), а НЕ по единому темпу на всю песню.

    Если бы код брал только первый темп (122.3) на весь трек — время бы
    разъехалось на ~2.5 c к такту 99. На pnd баг не проявлялся: там темп один.
    """
    _require(TTN_NOAUDIO)
    song = parse_cached(str(TTN_NOAUDIO))
    setts = _set_tempo_events(build_track(song, song.tracks[0])[0])

    def tick_to_ms(target, tempo_map):
        # tempo_map: [(tick, bpm)], кусочно-постоянно
        ms, cur_tick, cur_bpm = 0.0, 0, tempo_map[0][1]
        for tick, bpm in tempo_map[1:]:
            if target <= tick:
                break
            ms += (tick - cur_tick) / g.TICKS_PER_BEAT * 60000.0 / cur_bpm
            cur_tick, cur_bpm = tick, bpm
        ms += (target - cur_tick) / g.TICKS_PER_BEAT * 60000.0 / cur_bpm
        return ms

    bar99_tick = 99 * TICKS_PER_BAR_4_4
    piecewise = tick_to_ms(bar99_tick, setts)

    # эталон вручную: бары 0..40 @122.3 (41 бар) + бары 41..98 @125 (58 баров)
    manual = (41 * 4 * 60000.0 / 122.3) + (58 * 4 * 60000.0 / 125.0)
    assert abs(piecewise - manual) < 1.0

    # наивный единый темп (122.3 на всё) дал бы заметно другое время
    naive = 99 * 4 * 60000.0 / 122.3
    assert abs(piecewise - naive) > 1000.0  # расхождение > 1 c — темп-карта работает


# --------------------------------------------------------------------------- #
#  Тест 8 — B4: мультиголос детектируется и предупреждается (+ xfail на тайминг)
# --------------------------------------------------------------------------- #
def _make_multivoice_gp(src: Path, dst: Path) -> str:
    """Синтетический мультиголосый фикстур: у ОДНОГО одноголосого такта дублируем
    его голос в слот 2 (<Voices>N -1 -1 -1> -> <Voices>N N -1 -1>).

    Патчим такт, чей голос СОДЕРЖИТ НОТЫ (иначе дубль паузы обрежется фиксом B6
    и мультиголос не проявится). Возвращает id продублированного голоса.
    """
    with zipfile.ZipFile(src) as zf:
        names = zf.namelist()
        data = {n: zf.read(n) for n in names}
    gpif = data["Content/score.gpif"].decode("utf-8")

    # beat_id -> есть ли <Notes> (реальные ноты)
    beat_has_notes = {
        int(m.group(1)): ("<Notes>" in m.group(2))
        for m in re.finditer(r'<Beat id="(\d+)">(.*?)</Beat>', gpif, re.S)
    }
    # voice_id -> число бит с нотами
    voice_notes = {}
    for m in re.finditer(r'<Voice id="(\d+)">.*?<Beats>([\d ]+)</Beats>', gpif, re.S):
        vid = m.group(1)
        voice_notes[vid] = sum(beat_has_notes.get(int(b), False) for b in m.group(2).split())

    # среди одноголосых тактов берём тот, чей голос самый «нотонасыщенный»
    candidates = [(voice_notes.get(mo.group(1), 0), mo.group(1))
                  for mo in re.finditer(r"<Voices>(\d+) -1 -1 -1</Voices>", gpif)]
    candidates.sort(reverse=True)
    assert candidates and candidates[0][0] > 0, "нет одноголосого такта с нотами"
    target = candidates[0][1]

    patched, n = re.subn(rf"<Voices>{target} -1 -1 -1</Voices>",
                         f"<Voices>{target} {target} -1 -1</Voices>", gpif, count=1)
    assert n == 1
    data["Content/score.gpif"] = patched.encode("utf-8")
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            zf.writestr(name, data[name])
    return target


def test_multivoice_detection_and_warning(tmp_path):
    _require(TTN_NOAUDIO)
    fx = tmp_path / "multivoice.gp"
    _make_multivoice_gp(TTN_NOAUDIO, fx)

    assert gp_import._count_gpif_multivoice_bars(fx) >= 1

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        gp_import.parse_song(str(fx))
    assert any("голос" in str(x.message) for x in w), "нет предупреждения о мультиголосе"


# --------------------------------------------------------------------------- #
#  Хелперы для синтетических GPIF-фикстур (патч свойств конкретной ноты)
# --------------------------------------------------------------------------- #
def _gpif_note_chain(gpif_root):
    """id-цепочки GPIF: note->beat->voice->bar->(имя колонки, masterbar_idx).

    Колонки <Bars> идут ПО НОТОНОСЦАМ (B14): для мульти-стафф треков имя
    колонки = "<имя> (Staff N)" — так же, как их разворачивает gp_import.
    """
    notes_of_beat = {b.get("id"): (b.findtext("Notes") or "").split()
                     for b in gpif_root.findall("Beats/Beat")}
    beats_of_voice = {v.get("id"): (v.findtext("Beats") or "").split()
                      for v in gpif_root.findall("Voices/Voice")}
    voices_of_bar = {bar.get("id"): [v for v in (bar.findtext("Voices") or "").split() if v != "-1"]
                     for bar in gpif_root.findall("Bars/Bar")}
    col_names = []
    for t in gpif_root.findall("Tracks/Track"):
        name = t.findtext("Name")
        staffs = t.findall("Staves/Staff")
        if len(staffs) <= 1:
            col_names.append(name)
        else:
            col_names.extend(f"{name} (Staff {i + 1})" for i in range(len(staffs)))
    out = []  # (column_name, masterbar_idx, note_id)
    for mi, mb in enumerate(gpif_root.findall("MasterBars/MasterBar")):
        for ti, bar_id in enumerate((mb.findtext("Bars") or "").split()):
            for vid in voices_of_bar.get(bar_id, []):
                for bid in beats_of_voice.get(vid, []):
                    for nid in notes_of_beat.get(bid, []):
                        out.append((col_names[ti] if ti < len(col_names) else None, mi, nid))
    return out


def _patch_gp_note(src: Path, dst: Path, track_name: str, xml_snippet: str,
                   into_properties: bool, min_bar: int = 0) -> int:
    """Скопировать .gp, добавив первой ноте трека track_name (в такте >= min_bar)
    XML-сниппет: в <Properties> (into_properties=True) или прямым ребёнком
    <Note>. Возвращает 0-based индекс такта пропатченной ноты."""
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(src) as zf:
        names = zf.namelist()
        data = {n: zf.read(n) for n in names}
    gpif = data["Content/score.gpif"].decode("utf-8")
    root = ET.fromstring(gpif)
    # лиги искажают gate/velocity-проверки: берём ноту без <Tie>
    tied_ids = {n.get("id") for n in root.findall("Notes/Note") if n.find("Tie") is not None}
    target = next((mi, nid) for tname, mi, nid in _gpif_note_chain(root)
                  if tname == track_name and mi >= min_bar and nid not in tied_ids)
    bar_idx, note_id = target
    if into_properties:
        pattern = rf'(<Note id="{note_id}">.*?<Properties>)'
    else:
        pattern = rf'(<Note id="{note_id}">)'
    patched, n = re.subn(pattern, rf"\1{xml_snippet}", gpif, count=1, flags=re.S)
    assert n == 1, f"нота id={note_id} не пропатчилась"
    data["Content/score.gpif"] = patched.encode("utf-8")
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            zf.writestr(name, data[name])
    return bar_idx


def ks_events(midi_track, ks_note):
    """Тики note_on конкретной keyswitch-ноты."""
    abs_tick, out = 0, []
    for msg in midi_track:
        abs_tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and msg.note == ks_note:
            out.append(abs_tick)
    return out


# --------------------------------------------------------------------------- #
#  Тест 9 — Palm Mute в РИТМ-СЕКЦИИ (Hydra C#-1 = MIDI 13), реальный такт
# --------------------------------------------------------------------------- #
def test_palm_mute_rhythm_guitar_pnd():
    """pnd, Rhytm Guitar: P.M. начинается в такте 9 (1-based). Экспорт обязан
    переключить артикуляцию keyswitch-ем 13 (C#-1) не позже начала первого
    P.M.-бита и вернуться в sustain (12) в конце трека."""
    _require(PND_NOAUDIO)
    song = parse_cached(str(PND_NOAUDIO))
    track = next(t for t in song.tracks if t.name == "Rhytm Guitar")

    # первый P.M.-бит из модели (реальный образец, такт 9)
    first_pm_tick, pm_bar = None, None
    for measure, voice, vi, bi, beat, mst, start_tick, dur in g.iter_voice_beats_with_canonical_ticks(track):
        if any(n.effect.palmMute for n in beat.notes):
            first_pm_tick = start_tick
            pm_bar = start_tick // TICKS_PER_BAR_4_4 + 1
            break
    assert first_pm_tick is not None, "в Rhytm Guitar не найден P.M."
    assert pm_bar == 9  # golden: такт 9 (1-based), сверено с score.gpif

    mt, stats = g.build_instrument_midi(song, track, "GUITAR")
    pm_ks = ks_events(mt, 13)                    # C#-1 = palm mute (Hydra)
    assert pm_ks, "keyswitch Palm Mute (13) не эмитирован"
    assert pm_ks[0] <= first_pm_tick, "KS palm mute пришёл ПОЗЖЕ первого P.M.-бита"
    assert first_pm_tick - pm_ks[0] <= 200, "KS palm mute слишком рано (не lead-окно)"
    assert ks_events(mt, 12), "нет возврата в sustain (12)"


# --------------------------------------------------------------------------- #
#  Тест 10 — Tapping в СОЛО (Hydra G#-1 = MIDI 20), синтетический такт
#  (в реальных образцах tap-разметки нет — проверено по всему score.gpif)
# --------------------------------------------------------------------------- #
def test_tapping_solo_guitar_keyswitch(tmp_path):
    """ВАЖНО: GPIF переиспользует Note id (flyweight), поэтому патч одного id
    помечает все биты, ссылающиеся на него — для теста это допустимо (в
    реальных файлах GP материализует отдельные id при разных свойствах)."""
    _require(TTN_NOAUDIO)
    fx = tmp_path / "tapped.gp"
    _patch_gp_note(
        TTN_NOAUDIO, fx, "Solo Guitar",
        '<Property name="Tapped"><Enable/></Property>',
        into_properties=True, min_bar=10,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        song = gp_import.parse_song(str(fx))
    track = next(t for t in song.tracks if t.name == "Solo Guitar")

    tapped_ticks = [
        start_tick
        for measure, voice, vi, bi, beat, mst, start_tick, dur in g.iter_voice_beats_with_canonical_ticks(track)
        for n in beat.notes if n.effect.tapping
    ]
    assert tapped_ticks, "Tapped-свойство не дошло до модели (sidecar)"

    mt, _ = g.build_instrument_midi(song, track, "GUITAR")
    tap_ks = ks_events(mt, 20)                   # G#-1 = tapping (Hydra)
    assert tap_ks, "keyswitch Tapping (20) не эмитирован"
    first_tap = min(tapped_ticks)
    assert any(first_tap - 200 <= t <= first_tap for t in tap_ks), (
        "KS tapping не пришёл перед первой tap-нотой"
    )


# --------------------------------------------------------------------------- #
#  Тест 11 — Darkwall: октава keyswitch-ей C-2 (0..7), НЕ октава Hydra
# --------------------------------------------------------------------------- #
def test_darkwall_keyswitch_octave(tmp_path):
    """Palm mute на басу = E-2 (MIDI 4) по мануалу Darkwall 3.5, а не C#-1 (13).
    Возврат в sustain = C-2 (MIDI 0). Фикстура: P.M. на первой ноте баса ttn."""
    _require(TTN_NOAUDIO)
    fx = tmp_path / "bass_pm.gp"
    _patch_gp_note(
        TTN_NOAUDIO, fx, "Bass",
        '<Property name="PalmMuted"><Enable/></Property>',
        into_properties=True, min_bar=5,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        song = gp_import.parse_song(str(fx))
    track = next(t for t in song.tracks if t.name == "Bass")
    assert g.resolve_track_type(track) == "BASS"

    mt, stats = g.build_instrument_midi(song, track, "BASS")
    assert ks_events(mt, 4), "нет keyswitch Palm Mute Darkwall (E-2 = 4)"
    assert not ks_events(mt, 13), "использована октава Hydra (13) вместо Darkwall (4)"
    assert ks_events(mt, 0), "нет возврата в sustain Darkwall (C-2 = 0)"


# --------------------------------------------------------------------------- #
#  Тест 12 — Барабаны: GM -> Shreddage Drums (remap без ложного Side Stick warning)
# --------------------------------------------------------------------------- #
def test_drums_mapping_pnd_side_stick():
    """pnd/Drumkit: Side Stick (GM 37) экспортируется как 37 без ложного
    предупреждения: маппинг пользователя подтверждает C#1 = cross-stick."""
    _require(PND_NOAUDIO)
    song = parse_cached(str(PND_NOAUDIO))
    track = next(t for t in song.tracks if t.name == "Drumkit")
    assert g.resolve_track_type(track) == "DRUMS"

    mt, stats = g.build_drum_midi(song, track)
    pitches = Counter(msg.note for msg in mt
                      if msg.type == "note_on" and msg.velocity > 0)
    assert pitches[37] > 0, "Side Stick (37) пропал из экспорта"
    assert not any("Side Stick" in w for w in stats["warnings"]), "вернулось ложное предупреждение о Side Stick"
    assert stats["notes"] == source_nontie_note_count(track)


def test_drums_mapping_ttn_remap():
    """ttn/Drums: GM High Tom (50) -> 48 (в Shreddage 50 = Crash 1 Choke!),
    GM Ride 2 (59) -> 51 (Ride 1 Edge). Нот 50/59 в выходе быть не должно."""
    _require(TTN_NOAUDIO)
    song = parse_cached(str(TTN_NOAUDIO))
    track = next(t for t in song.tracks if t.name == "Drums")

    mt, _ = g.build_drum_midi(song, track)
    pitches = Counter(msg.note for msg in mt
                      if msg.type == "note_on" and msg.velocity > 0)
    assert pitches[48] > 0, "High Tom не переехал на 48"
    assert pitches[50] == 0, "GM 50 попал в выход: у Shreddage это Crash 1 Choke"
    assert pitches[51] > 0, "Ride 2 не переехал на 51"
    assert pitches[59] == 0, "GM 59 (Ride 2) не переведён"


def test_drums_flam_formula():
    """Формула флэмов из конфига: base_tom_note + 24 (по мануалу)."""
    from articulation_config import config_for_track_type, flam_note
    cfg = config_for_track_type("DRUMS")
    assert flam_note(cfg, 41) == 65
    assert flam_note(cfg, 47) == 71
    # флэм-нота, пришедшая GM-входом, проходит как валидная
    from articulation_config import drum_note_out
    out, warn = drum_note_out(cfg, 65)
    assert out == 65 and warn is None


# --------------------------------------------------------------------------- #
#  Тест 13 — Hydra Pitch Bend Range = 7 st, Darkwall = 2 st
# --------------------------------------------------------------------------- #
def test_pitch_bend_range_matches_instrument_configs():
    """Hydra использует измеренный range 7 для бендов до 6 st без потолка;
    Darkwall сохраняет пока не проверенный дефолт 2 st."""
    from articulation_config import config_for_track_type
    assert float(config_for_track_type("GUITAR")["pitch_bend_range"]) == 7.0
    assert float(config_for_track_type("BASS")["pitch_bend_range"]) == 2.0

    _require(PND_NOAUDIO)
    song = parse_cached(str(PND_NOAUDIO))
    track = next(t for t in song.tracks if t.name == "Solo Guitar")
    mt, _ = g.build_instrument_midi(song, track, "GUITAR")

    msgs = list(mt)
    rpn_idx = [i for i, m in enumerate(msgs)
               if m.type == "control_change" and m.control == 101 and m.value == 0]
    assert rpn_idx, "нет RPN 101=0 (Pitch Bend Range)"
    i = rpn_idx[0]
    data6 = next(m for m in msgs[i:i + 6]
                 if m.type == "control_change" and m.control == 6)
    assert data6.value == 7, f"RPN Pitch Bend Range = {data6.value}, ожидалось 7"

    # Партитура содержит бенды до 6 st: при range=7 они не должны упираться
    # в 8191 и превращаться в полку/скачок.
    max_pw = max((m.pitch for m in msgs if m.type == "pitchwheel"), default=0)
    assert 0 < max_pw < 8191, "Hydra bend снова упирается в потолок"


# --------------------------------------------------------------------------- #
#  Тест 14 — B12: Категория A на не-Shreddage треках (staccato/accent/hairpin)
# --------------------------------------------------------------------------- #
def test_category_a_staccato_accent_on_other_track(tmp_path):
    """Staccato -> gate ~50%, Accent -> velocity boost на OTHER-треке (Piano).
    Фикстура: <Accent>9</Accent> (bit0 staccato + bit3 normal accent)."""
    _require(TTN_NOAUDIO)
    track_name = "Piano (Staff 1)"      # мульти-стафф трек развёрнут (B14)
    fx = tmp_path / "piano_stacc.gp"
    _patch_gp_note(
        TTN_NOAUDIO, fx, track_name,
        "<Accent>9</Accent>",
        into_properties=False, min_bar=20,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        song_fx = gp_import.parse_song(str(fx))
        song_ref = gp_import.parse_song(str(TTN_NOAUDIO))

    def events_for(song):
        track = next(t for t in song.tracks if t.name == track_name)
        assert g.resolve_track_type(track) == "OTHER"
        return g.build_other_midi(song, track)[0]

    ref = {(p, on): (off, ) for p, on, off in note_events(events_for(song_ref))}
    changed = []
    for p, on, off in note_events(events_for(song_fx)):
        r = ref.get((p, on))
        if r is not None and r[0] != off:
            changed.append((p, on, off, r[0]))
    assert changed, "staccato не укоротил ноту на OTHER-треке (B12 вернулся)"
    # flyweight-патч задевает и унисоны (пары on/off могут перепутаться),
    # поэтому допуск мягкий: нота стала заметно короче номинала
    p, on, off, ref_off = changed[0]
    assert (off - on) <= (ref_off - on) * 0.75, "gate time не сработал"
    assert any((o - onn) <= (r - onn) * 0.55 for _, onn, o, r in changed), (
        "ни одна нота не укорочена до ~50% (gate)"
    )

    # accent: у какой-то ноты velocity выросла ровно на ACCENT_VEL_BOOST
    def note_on_vels(song):
        abs_tick, out = 0, {}
        track = next(t for t in song.tracks if t.name == track_name)
        for msg in g.build_other_midi(song, track)[0]:
            abs_tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                out.setdefault((msg.note, abs_tick), []).append(msg.velocity)
        return out
    vels_fx, vels_ref = note_on_vels(song_fx), note_on_vels(song_ref)
    boosted = [
        key for key, vlist in vels_fx.items()
        if key in vels_ref and sorted(vlist) != sorted(vels_ref[key])
        and max(vlist) == g.clamp_vel(max(vels_ref[key]) + g.ACCENT_VEL_BOOST)
    ]
    assert boosted, "accent не поднял velocity ни одной ноты (B12)"


def test_category_a_hairpin_cc11_on_other_track():
    """ttn/FX (Staff 1|2): Decrescendo (такт 9) -> кривая CC11 вниз + сброс 127."""
    _require(TTN_NOAUDIO)
    song = parse_cached(str(TTN_NOAUDIO))
    fx_tracks = [t for t in song.tracks if t.name.startswith("FX")]
    assert fx_tracks, "FX-треки не найдены (B14: ожидались 'FX (Staff N)')"

    found = False
    for track in fx_tracks:
        assert g.resolve_track_type(track) == "OTHER"
        mt, _ = g.build_other_midi(song, track)
        abs_tick, cc11 = 0, []
        for msg in mt:
            abs_tick += msg.time
            if msg.type == "control_change" and msg.control == 11:
                cc11.append((abs_tick, msg.value))
        if not cc11:
            continue
        assert cc11[0][1] == 127, "нет базового CC11=127"
        if min(v for _, v in cc11) < 100:
            assert any(v == 127 for t, v in cc11[1:]), "нет сброса CC11 в 127"
            found = True
    assert found, "ни на одном FX-стаффе hairpin не дал CC11-спуска"


# --------------------------------------------------------------------------- #
#  Тест 17 — B15: легато источник->цель (реальные такты 70-71 pnd/Solo Guitar)
# --------------------------------------------------------------------------- #
def test_legato_origin_destination_b15():
    """GP ставит флаг hammer на ноту-ИСТОЧНИК. До фикса конвертер глушил сам
    источник (velocity=1 -> Shreddage «не слышит» ноту) и продлевал ноту ПЕРЕД
    ним (D4 из пробежки такта 70 дронила 1.7 доли поверх такта 71).

    Golden-кейс: такт 71, C5 (половинная, из неё pull-off в A4):
      - C5 звучит с нормальной velocity (не 1);
      - D4 (последняя 32-я такта 70) НЕ продлевается за свою длительность;
      - C5 продлевается внахлёст (~40 мс) в атаку A4 — legato для Shreddage.
    """
    _require(PND_NOAUDIO)
    song = parse_cached(str(PND_NOAUDIO))
    track = next(t for t in song.tracks if t.name == "Solo Guitar")

    bar70, bar71 = 69 * TICKS_PER_BAR_4_4, 70 * TICKS_PER_BAR_4_4
    # sanity по модели: C5 на 1440 такта 71 — hammer/pull-источник
    c5 = next(n for m, v, vi, bi, b, mst, st, dur in g.iter_voice_beats_with_canonical_ticks(track)
              if st == bar71 + 1440 for n in b.notes)
    assert c5.effect.hammer, "разметка изменилась: C5 больше не hop-источник"

    events = note_events(g.build_instrument_midi(song, track, "GUITAR")[0])

    d4 = [e for e in events if e[0] == 62 and e[1] == bar70 + 3720]
    assert d4, "не найдена D4 в конце такта 70"
    assert d4[0][2] - d4[0][1] <= 240, (
        f"D4 продлена до {d4[0][2] - d4[0][1]} тиков — дрон вместо паузы (B15 вернулся)"
    )

    c5_ev = [e for e in events if e[0] == 72 and e[1] == bar71 + 1440]
    assert c5_ev, "не найдена C5 в такте 71"
    on, off = c5_ev[0][1], c5_ev[0][2]
    a4_start = bar71 + 3360
    assert off > a4_start, "C5 не перекрывает атаку A4 — легато не сработает"
    assert off - a4_start <= 240, "перекрытие C5->A4 слишком длинное"

    abs_tick = 0
    c5_vel = None
    for msg in g.build_instrument_midi(song, track, "GUITAR")[0]:
        abs_tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and msg.note == 72 and abs_tick == on:
            c5_vel = msg.velocity
            break
    assert c5_vel and c5_vel > 1, f"C5 беззвучна (velocity={c5_vel}) — B15 вернулся"


def _mini_song(beats_spec):
    """Минимальный GPSong с одним треком/тактом из спецификации битов:
    [(dur_value, [(string, fret, hammer)]), ...] — ноты None = пауза."""
    from guitarpro.models import NoteType

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
    beats = []
    for dur_value, notes_spec in beats_spec:
        dur = GPDuration(value=dur_value, time=int(960 * 4 / dur_value))
        notes = []
        for string, fret, hammer in (notes_spec or []):
            notes.append(GPNote(string=string, value=fret, velocity=95,
                                effect=GPNoteEffect(hammer=hammer),
                                type=NoteType.normal, realValue=0))
        beats.append(GPBeat(start=0, duration=dur, notes=notes,
                            effect=GPBeatEffect(), status=GPBeatStatus("normal")))
    header = GPMeasureHeader(timeSignature=GPTimeSignature(4, GPDenominator(4)))
    track = GPTrack(name="Test Guitar", strings=[GPString(0, 64)],
                    channel=GPChannel(instrument=30),
                    measures=[GPMeasure(start=960, header=header,
                                        voices=[GPVoice(beats=beats)])])
    return GPSong(title="", artist="", album="", tempo=120.0,
                  tracks=[track], measureHeaders=[header]), track


def test_legato_not_bridged_across_rest_b15():
    """Hammer-источник, за которым ПАУЗА до следующей ноты на той же струне,
    НЕ продлевается через паузу (легато прервано). А смежная пара
    источник->цель получает перекрытие."""
    # такт: нота(hammer) 1/4, ПАУЗА 1/4, нота 1/4, ПАУЗА 1/4
    song, track = _mini_song([
        (4, [(0, 5, True)]), (4, None), (4, [(0, 7, False)]), (4, None),
    ])
    events = note_events(g.build_instrument_midi(song, track, "GUITAR")[0])
    first = next(e for e in events if e[1] == 0)
    assert first[2] == 960, f"источник продлён через паузу: off={first[2]} (B15)"

    # смежная пара: нота(hammer) 1/4 + нота 1/4 -> перекрытие ~LEGATO_OVERLAP_MS
    song2, track2 = _mini_song([
        (4, [(0, 5, True)]), (4, [(0, 7, False)]), (2, None),
    ])
    events2 = note_events(g.build_instrument_midi(song2, track2, "GUITAR")[0])
    first2 = next(e for e in events2 if e[1] == 0)
    overlap = g.ms_to_ticks(g.LEGATO_OVERLAP_MS, 120.0)
    assert first2[2] == 960 + overlap, (
        f"нет перекрытия источник->цель: off={first2[2]}, ожидалось {960 + overlap}")


def _life_cfg(*, auto_probability=1.0, fret_probability=1.0):
    """Hydra-конфиг с детерминированными вероятностями для unit-тестов."""
    cfg = copy.deepcopy(config_for_track_type("GUITAR"))
    cfg["performance_life"] = {
        "auto_sustain_vibrato": {
            "min_note_ms": 900,
            "delay_ms": [250, 500],
            "ramp_ms": 180,
            "release_ms": 120,
            "amount": [18, 34],
            "probability": auto_probability,
            "track_name_keywords": ["solo", "lead"],
        },
        "fret_noise_on_hand_shift": {
            "min_fret_shift": 4,
            "probability": fret_probability,
        },
    }
    return cfg


def _absolute_cc(midi_track, control):
    tick = 0
    out = []
    for msg in midi_track:
        tick += msg.time
        if msg.type == "control_change" and msg.control == control:
            out.append((tick, msg.value))
    return out


def test_auto_sustain_vibrato_adds_delayed_cc1_only_when_enabled():
    """Длинный одиночный sustain получает мягкую CC1-огибающую по opt-in."""
    song, track = _mini_song([(1, [(0, 5, False)])])
    track.name = "Solo Guitar"

    plain, plain_stats = g.build_instrument_midi(song, track, "GUITAR", cfg=_life_cfg())
    alive, alive_stats = g.build_instrument_midi(
        song, track, "GUITAR", cfg=_life_cfg(),
        auto_sustain_vibrato=True, performance_seed=17,
    )

    assert _absolute_cc(plain, g.VIBRATO_CC) == []
    cc1 = _absolute_cc(alive, g.VIBRATO_CC)
    assert cc1, "opt-in auto vibrato не добавил CC1"
    assert cc1[0][0] >= g.ms_to_ticks(250, song.tempo)
    assert cc1[-1] == (3840, 0)
    assert plain_stats.get("auto_vibrato_notes", 0) == 0
    assert alive_stats["auto_vibrato_notes"] == 1


def test_auto_sustain_vibrato_skips_chords():
    """Канальный CC1 нельзя применять к аккорду: все струны качались бы синхронно."""
    from gp_import import GPString

    song, track = _mini_song([(1, [(0, 5, False), (1, 7, False)])])
    track.name = "Lead Guitar"
    track.strings.append(GPString(1, 59))

    midi_track, stats = g.build_instrument_midi(
        song, track, "GUITAR", cfg=_life_cfg(),
        auto_sustain_vibrato=True, performance_seed=17,
    )

    assert _absolute_cc(midi_track, g.VIBRATO_CC) == []
    assert stats["auto_vibrato_notes"] == 0


def test_auto_sustain_vibrato_skips_non_solo_guitar_tracks():
    song, track = _mini_song([(1, [(0, 5, False)])])
    track.name = "Rhythm Guitar"

    midi_track, stats = g.build_instrument_midi(
        song, track, "GUITAR", cfg=_life_cfg(),
        auto_sustain_vibrato=True, performance_seed=17,
    )

    assert _absolute_cc(midi_track, g.VIBRATO_CC) == []
    assert stats["auto_vibrato_notes"] == 0


def test_auto_sustain_vibrato_preserves_explicit_gp_vibrato_without_duplicate():
    song, track = _mini_song([(1, [(0, 5, False)])])
    track.name = "Solo Guitar"
    track.measures[0].voices[0].beats[0].notes[0].effect.vibrato = True

    midi_track, stats = g.build_instrument_midi(
        song, track, "GUITAR", cfg=_life_cfg(),
        auto_sustain_vibrato=True, performance_seed=17,
    )

    assert _absolute_cc(midi_track, g.VIBRATO_CC)
    assert stats["auto_vibrato_notes"] == 0


def test_auto_sustain_vibrato_skips_short_notes():
    song, track = _mini_song([
        (8, [(0, 5, False)]),
        (8, None), (4, None), (2, None),
    ])
    track.name = "Solo Guitar"

    midi_track, stats = g.build_instrument_midi(
        song, track, "GUITAR", cfg=_life_cfg(),
        auto_sustain_vibrato=True, performance_seed=17,
    )

    assert _absolute_cc(midi_track, g.VIBRATO_CC) == []
    assert stats["auto_vibrato_notes"] == 0


def test_fret_noise_emitted_only_for_opt_in_large_hand_shift():
    song, track = _mini_song([
        (4, [(0, 2, False)]),
        (4, [(0, 8, False)]),
        (2, None),
    ])
    cfg = _life_cfg()
    fx_note = cfg["fx_keyswitches"]["fret_noise"]

    plain, plain_stats = g.build_instrument_midi(song, track, "GUITAR", cfg=cfg)
    noisy, noisy_stats = g.build_instrument_midi(
        song, track, "GUITAR", cfg=cfg,
        fret_noise_on_hand_shift=True, performance_seed=23,
    )

    assert ks_events(plain, fx_note) == []
    expected_tick = 960 - g.ms_to_ticks(g.KS_LEAD_MS, song.tempo)
    assert ks_events(noisy, fx_note) == [expected_tick]
    assert plain_stats["fret_noise_events"] == 0
    assert noisy_stats["fret_noise_events"] == 1
    assert noisy_stats["ks"] == plain_stats["ks"] + 1


def test_fret_noise_skips_small_position_changes():
    song, track = _mini_song([
        (4, [(0, 4, False)]),
        (4, [(0, 6, False)]),
        (2, None),
    ])
    cfg = _life_cfg()
    midi_track, stats = g.build_instrument_midi(
        song, track, "GUITAR", cfg=cfg,
        fret_noise_on_hand_shift=True, performance_seed=23,
    )

    assert ks_events(midi_track, cfg["fx_keyswitches"]["fret_noise"]) == []
    assert stats["fret_noise_events"] == 0


def test_hydra_config_defines_opt_in_performance_life_profiles():
    cfg = config_for_track_type("GUITAR")
    life = cfg["performance_life"]
    assert 0.0 < life["auto_sustain_vibrato"]["probability"] < 1.0
    assert life["auto_sustain_vibrato"]["track_name_keywords"]
    assert life["fret_noise_on_hand_shift"]["min_fret_shift"] >= 3
    assert 0.0 < life["fret_noise_on_hand_shift"]["probability"] < 1.0

#  Тест 16 — B14: мульти-стафф треки не смещают последующие треки
# --------------------------------------------------------------------------- #
def test_multistaff_tracks_not_shifted_b14():
    """ttn: FX и Piano имеют по 2 нотоносца. До фикса ApolloTab считал
    «колонка Bars = трек», из-за чего 'Solo Guitar' получал партию Clean
    Guitar, 'Piano' — партию Rhytm Guitar (с 1500+ P.M.), а настоящий Piano
    терялся. После фикса мульти-стафф треки развёрнуты в '<имя> (Staff N)'."""
    _require(TTN_NOAUDIO)
    song = parse_cached(str(TTN_NOAUDIO))
    names = [t.name for t in song.tracks]
    assert names == [
        "Drums", "FX (Staff 1)", "FX (Staff 2)", "Bass", "Voice",
        "Clean Guitar", "Solo Guitar", "Rhytm Guitar",
        "Piano (Staff 1)", "Piano (Staff 2)",
    ]

    def pm_count(track_name):
        track = next(t for t in song.tracks if t.name == track_name)
        return sum(1 for m in track.measures for v in m.voices for b in v.beats
                   for n in b.notes if n.effect.palmMute)

    # реальный Rhytm Guitar — с массивной P.M.-разметкой (раньше она
    # оказывалась в треке с именем 'Piano')
    assert pm_count("Rhytm Guitar") > 1000, "Rhytm Guitar получил чужую партию (B14)"
    assert pm_count("Piano (Staff 1)") + pm_count("Piano (Staff 2)") == 0


# --------------------------------------------------------------------------- #
#  Тест 15 — Часть 7: звучащая высота натуральных/искусственных гармоник
# --------------------------------------------------------------------------- #
def test_harmonic_node_interval_table():
    assert g.harmonic_interval_from_node(12.0) == 12
    assert g.harmonic_interval_from_node(7.0) == 19
    assert g.harmonic_interval_from_node(5.0) == 24
    assert g.harmonic_interval_from_node(4.0) == 28
    assert g.harmonic_interval_from_node(9.0) == 28
    assert g.harmonic_interval_from_node(3.2) == 31
    assert g.harmonic_interval_from_node(8.0) is None  # не узел


def test_artificial_harmonic_sounding_pitch_pnd():
    """pnd/Solo Guitar, такт 63 (1-based): A.H. лад 5, узел +12 -> звучащая
    высота = прижатая + 12, затем октавами вниз в диапазон артикуляции
    Harmonics (max_note из конфига). Экспорт обязан отдать именно её."""
    _require(PND_NOAUDIO)
    song = parse_cached(str(PND_NOAUDIO))
    track = next(t for t in song.tracks if t.name == "Solo Guitar")
    sp = {s.number: s.value for s in track.strings}

    harm_notes = [
        (start_tick, n)
        for measure, voice, vi, bi, beat, mst, start_tick, dur in g.iter_voice_beats_with_canonical_ticks(track)
        for n in beat.notes
        if n.effect.harmonic is not None
        and type(n.effect.harmonic).__name__ == "ArtificialHarmonic"
    ]
    assert harm_notes, "в Solo Guitar нет искусственных гармоник"
    tick0, n0 = harm_notes[0]
    fretted = sp[n0.string] + n0.value
    assert g.harmonic_sounding_pitch(n0, sp) == fretted + 12  # узел 12.0 -> +1 октава

    from articulation_config import config_for_track_type
    cfg = config_for_track_type("GUITAR")
    expected = g.shreddage_harmonic_pitch(n0, sp, cfg)
    assert expected % 12 == (fretted + 12) % 12, "питч-класс гармоники потерян"

    events = note_events(g.build_instrument_midi(song, track, "GUITAR")[0])
    assert any(p == expected and on == tick0 for p, on, off in events), (
        "гармоника экспортирована не той высотой"
    )


def test_harmonics_folded_into_articulation_range():
    """Диапазон сэмплов артикуляции Harmonics уже плейбл-рейнджа: ноты выше
    max_note из конфига в Hydra НЕМЫЕ (реальный кейс: такты 65/68/73-74/
    106-107 pnd — звучащие высоты гармоник 80..88 уходили «в ноль»).
    Все экспортированные гармоники обязаны быть <= max_note."""
    _require(PND_NOAUDIO)
    from articulation_config import config_for_track_type
    cfg = config_for_track_type("GUITAR")
    max_note = int(cfg["keyswitches"]["harmonics"]["max_note"])

    song = parse_cached(str(PND_NOAUDIO))
    track = next(t for t in song.tracks if t.name == "Solo Guitar")
    sp = {s.number: s.value for s in track.strings}

    harm_ticks = {}
    for measure, voice, vi, bi, beat, mst, start_tick, dur in g.iter_voice_beats_with_canonical_ticks(track):
        for n in beat.notes:
            if n.effect.harmonic is not None and getattr(n.type, "name", "") != "tie":
                htype = type(n.effect.harmonic).__name__
                if htype in ("NaturalHarmonic", "ArtificialHarmonic", "SemiHarmonic"):
                    harm_ticks[start_tick] = g.shreddage_harmonic_pitch(n, sp, cfg)
    assert harm_ticks, "в Solo Guitar нет гармоник"

    events = note_events(g.build_instrument_midi(song, track, "GUITAR")[0])
    exported = {}
    for p, on, off in events:
        exported.setdefault(on, set()).add(p)
    for tick, expected in harm_ticks.items():
        assert expected in exported.get(tick, set()), (
            f"гармоника на тике {tick}: ожидалась {expected}, есть {exported.get(tick)}"
        )
        assert expected <= max_note, (
            f"гармоника на тике {tick} = {expected} выше потолка {max_note} "
            "(в Hydra будет немой)"
        )
    # такт 65: E6 (88) сложена в E5 (76) — та самая «нулевая» нота
    bar65_tick = 64 * TICKS_PER_BAR_4_4 + 2160
    assert 76 in exported.get(bar65_tick, set())


@pytest.mark.xfail(strict=True, reason="B4: второй голос склеивается в один голос "
                                       "последовательно (не реализовано разделение голосов)")
def test_multivoice_second_voice_not_flattened_xfail(tmp_path):
    """Дублируем voice 0 в слот 1 одного такта. При КОРРЕКТНОМ мультиголосе такт
    остаётся длиной в канон (голоса параллельны). Сейчас ApolloTab склеивает оба
    голоса в один плоский measure.beats -> длительность такта ~удваивается.

    Инвариант «сумма длительностей такта <= канон + допуск» ловит это: сейчас он
    нарушается на пропатченном такте (xfail). Если B4 починят (разделение голосов
    или параллельное сведение) — инвариант выполнится, тест пройдёт, а strict-xfail
    заставит обновить тест (баг не «улучшится» незаметно).
    """
    _require(TTN_NOAUDIO)
    fx = tmp_path / "multivoice.gp"
    _make_multivoice_gp(TTN_NOAUDIO, fx)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        patched = gp_import.parse_song(str(fx))

    TUPLET_TOL = 40
    for track in patched.tracks:
        for m in track.measures:
            canon = g.measure_length_ticks(m)
            for v in m.voices:
                s = sum(b.duration.time for b in v.beats)
                assert s - canon <= TUPLET_TOL
