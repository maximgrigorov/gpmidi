"""--slide-to-target: слайд к РЕАЛЬНОЙ высоте следующей ноты (opt-in).

Без флага экспорт байт-в-байт прежний (фиксированные SLIDE_BEND_ST полутонов,
см. test_bend_slide_timing.py). С флагом:
  * shift/legato slide -> ступенчатый PB до фактического интервала;
  * нет цели / цель за паузой / authored bend / полифония -> PB не трогаем;
  * интервал глубже шкалы PB -> прежняя частичная глубина, но в верную сторону;
  * нативный Hydra Legato Slide — только если он verified в карте артикуляций.
"""
from __future__ import annotations

import copy

from guitarpro.models import SlideType

import gp_to_shreddage as g
from articulation_config import config_for_track_type
from gp_import import GPBend, GPBendPoint, GPNoteEffect, NaturalHarmonic
from test_bend_slide_timing import _beat, _note, _song

PB_RANGE = 7          # shreddage_hydra_3.5.yaml


def _events(midi_track):
    tick = 0
    rows = []
    for index, message in enumerate(midi_track):
        tick += message.time
        rows.append((tick, index, message))
    return rows


def _pitchwheel(midi_track):
    return [(tick, message.pitch) for tick, _i, message in _events(midi_track)
            if message.type == "pitchwheel"]


def _musical(midi_track, kind):
    return [(tick, index, message.note) for tick, index, message in _events(midi_track)
            if message.type == kind and message.note >= 28]


def _pw(semitones, pb_range=PB_RANGE):
    return g.semitones_to_pitchwheel(semitones, pb_range)


def _slide_song(source_fret, target_fret, slide=SlideType.legatoSlideTo, *,
                source_effect=None, target_effect=None, partner=None):
    effect = source_effect or GPNoteEffect()
    effect.slides = [slide]
    source = [_note(source_fret, effect=effect)]
    if partner is not None:
        source.append(partner)
    target = _note(target_fret, effect=target_effect or GPNoteEffect())
    return _song([[_beat(source), _beat([target])]])


def _build(song, track, track_type="GUITAR", **kwargs):
    return g.build_instrument_midi(song, track, track_type, slide_to_target=True, **kwargs)


def test_legato_slide_up_reaches_the_real_target_interval():
    song, track = _slide_song(5, 9)                 # +4 полутона
    midi_track, stats = _build(song, track)
    pw = _pitchwheel(midi_track)

    glide = [(tick, value) for tick, value in pw if 0 < tick < 960]
    values = [value for _tick, value in glide]
    assert values == sorted(values), "глайд обязан идти монотонно вверх"
    assert values[-1] == _pw(4)
    # одно событие на пройденный лад, плюс стартовая точка
    assert [value for value in values if value] == [_pw(k) for k in (1, 2, 3, 4)]
    assert glide[-1][0] == 959
    assert stats["slide_to_target"] == {"pitch_bend_to_target": 1}

    # прежний режим уходил на фиксированные 2 полутона при любом интервале
    legacy, _ = g.build_instrument_midi(song, track, "GUITAR")
    assert max(value for _tick, value in _pitchwheel(legacy)) == _pw(2)


def test_shift_slide_down_reaches_the_real_target_interval():
    song, track = _slide_song(9, 6, SlideType.shiftSlideTo)    # -3 полутона
    midi_track, stats = _build(song, track)
    glide = [value for tick, value in _pitchwheel(midi_track) if 0 < tick < 960]

    assert glide[-1] == _pw(-3)
    assert [value for value in glide if value] == [_pw(-1), _pw(-2), _pw(-3)]
    assert stats["slide_to_target"] == {"pitch_bend_to_target": 1}


