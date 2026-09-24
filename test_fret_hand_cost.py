"""Стоимость переноса левой руки (--fret-hand-cost) и её дифференциальная проверка.

Синтетика через build_instrument_midi: соло-линия шестнадцатыми, в которой рука
то стоит на месте (одинаковые лады), то прыгает на 7 ладов. Главное здесь —
test_differential_check_fails_without_the_feature: проверка, которая ни разу не
падала, не проверена (LESSONS.md п.11).
"""
from __future__ import annotations

import pytest
from guitarpro.models import NoteType, SlideType
from mido import MidiFile

import gp_to_shreddage as g
import hand_cost_check as hc
import humanize as h
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
from verify_midi import smoke_check

SIXTEENTH = GPDuration(value=16, time=240)
QUARTER = GPDuration(value=4, time=960)
PROFILE = h.load_profile("guitar_metal")
CFG = config_for_track_type(g.TRACK_GUITAR)
SHIFT_FRETS = float(CFG["performance_life"]["fret_noise_on_hand_shift"]["min_fret_shift"])
# Рука стоит четыре шестнадцатых на 5-м ладу, затем четыре на 12-м: в каждом
# такте 4 прыжка на 7 ладов и 12 битов без переноса.
JUMP_BAR = [5, 5, 5, 5, 12, 12, 12, 12] * 2


def _note(fret, *, string=2, note_type=NoteType.normal, **effect):
    return GPNote(string=string, value=fret, velocity=95,
                  effect=GPNoteEffect(**effect), type=note_type, realValue=0)


def _beat(notes, duration=SIXTEENTH):
    return GPBeat(start=0, duration=duration, notes=notes,
                  effect=GPBeatEffect(), status=GPBeatStatus("normal"))


def _song(bars, tempo=90):
    header = GPMeasureHeader(GPTimeSignature(4, GPDenominator(4)))
    track = GPTrack(
        name="Solo Guitar",
        strings=[GPString(1, 64), GPString(2, 59), GPString(3, 55),
                 GPString(4, 50), GPString(5, 45), GPString(6, 40)],
        channel=GPChannel(30),
        measures=[GPMeasure(0, header, [GPVoice(beats)]) for beats in bars],
    )
    return GPSong("fixture", "", "", tempo, [track], [header for _ in bars]), track


def _jump_song(bars=12):
    return _song([[_beat([_note(f)]) for f in JUMP_BAR] for _ in range(bars)])


def _notes(midi_track):
    """[(on, off, pitch)] без keyswitch-нот, в порядке атак."""
    ks = hc._keyswitch_notes(CFG)
    tick, sounding, out = 0, {}, []
    for msg in midi_track:
        tick += msg.time
        if msg.type not in ("note_on", "note_off") or msg.note in ks:
            continue
        if msg.type == "note_on" and msg.velocity > 0:
            sounding.setdefault(msg.note, []).append(tick)
        elif sounding.get(msg.note):
            out.append((sounding[msg.note].pop(0), tick, msg.note))
    return sorted(out)


def _build(song, track, **kwargs):
    return g.build_instrument_midi(song, track, g.TRACK_GUITAR, **kwargs)


def _bytes(midi_track):
    return [(m.type, m.time, getattr(m, "note", None), getattr(m, "velocity", None),
             getattr(m, "pitch", None)) for m in midi_track]


# --------------------------------------------------------------------------- #
#  Модель
# --------------------------------------------------------------------------- #
def test_cost_is_zero_without_shift_and_grows_with_saturation():
    costs = [h.fret_hand_cost_ms(d / 2.0, SHIFT_FRETS, PROFILE) for d in range(0, 49)]
    ceiling = float(PROFILE["fret_hand_cost"]["saturation_ms"])
    assert costs[0] == 0.0
    assert all(b >= a for a, b in zip(costs, costs[1:])), "стоимость обязана расти с расстоянием"
    assert max(costs) < ceiling, "рука не тормозит бесконечно"
    within = h.fret_hand_cost_ms(SHIFT_FRETS - 1, SHIFT_FRETS, PROFILE)
    at_shift = h.fret_hand_cost_ms(SHIFT_FRETS, SHIFT_FRETS, PROFILE)
    assert within * 2 < at_shift, "смена позиции обязана быть заметно дороже, чем внутри позиции"


