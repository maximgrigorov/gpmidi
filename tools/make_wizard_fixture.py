#!/usr/bin/env python3
"""Build the deterministic synthetic GP5 fixture used by the UI wizard tests."""

from __future__ import annotations

import argparse
from pathlib import Path

import guitarpro

TRACKS = (
    ("Drums", True, 0),
    ("Bass", False, 33),
    ("Guitar (Solo)", False, 29),
    ("Guitar (Rhythm)", False, 30),
    ("Keys", False, 0),
)


def build_song(measures: int = 8) -> guitarpro.Song:
    song = guitarpro.Song()
    song.title = "Wizard synthetic fixture"
    song.artist = "gpmidi tests"
    song.tempo = 120
    while len(song.measureHeaders) < measures:
        song.measureHeaders.append(guitarpro.MeasureHeader())
    del song.measureHeaders[measures:]
    for index, header in enumerate(song.measureHeaders, start=1):
        header.number = index
        header.timeSignature.numerator = 4
        header.timeSignature.denominator.value = 4

    song.tracks = []
    for number, (name, percussion, instrument) in enumerate(TRACKS, start=1):
        track = guitarpro.Track(song, number=number, name=name, isPercussionTrack=percussion)
        track.channel.instrument = instrument
        track.measures = []
        for measure_index, header in enumerate(song.measureHeaders):
            measure = guitarpro.Measure(track, header)
            voice = measure.voices[0]
            voice.beats = []
            for beat_index in range(4):
                beat = guitarpro.Beat(voice)
                beat.duration.value = 4
                # One intentional rest exercises empty beats without making a track empty.
                if not (measure_index == 3 and beat_index == 3):
                    base_value = 0 if percussion else 3 + ((measure_index + beat_index) % 5)
                    note = guitarpro.Note(beat, value=base_value, string=1, velocity=88)
                    if measure_index == 0 and beat_index == 1:
                        note.effect.hammer = True
                    if measure_index == 1 and beat_index == 0:
                        note.effect.palmMute = True
                    if measure_index == 1 and beat_index == 2 and not percussion:
                        note.effect.bend = guitarpro.BendEffect(
                            type=guitarpro.BendType.bend,
                            value=2,
                            points=[guitarpro.BendPoint(0, 0), guitarpro.BendPoint(60, 2)],
                        )
                    beat.notes.append(note)
                    if measure_index == 2 and beat_index == 0 and not percussion:
                        beat.notes.append(guitarpro.Note(beat, value=base_value + 3, string=2, velocity=84))
                voice.beats.append(beat)
            track.measures.append(measure)
        song.tracks.append(track)
    return song


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("tests/fixtures/wizard_fixture.gp5"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    guitarpro.write(build_song(), args.output, version=(5, 1, 0))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