def test_glide_length_follows_the_interval_and_stays_inside_the_source():
    small_song, small_track = _slide_song(5, 6)
    wide_song, wide_track = _slide_song(5, 11)
    small = [t for t, v in _pitchwheel(_build(small_song, small_track)[0]) if 0 < t < 960]
    wide = [t for t, v in _pitchwheel(_build(wide_song, wide_track)[0]) if 0 < t < 960]
    small_len, wide_len = small[-1] - small[0], wide[-1] - wide[0]

    assert wide_len > small_len
    assert wide[0] >= 480, "глайд не длиннее половины ноты-источника"


def test_slide_without_a_following_note_falls_back_safely():
    effect = GPNoteEffect(slides=[SlideType.legatoSlideTo])
    song, track = _song([[_beat([_note(7, effect=effect)])]])
    midi_track, stats = _build(song, track)

    assert all(value == 0 for _tick, value in _pitchwheel(midi_track))
    assert stats["slide_to_target"] == {"skipped:no_target": 1}
    # прежний режим придумывал направление по типу слайда: +2 вверх
    legacy, _ = g.build_instrument_midi(song, track, "GUITAR")
    assert any(value > 0 for _tick, value in _pitchwheel(legacy))


def test_target_after_a_long_rest_is_not_a_slide_target():
    effect = GPNoteEffect(slides=[SlideType.legatoSlideTo])
    rest = _beat([])
    song, track = _song([[_beat([_note(7, effect=effect)]), rest, rest], [_beat([_note(9)])]])
    midi_track, stats = _build(song, track)

    assert all(value == 0 for _tick, value in _pitchwheel(midi_track))
    assert stats["slide_to_target"] == {"skipped:target_after_rest": 1}


def test_interval_deeper_than_pitch_bend_range_keeps_fixed_depth_in_true_direction():
    song, track = _slide_song(12, 3)                # -9 полутонов при шкале 7
    midi_track, stats = _build(song, track)
    values = [value for tick, value in _pitchwheel(midi_track) if 0 < tick < 960]

    assert values[-1] == _pw(-2)
    assert all(abs(value) < 8191 for value in values), "никаких упоров в потолок"
    assert stats["slide_to_target"] == {"fixed_depth_fallback": 1}


def test_authored_bend_on_the_source_wins_over_the_slide():
    bend = GPBend(points=[GPBendPoint(position=0, value=0),
                          GPBendPoint(position=6, value=4),
                          GPBendPoint(position=12, value=4)])
    song, track = _slide_song(5, 9, source_effect=GPNoteEffect(bend=bend))
    midi_track, stats = _build(song, track)

    bend_only = copy.deepcopy(track)
    bend_only.measures[0].voices[0].beats[0].notes[0].effect.slides = []
    expected, _ = _build(song, bend_only)

    assert _pitchwheel(midi_track) == _pitchwheel(expected)
    assert stats["slide_to_target"] == {"skipped:authored_bend": 1}


def test_prebent_target_is_a_conflict_not_a_slide_target():
    prebend = GPBend(points=[GPBendPoint(position=0, value=4),
                             GPBendPoint(position=12, value=0)])
    song, track = _slide_song(5, 7, target_effect=GPNoteEffect(bend=prebend))
    midi_track, stats = _build(song, track)

    assert all(value == 0 for tick, value in _pitchwheel(midi_track) if tick < 960)
    assert stats["slide_to_target"] == {"skipped:target_prebend": 1}


def test_double_stop_with_a_held_note_gets_no_pitch_bleed():
    held = _note(5, string=2)                        # звучит под слайдом
    song, track = _slide_song(7, 9, partner=held)
    midi_track, stats = _build(song, track)

    assert all(value == 0 for _tick, value in _pitchwheel(midi_track))
    assert stats["slide_to_target"] == {"skipped:polyphony": 1}
    # прежний режим гнул весь канал — и держащуюся ноту тоже
    legacy, _ = g.build_instrument_midi(song, track, "GUITAR")
    assert any(value for _tick, value in _pitchwheel(legacy))