def test_delay_means_the_same_milliseconds_at_any_resolution_and_tempo():
    """Время переноса физическое: tpb 960 и 480 и разные темпы — одни и те же мс."""
    def delay_ms(tpb, bpm):
        ticks = h.fret_hand_delay(7, SHIFT_FRETS, 0.0, 4 * tpb, bpm, tpb, PROFILE)
        return ticks * 60000.0 / (bpm * tpb)

    reference = h.fret_hand_cost_ms(7, SHIFT_FRETS, PROFILE)
    for tpb in (480, 960):
        for bpm in (75, 90, 120):
            assert delay_ms(tpb, bpm) == pytest.approx(reference, abs=60000.0 / (bpm * tpb))


def test_silence_before_the_attack_is_credited_to_the_move():
    cost = h.fret_hand_cost_ms(7, SHIFT_FRETS, PROFILE)
    full = h.fret_hand_delay(7, SHIFT_FRETS, 0.0, 960, 90, 960, PROFILE)
    half = h.fret_hand_delay(7, SHIFT_FRETS, cost / 2, 960, 90, 960, PROFILE)
    assert full > half > 0
    assert h.fret_hand_delay(7, SHIFT_FRETS, cost + 1, 960, 90, 960, PROFILE) == 0


def test_delay_is_capped_by_sixteenth_fraction_and_by_the_beat_itself():
    hc_cfg = PROFILE["fret_hand_cost"]
    fast = h.fret_hand_delay(12, SHIFT_FRETS, 0.0, 960, 240, 960, PROFILE)
    assert fast <= hc_cfg["max_delay_frac16"] * 240
    tiny_beat = 30
    assert h.fret_hand_delay(12, SHIFT_FRETS, 0.0, tiny_beat, 90, 960, PROFILE) \
        <= hc_cfg["max_delay_note_frac"] * tiny_beat


def test_profile_section_rejects_missing_keys_instead_of_defaulting():
    section = dict(PROFILE["fret_hand_cost"])
    del section["shift_ms"]
    with pytest.raises(ValueError, match="shift_ms"):
        h._validate_fret_hand_cost(section, "guitar_metal.yaml")


def test_threshold_lives_in_articulation_map_not_in_profile():
    assert PROFILE["config_version"] >= 2
    assert "min_fret_shift" not in PROFILE["fret_hand_cost"]
    assert "fret_hand_cost" not in h.load_profile("bass_metal")


# --------------------------------------------------------------------------- #
#  Флаг и совместимость
# --------------------------------------------------------------------------- #
def test_flag_is_inert_without_humanize_and_defaults_off():
    song, track = _jump_song(2)
    plain, _ = _build(song, track)
    assert _bytes(_build(song, track, fret_hand_cost=True)[0]) == _bytes(plain)
    humanized, _ = _build(song, track, humanize=True)
    assert _bytes(_build(song, track, humanize=True, fret_hand_cost=False)[0]) == _bytes(humanized)


def test_cli_rejects_the_flag_without_humanize():
    with pytest.raises(ValueError, match="--humanize"):
        g.parse_cli_options(["gp_to_shreddage.py", "song.gp5", "--fret-hand-cost"])
    options = g.parse_cli_options(
        ["gp_to_shreddage.py", "song.gp5", "--humanize", "--fret-hand-cost"])
    assert options["fret_hand_cost"] is True
    assert g.parse_cli_options(["gp_to_shreddage.py", "song.gp5"])["fret_hand_cost"] is False


