"""Бенды/слайды на лигованных нотах и направление слайда через тактовую черту.

Оба сценария — синтетика через build_instrument_midi: бенд обязан накрывать
ПОЛНУЮ звучащую длительность (с лигами), а слайд на последней доле такта обязан
видеть следующую ноту в следующем такте.
"""
from __future__ import annotations

from guitarpro.models import NoteType, SlideType

import gp_to_shreddage as g
from gp_import import (
    GPBeat,
    GPBeatEffect,
    GPBeatStatus,
    GPBend,
    GPBendPoint,
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


def _note(fret: int, *, string: int = 1, effect: GPNoteEffect | None = None,
          note_type: NoteType = NoteType.normal) -> GPNote:
    return GPNote(
        string=string, value=fret, velocity=95,
        effect=effect or GPNoteEffect(), type=note_type, realValue=0,
    )


def _beat(notes: list[GPNote]) -> GPBeat:
    return GPBeat(
        start=0, duration=QUARTER, notes=notes,
        effect=GPBeatEffect(), status=GPBeatStatus("normal"),
    )


def _song(measures_of_beats: list[list[GPBeat]]) -> tuple[GPSong, GPTrack]:
    header = GPMeasureHeader(GPTimeSignature(4, GPDenominator(4)))
    track = GPTrack(
        name="Solo Guitar",
        strings=[
            GPString(1, 64), GPString(2, 59), GPString(3, 55),
            GPString(4, 50), GPString(5, 45), GPString(6, 40),
        ],
        channel=GPChannel(30),
        measures=[GPMeasure(0, header, [GPVoice(beats)]) for beats in measures_of_beats],
    )
    song = GPSong("fixture", "", "", 120, [track], [header for _ in measures_of_beats])
    return song, track


def _pitchwheel_events(midi_track) -> list[tuple[int, int]]:
    tick = 0
    out = []
    for message in midi_track:
        tick += message.time
        if message.type == "pitchwheel":
            out.append((tick, message.pitch))
    return out


def test_bend_curve_and_reset_cover_the_tied_note_end():
    """Лига продлевает ноту; сброс PB в 0 обязан стоять на НАСТОЯЩЕМ конце.

    Раньше кривая рисовалась по длине первого сегмента, и сброс попадал в
    середину звучащей ноты — слышимый щелчок высоты. Вибрато было отложено
    до конца цикла ровно по этой причине, bend/slide — нет.
    """
    bend = GPBend(points=[GPBendPoint(position=0, value=0),
                          GPBendPoint(position=12, value=4)])
    origin = _note(5, effect=GPNoteEffect(bend=bend))
    tie = _note(5, note_type=NoteType.tie)
    song, track = _song([[_beat([origin]), _beat([tie])]])

    midi_track, _stats = g.build_instrument_midi(song, track, "GUITAR")
    pw = _pitchwheel_events(midi_track)

    top = g.semitones_to_pitchwheel(2, 7)
    assert (959, top) in pw           # вершина относится к первому GP-сегменту
    assert (1920, 0) in pw            # reset — на конце всей лиги, не на 960
    assert (960, 0) not in pw         # высота удерживается через tie без щелчка


def test_tied_bend_segment_keeps_its_own_release_curve():
    """Bend на tie-сегменте продолжает удерживаемую ноту, а не теряется."""
    rise = GPBend(points=[
        GPBendPoint(position=0, value=4),
        GPBendPoint(position=12, value=8),
    ])
    release = GPBend(points=[
        GPBendPoint(position=0, value=8),
        GPBendPoint(position=3, value=8),
        GPBendPoint(position=6, value=0),
        GPBendPoint(position=12, value=0),
    ])
    origin = _note(8, effect=GPNoteEffect(bend=rise))
    tie = _note(8, effect=GPNoteEffect(bend=release), note_type=NoteType.tie)
    song, track = _song([[_beat([origin]), _beat([tie])]])

    midi_track, _stats = g.build_instrument_midi(song, track, "GUITAR")
    pw = _pitchwheel_events(midi_track)

    two = g.semitones_to_pitchwheel(2, 7)
    four = g.semitones_to_pitchwheel(4, 7)
    assert (0, two) in pw
    assert (959, four) in pw
    assert (960, four) in pw
    assert (1440, 0) in pw
    assert (1920, 0) in pw


def test_bend_points_are_timed_to_origin_segment_not_full_tie():
    """Лига не должна вдвое задерживать вершину bend первого сегмента."""
    bend = GPBend(points=[
        GPBendPoint(position=0, value=0),
        GPBendPoint(position=3, value=10),
        GPBendPoint(position=6, value=10),
        GPBendPoint(position=9, value=0),
        GPBendPoint(position=12, value=0),
    ])
    origin = _note(4, effect=GPNoteEffect(bend=bend))
    tie = _note(4, note_type=NoteType.tie)
    song, track = _song([[_beat([origin]), _beat([tie])]])

    midi_track, _stats = g.build_instrument_midi(song, track, "GUITAR")
    pw = _pitchwheel_events(midi_track)

    five = g.semitones_to_pitchwheel(5, 7)
    assert (240, five) in pw
    assert (480, five) in pw
    assert (720, 0) in pw
    assert (1920, 0) in pw


def test_shift_slide_sees_lower_target_across_the_barline():
    """Нисходящий shift-слайд через тактовую черту обязан гнуться ВНИЗ.

    Раньше поиск следующей ноты шёл только по битам текущего такта: слайд на
    последней доле цели не находил и уходил в фолбэк по типу, где shift/legato
    слайды по умолчанию идут вверх.
    """
    slide_note = _note(7, effect=GPNoteEffect(slides=[SlideType.shiftSlideTo]))
    target = _note(5)  # ниже на 2 лада, в СЛЕДУЮЩЕМ такте
    song, track = _song([[_beat([slide_note])], [_beat([target])]])

    midi_track, _stats = g.build_instrument_midi(song, track, "GUITAR")
    pw = _pitchwheel_events(midi_track)

    slide_targets = [pitch for _tick, pitch in pw if pitch != 0]
    assert slide_targets, "слайд не эмитнулся вовсе"
    assert all(pitch < 0 for pitch in slide_targets), (
        f"слайд гнётся вверх вместо вниз: {pw}"
    )


def test_slide_target_search_skips_tie_continuations():
    """Tie-нота — продолжение слайдящей ноты, а не её цель: направление
    определяет следующая РЕАЛЬНАЯ нота."""
    slide_note = _note(7, effect=GPNoteEffect(slides=[SlideType.shiftSlideTo]))
    tie = _note(7, note_type=NoteType.tie)
    target = _note(5)
    song, track = _song([[_beat([slide_note]), _beat([tie])], [_beat([target])]])

    midi_track, _stats = g.build_instrument_midi(song, track, "GUITAR")
    pw = _pitchwheel_events(midi_track)

    slide_targets = [pitch for _tick, pitch in pw if pitch != 0]
    assert slide_targets and all(pitch < 0 for pitch in slide_targets)