def test_parallel_double_stop_slide_shares_one_curve():
    upper = GPNoteEffect(slides=[SlideType.legatoSlideTo])
    lower = GPNoteEffect(slides=[SlideType.legatoSlideTo])
    source = [_note(5, string=1, effect=upper), _note(7, string=2, effect=lower)]
    target = [_note(7, string=1), _note(9, string=2)]
    song, track = _song([[_beat(source), _beat(target)]])
    midi_track, stats = _build(song, track)
    glide = [(tick, value) for tick, value in _pitchwheel(midi_track) if 0 < tick < 960]

    assert glide[-1][1] == _pw(2)
    assert len({tick for tick, _v in glide}) == len(glide), "кривая не дублируется"
    assert stats["slide_to_target"] == {"pitch_bend_to_target": 2}


def test_pitch_is_reset_before_the_target_attack_and_stays_clean():
    song, track = _slide_song(5, 8)
    midi_track, _stats = _build(song, track)
    rows = _events(midi_track)

    target_on = next((tick, index) for tick, index, note in _musical(midi_track, "note_on")
                     if tick == 960)
    before = [message.pitch for tick, index, message in rows
              if message.type == "pitchwheel" and (tick, index) < target_on]
    assert before[-1] == 0, "цель атакуется при ненулевом PB"
    assert not any(message.pitch for tick, _i, message in rows
                   if message.type == "pitchwheel" and tick >= 960)


def test_event_order_at_the_target_is_off_reset_keyswitch_then_note_on():
    # цель меняет артикуляцию -> свой keyswitch за KS_LEAD_MS до атаки
    song, track = _slide_song(5, 8, target_effect=GPNoteEffect(palmMute=True))
    midi_track, _stats = _build(song, track)
    rows = _events(midi_track)
    index_of = {(tick, message.type, getattr(message, "note", None),
                 getattr(message, "pitch", None)): index for tick, index, message in rows}

    source_off = index_of[(960, "note_off", 64 + 5, None)]
    reset = index_of[(960, "pitchwheel", None, 0)]
    target_on = index_of[(960, "note_on", 64 + 8, None)]
    assert source_off < reset < target_on

    palm_ks = next(tick for tick, _i, message in rows
                   if message.type == "note_on" and message.note == 13)
    assert palm_ks < 960
    last_glide = max(tick for tick, _i, message in rows
                     if message.type == "pitchwheel" and message.pitch)
    assert last_glide < 960


def _verified_native_cfg():
    cfg = copy.deepcopy(config_for_track_type("GUITAR"))
    cfg["slide_to_target"]["native"]["verified"] = True
    return cfg


def test_verified_native_legato_slide_uses_keyswitch_and_overlap_without_pb():
    cfg = _verified_native_cfg()
    song, track = _slide_song(5, 14)                 # +9: глубже шкалы PB, но <= 12
    midi_track, stats = _build(song, track, cfg=cfg)
    ks = cfg["fx_keyswitches"]["legato_slide_to_next"]

    assert all(value == 0 for _tick, value in _pitchwheel(midi_track))
    ks_on = [tick for tick, _i, note in _musical_or_ks(midi_track, "note_on", ks)]
    ks_off = [tick for tick, _i, note in _musical_or_ks(midi_track, "note_off", ks)]
    assert len(ks_on) == len(ks_off) == 1
    assert ks_on[0] < 960 < ks_off[0], "non-latching KS держится через атаку цели"
    source_off = next(tick for tick, _i, note in _musical(midi_track, "note_off") if note == 69)
    assert source_off > 960, "легато Hydra = перекрытие источника и цели"
    assert stats["slide_to_target"] == {"native_keyswitch": 1}


def _musical_or_ks(midi_track, kind, ks):
    return [(tick, index, message.note) for tick, index, message in _events(midi_track)
            if message.type == kind and message.note == ks]