# --------------------------------------------------------------------------- #
#  Встраивание в конвертер
# --------------------------------------------------------------------------- #
def test_only_attacks_of_jump_beats_move_and_only_later():
    song, track = _jump_song(4)
    base = _notes(_build(song, track, humanize=True)[0])
    moved = _notes(_build(song, track, humanize=True, fret_hand_cost=True)[0])
    assert [p for *_x, p in base] == [p for *_x, p in moved]
    # Конец ноты фича не двигает. Отличаться он может только одним способом:
    # --humanize обрезает хвост по атаке следующей ноты на той же струне
    # (LESSONS.md п.15), а эту атаку фича задержала — обрезка встала позже.
    for i, (b, m) in enumerate(zip(base, moved)):
        own_end = b[0] + SIXTEENTH.time      # сдвинутое начало + длительность, без обрезки
        next_attack = moved[i + 1][0] if i + 1 < len(moved) else own_end
        assert m[1] >= b[1], i
        assert m[1] == min(own_end, next_attack), i
    assert all(a[1] <= b[0] for a, b in zip(moved, moved[1:])), \
        "задержанная нота не налезает на следующую атаку"
    shifts = [m[0] - b[0] for b, m in zip(base, moved)]
    assert min(shifts) >= 0, "перенос руки не может сдвинуть атаку вперёд"
    # ноты на 12-м ладу в началах групп — прыжки; внутри групп рука стоит
    jump_positions = {i for i in range(len(JUMP_BAR)) if i % 4 == 0}
    for index, shift in enumerate(shifts):
        is_jump = index % len(JUMP_BAR) in jump_positions and index > 0
        assert (shift > 0) == is_jump, index


def test_keyswitch_is_emitted_before_the_delayed_attack(tmp_path):
    bars = []
    for _ in range(4):
        beats = [_beat([_note(f)]) for f in JUMP_BAR]
        # смена артикуляции ровно на прыжке: KS обязан встать перед ЗАДЕРЖАННОЙ нотой
        beats[4] = _beat([_note(12, palmMute=True)])
        bars.append(beats)
    song, track = _song(bars)
    midi_track, _ = _build(song, track, humanize=True, fret_hand_cost=True)
    path = tmp_path / "solo.mid"
    midi = MidiFile(type=0, ticks_per_beat=g.TICKS_PER_BEAT)
    midi.tracks.append(midi_track)
    midi.save(path)
    codes = {code for sev, code, _msg in smoke_check(path, g.TRACK_GUITAR)}
    assert not codes & {"KS_LEAD", "STUCK", "VEL_ZONE", "PB_LEAK", "BEND_CEILING"}


def test_legato_and_slide_targets_are_not_delayed_and_keep_their_overlap():
    # 5 -> 5(hammer/slide) -> 12 -> 12, по кругу: прыжок 5->12 — цель легато,
    # а 12->5 на стыке групп — обычный прыжок медиатором.
    for effect in ({"hammer": True}, {"slides": [SlideType.legatoSlideTo]}):
        group = [_beat([_note(5)]), _beat([_note(5, **effect)]), _beat([_note(12)]),
                 _beat([_note(12)])]
        song, track = _song([group * 4])
        base = _notes(_build(song, track, humanize=True)[0])
        moved, stats = _build(song, track, humanize=True, fret_hand_cost=True)
        moved = _notes(moved)
        targets = range(2, len(base), 4)
        picked_jumps = range(4, len(base), 4)
        assert all(moved[i][0] == base[i][0] for i in targets), effect
        assert all(moved[i][0] > base[i][0] for i in picked_jumps), effect
        assert stats["fret_hand_cost_legato_skipped"] == len(targets)
        # Перекрытие источник -> цель ровно такое же, как без фичи. Что
        # --humanize само рвёт часть таких пар — отдельный дефект (LESSONS.md
        # п.15), фича его не усугубляет.
        assert all((moved[i - 1][1] > moved[i][0]) == (base[i - 1][1] > base[i][0])
                   for i in targets), effect


def test_rest_before_the_jump_absorbs_the_delay():
    bar = [_beat([_note(5)], QUARTER), _beat([], QUARTER), _beat([_note(12)], QUARTER),
           _beat([_note(12)], QUARTER)]
    song, track = _song([bar])
    base = _notes(_build(song, track, humanize=True)[0])
    moved = _notes(_build(song, track, humanize=True, fret_hand_cost=True)[0])
    assert base == moved, "за четвертную паузу рука успевает переехать"


def test_fret_noise_stays_on_the_same_beats_and_ticks():
    song, track = _jump_song(6)

    def fret_noise(midi_track):
        note = CFG["fx_keyswitches"]["fret_noise"]
        tick, out = 0, []
        for msg in midi_track:
            tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0 and msg.note == note:
                out.append(tick)
        return out

    before, s_before = _build(song, track, humanize=True, fret_noise_on_hand_shift=True)
    after, s_after = _build(song, track, humanize=True, fret_noise_on_hand_shift=True,
                            fret_hand_cost=True)
    assert s_before["fret_noise_events"] == s_after["fret_noise_events"] > 0
    assert fret_noise(before) == fret_noise(after)


