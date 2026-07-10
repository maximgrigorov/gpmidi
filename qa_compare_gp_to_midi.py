from __future__ import annotations

import json
from pathlib import Path
from collections import Counter

from mido import MidiFile

import app
from articulation_config import config_for_track_type

# Все keyswitch-ноты обоих инструментов — из версионируемых конфигов
KEYSWITCH_NOTES = {
    int(spec["note"])
    for track_type in ("GUITAR", "BASS")
    for spec in config_for_track_type(track_type)["keyswitches"].values()
}


def extract_gp_sounding_notes(song, track_name: str):
    track = next(t for t in song.tracks if t.name == track_name)
    # B3: эталонная высота ноты считается из string+fret (настройка струны +
    # лад), а НЕ из note.realValue: у ApolloTab realValue (midi_pitch) смещён
    # на целое число октав (+24 в pnd, +12 в ttn) для GP8-файлов.
    string_pitch = {s.number: s.value for s in track.strings}
    notes = []
    for measure_index, measure in enumerate(track.measures, start=1):
        for voice in measure.voices:
            tick = 0
            for beat in voice.beats:
                duration = int(getattr(beat.duration, "time", 0) or 0)
                if beat.status.name == "normal":
                    for note in beat.notes:
                        if getattr(note.type, "name", "") != "tie":
                            pitch = string_pitch.get(note.string, 0) + note.value
                            notes.append(
                                {
                                    "measure": measure_index,
                                    "tick": tick,
                                    "pitch": max(0, min(127, pitch)),
                                    "duration": duration,
                                }
                            )
                tick += duration
    return notes


def extract_midi_notes(midi_path: Path):
    midi = MidiFile(str(midi_path))
    abs_tick = 0
    active: dict[int, list[int]] = {}
    notes = []
    for msg in midi.tracks[0]:
        abs_tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and msg.note not in KEYSWITCH_NOTES:
            active.setdefault(msg.note, []).append(abs_tick)
        elif msg.type in {"note_off", "note_on"} and (msg.type == "note_off" or msg.velocity == 0):
            stack = active.get(msg.note) or []
            if stack:
                start_tick = stack.pop(0)
                notes.append(
                    {
                        "start_tick": start_tick,
                        "end_tick": abs_tick,
                        "pitch": msg.note,
                        "duration": abs_tick - start_tick,
                    }
                )
    return notes


def build_export(song, workdir: Path, track_name: str):
    out_dir = workdir / "out"
    job_dir = workdir / "job"
    out_dir.mkdir(parents=True, exist_ok=True)
    job_dir.mkdir(parents=True, exist_ok=True)
    with app.app.test_request_context("/"):
        tracks = app.build_track_summary(song, out_dir, job_dir, "qa")
    solo = next(t for t in tracks if t["track_name"] == track_name)
    preview_path = job_dir / "preview" / f"{Path(solo['download_name']).stem}.json"
    preview = json.loads(preview_path.read_text())
    midi_notes = extract_midi_notes(out_dir / solo["download_name"])
    return solo, preview, midi_notes


def main(src: str = "pnd.gp5", track_name: str = "Solo Guitar"):
    import sys
    from gp_import import parse_song
    if len(sys.argv) > 1:
        src = sys.argv[1]
    if len(sys.argv) > 2:
        track_name = sys.argv[2]
    song = parse_song(src)
    gp_notes = extract_gp_sounding_notes(song, track_name)
    solo, preview, midi_notes = build_export(song, Path("/tmp/gpmidi-qa"), track_name)

    report = {
        "track": track_name,
        "source_gp_sounding_notes": len(gp_notes),
        "exported_midi_notes": len(midi_notes),
        "preview_notes": preview["note_count"],
        "counts_match": len(gp_notes) == len(midi_notes) == preview["note_count"],
        "gp_measure_counts": dict(Counter(n["measure"] for n in gp_notes if n["measure"] in (65, 66))),
        "preview_articulations": preview.get("articulation_counts", {}),
        "download_name": solo["download_name"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
