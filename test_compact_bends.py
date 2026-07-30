"""Компактные bend-кривые и явная настройка Hydra 3.5 в UI."""
from __future__ import annotations

from guitarpro.models import NoteType

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
    GPTuplet,
    GPVoice,
)
import gp_to_shreddage as g


def _absolute_pitchwheel_events(events):
    track = events.to_miditrack()
    tick = 0
    out = []
    for message in track:
        tick += message.time
        if message.type == "pitchwheel":
            out.append((tick, message.pitch))
    return out


def test_full_bend_uses_only_gp_anchor_points_and_final_reset():
    """Full bend: начало, вершина, конец плато и reset — без 10-ms россыпи."""
    events = g.EventList()
    bend = GPBend(points=[
        GPBendPoint(position=0, value=0),
        GPBendPoint(position=3, value=4),
        GPBendPoint(position=12, value=4),
    ])

    g.emit_bend(events, start_tick=1000, dur_ticks=960, bend=bend, bpm=120, pb_range=7)

    assert _absolute_pitchwheel_events(events) == [
        (1000, 0),
        (1240, g.semitones_to_pitchwheel(2, 7)),
        (1959, g.semitones_to_pitchwheel(2, 7)),
        (1960, 0),
    ]


def test_bend_release_preserves_semantic_corners_without_dense_sampling():
    """Плато и смена направления сохраняются, промежуточные ramp-точки — нет."""
    events = g.EventList()
    bend = GPBend(points=[
        GPBendPoint(position=0, value=0),
        GPBendPoint(position=3, value=8),
        GPBendPoint(position=6, value=8),
        GPBendPoint(position=9, value=4),
        GPBendPoint(position=12, value=4),
    ])

    g.emit_bend(events, start_tick=1000, dur_ticks=960, bend=bend, bpm=120, pb_range=7)

    assert _absolute_pitchwheel_events(events) == [
        (1000, 0),
        (1240, g.semitones_to_pitchwheel(4, 7)),
        (1480, g.semitones_to_pitchwheel(4, 7)),
        (1720, g.semitones_to_pitchwheel(2, 7)),
        (1959, g.semitones_to_pitchwheel(2, 7)),
        (1960, 0),
    ]


def test_slide_pitch_bend_uses_only_start_target_and_reset():
    """Линейный slide не должен создавать пять промежуточных Pitch Bend точек."""
    events = g.EventList()

    g.emit_slide(
        events,
        start_tick=1000,
        dur_ticks=960,
        slides=[],
        this_pitch=60,
        next_pitch=62,
        bpm=120,
        pb_range=7,
    )

    assert _absolute_pitchwheel_events(events) == [
        (1864, 0),
        (1959, g.semitones_to_pitchwheel(2, 7)),
        (1960, 0),
    ]


def test_pitch_bend_edge_durations_and_negative_direction():
    """Нулевая длина пропускается; один тик и negative bend не уходят до старта."""
    zero = g.EventList()
    bend = GPBend(points=[
        GPBendPoint(position=0, value=0),
        GPBendPoint(position=12, value=-4),
    ])
    g.emit_bend(zero, 1000, 0, bend, 120, 7)
    g.emit_slide(zero, 1000, 0, [], 62, 60, 120, 7)
    assert _absolute_pitchwheel_events(zero) == []

    one_tick = g.EventList()
    g.emit_bend(one_tick, 1000, 1, bend, 120, 7)
    assert _absolute_pitchwheel_events(one_tick) == [
        (1000, 0),
        (1000, g.semitones_to_pitchwheel(-2, 7)),
        (1001, 0),
    ]


def test_bend_endpoint_is_held_until_end_of_tied_note():
    """GP bend достигает цели на исходном beat и держит её через tie до note-off."""
    header = GPMeasureHeader(GPTimeSignature(4, GPDenominator(4)))
    bend = GPBend(points=[
        GPBendPoint(position=0, value=0),
        GPBendPoint(position=3, value=4),
        GPBendPoint(position=12, value=4),
    ])
    bent = GPNote(
        string=1, value=8, velocity=95, effect=GPNoteEffect(bend=bend),
        type=NoteType.normal, realValue=0, playedOffset=0, playedDuration=1.0,
    )
    tied = GPNote(
        string=1, value=8, velocity=95, effect=GPNoteEffect(),
        type=NoteType.tie, realValue=0, playedOffset=0, playedDuration=1.0,
    )
    later_attack = GPNote(
        string=2, value=10, velocity=95, effect=GPNoteEffect(),
        type=NoteType.normal, realValue=0, playedOffset=0, playedDuration=1.0,
    )
    beats = [
        GPBeat(
            start=960,
            duration=GPDuration(value=16, tuplet=GPTuplet(), time=240),
            notes=[bent], effect=GPBeatEffect(), status=GPBeatStatus("normal"),
        ),
        GPBeat(
            start=1200,
            duration=GPDuration(value=8, tuplet=GPTuplet(), time=480),
            notes=[tied], effect=GPBeatEffect(), status=GPBeatStatus("normal"),
        ),
        GPBeat(
            start=1440,
            duration=GPDuration(value=16, tuplet=GPTuplet(), time=240),
            notes=[later_attack], effect=GPBeatEffect(), status=GPBeatStatus("normal"),
        ),
    ]
    track = GPTrack(
        name="Solo Guitar", strings=[GPString(1, 64), GPString(2, 59)],
        channel=GPChannel(30), measures=[GPMeasure(960, header, [GPVoice(beats)])],
    )
    song = GPSong("fixture", "", "", 120, [track], [header])

    midi_track, _ = g.build_instrument_midi(
        song, track, "GUITAR", preserve_gp_played_offsets=False,
    )
    events = []
    tick = 0
    for message in midi_track:
        tick += message.time
        if message.type == "pitchwheel":
            events.append((tick, message.pitch))

    assert events == [
        (0, 0),
        (60, g.semitones_to_pitchwheel(2, 7)),
        (239, g.semitones_to_pitchwheel(2, 7)),
        (720, 0),
    ]


def test_index_shows_hydra_35_kontakt_8_pitch_bend_range(monkeypatch):
    import app as web

    monkeypatch.setattr(web, "load_manifest", lambda: {"jobs": [], "current_job_id": None})
    web.app.config.update(TESTING=True)

    response = web.app.test_client().get("/")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Hydra 3.5" in text
    assert "Kontakt Player 8" in text
    assert "PITCH BEND RANGE" in text
    assert "7 полутонов" in text