# --------------------------------------------------------------------------- #
#  Дифференциальная проверка — и то, что она умеет падать
# --------------------------------------------------------------------------- #
def test_differential_check_passes_with_the_feature():
    song, track = _jump_song()
    midi_track, _ = _build(song, track, humanize=True, fret_hand_cost=True)
    result = hc.differential(song, track, midi_track, CFG)
    assert result["passed"], result
    assert result["median_gain_ms"] >= hc.MIN_MEDIAN_GAIN_MS


@pytest.mark.parametrize("kwargs", [{}, {"humanize": True}], ids=["quantized", "humanize-only"])
def test_differential_check_fails_without_the_feature(kwargs):
    """LESSONS.md п.11: проверка обязана срабатывать на заведомо сломанном выходе."""
    song, track = _jump_song()
    midi_track, _ = _build(song, track, **kwargs)
    result = hc.differential(song, track, midi_track, CFG)
    assert not result["passed"], result
    assert result["shift_beats"] >= hc.MIN_GROUP and result["zero_beats"] >= hc.MIN_GROUP, \
        "проверка обязана падать по существу, а не из-за нехватки битов"


def test_mann_whitney_behaves_on_known_inputs():
    assert hc.mann_whitney_greater([1.0] * 30, [1.0] * 30) == 1.0
    low, high = [float(i) for i in range(30)], [100.0 + i for i in range(30)]
    assert hc.mann_whitney_greater(high, low) < 1e-9
    assert hc.mann_whitney_greater(low, high) > 0.999
    # частичное перекрытие (10..39 против 0..29): значимо, но не абсурдно
    assert 1e-5 < hc.mann_whitney_greater([10.0 + i for i in range(30)], low) < 1e-3


def test_jitter_ms_counts_quantized_32nds_as_jitter():
    """Окаменелость LESSONS.md п.3: на квантованной линии 32-ми jitter_ms не ноль.

    Нечётная 32-я стоит в 40 тиках от ближайшего узла сетки 16-я + триоль
    (80 тиков при tpb=960). Для гитарного соло мерить отклонение от нотной
    атаки (onset_std_ms), а jitter_ms приводить только рядом с квантованным
    эталоном.
    """
    thirty_second = GPDuration(value=32, time=120)
    song, track = _song([[_beat([_note(5)], thirty_second) for _ in range(32)]])
    midi_track, _ = _build(song, track)
    assert hc.jitter_ms(midi_track, CFG) > 10.0
    assert hc.differential(song, track, midi_track, CFG)["onset_std_ms"] == 0.0


def test_check_needs_the_reference_when_authored_offsets_are_kept():
    """Авторские сдвиги GP (дефолт на соло) топят задержку в разбросе от сетки.

    На Spring Melody Solo сдвиги дают 28.8 мс, и проверка против сетки не видела
    включённую фичу (p = 0.17). Против эталона — того же экспорта без
    --humanize — видит и по-прежнему падает без фичи.
    """
    import random
    rng = random.Random(3)
    bars = []
    for _ in range(12):
        beats = []
        for fret in JUMP_BAR:
            note = _note(fret)
            note.playedOffset = rng.randint(-40, 40)      # 480 PPQ: до ±1/12 доли
            beats.append(_beat([note]))
        bars.append(beats)
    song, track = _song(bars)
    kept = {"preserve_gp_played_offsets": True}
    reference, _ = _build(song, track, **kept)
    with_feature, _ = _build(song, track, humanize=True, fret_hand_cost=True, **kept)
    without, _ = _build(song, track, humanize=True, **kept)

    assert not hc.differential(song, track, with_feature, CFG)["passed"], \
        "без эталона проверка обязана не видеть фичу — иначе этот тест ничего не доказывает"
    assert hc.differential(song, track, with_feature, CFG, reference)["passed"]
    assert not hc.differential(song, track, without, CFG, reference)["passed"]