def test_native_slide_is_not_used_for_shift_slides_or_when_unverified():
    cfg = _verified_native_cfg()
    ks = cfg["fx_keyswitches"]["legato_slide_to_next"]
    shift_song, shift_track = _slide_song(5, 8, SlideType.shiftSlideTo)
    shift_midi, shift_stats = _build(shift_song, shift_track, cfg=cfg)
    assert not _musical_or_ks(shift_midi, "note_on", ks)
    assert shift_stats["slide_to_target"] == {"pitch_bend_to_target": 1}

    song, track = _slide_song(5, 8)
    unverified, stats = _build(song, track)          # конфиг как в репозитории
    assert not _musical_or_ks(unverified, "note_on", ks)
    assert stats["slide_to_target"] == {"pitch_bend_to_target": 1}


def test_slide_in_from_below_starts_bent_on_the_attack():
    effect = GPNoteEffect(slides=[SlideType.intoFromBelow])
    song, track = _song([[_beat([_note(7, effect=effect)])]])
    midi_track, stats = _build(song, track)
    rows = _events(midi_track)
    pw = _pitchwheel(midi_track)

    assert pw[0] == (0, _pw(-2))
    first_bent = next(index for tick, index, m in rows if m.type == "pitchwheel" and m.pitch)
    note_on = next(index for tick, index, note in _musical(midi_track, "note_on"))
    assert first_bent < note_on, "заход задан до атаки на том же тике"
    assert [v for t, v in pw if t > 0][-1] == 0
    assert stats["slide_to_target"] == {"slide_in": 1}


def test_slide_out_falls_by_type_not_towards_the_next_note():
    effect = GPNoteEffect(slides=[SlideType.outDownwards])
    song, track = _song([[_beat([_note(5, effect=effect)]), _beat([_note(9)])]])
    midi_track, stats = _build(song, track)
    values = [value for tick, value in _pitchwheel(midi_track) if 0 < tick < 960]

    assert values[-1] == _pw(-2)
    assert stats["slide_to_target"] == {"slide_out": 1}


def test_harmonic_slides_are_left_alone():
    effect = GPNoteEffect(harmonic=NaturalHarmonic())
    song, track = _slide_song(12, 14, source_effect=effect)
    midi_track, stats = _build(song, track)

    assert all(value == 0 for _tick, value in _pitchwheel(midi_track))
    assert stats["slide_to_target"] == {"skipped:harmonic": 1}


def test_bass_uses_its_own_narrow_pitch_bend_range():
    song, track = _slide_song(5, 6)                  # +1 при шкале Darkwall 2
    track.name = "Bass"
    one, one_stats = _build(song, track, "BASS")
    assert [v for t, v in _pitchwheel(one) if 0 < t < 960][-1] == _pw(1, 2)
    assert one_stats["slide_to_target"] == {"pitch_bend_to_target": 1}

    song, track = _slide_song(5, 8)                  # +3: глубже шкалы
    track.name = "Bass"
    wide, wide_stats = _build(song, track, "BASS")
    assert max(v for t, v in _pitchwheel(wide)) == _pw(1, 2)
    assert wide_stats["slide_to_target"] == {"fixed_depth_fallback": 1}


def test_flag_changes_nothing_on_a_part_without_slides():
    song, track = _song([[_beat([_note(5)]), _beat([_note(7), _note(5, string=2)])]])
    plain, plain_stats = g.build_instrument_midi(song, track, "GUITAR")
    flagged, flagged_stats = _build(song, track)

    assert list(plain) == list(flagged)
    assert flagged_stats.get("slide_to_target", {}) == {}


def test_cli_flags_are_known_and_opt_in():
    parse = g.parse_cli_options
    defaults = parse(["gp_to_shreddage.py", "song.gp"])
    assert defaults["slide_to_target"] is False
    assert defaults["articulation_manifest"] is False
    options = parse(["gp_to_shreddage.py", "song.gp", "--slide-to-target",
                     "--articulation-manifest"])
    assert options["slide_to_target"] is True
    assert options["articulation_manifest"] is True
