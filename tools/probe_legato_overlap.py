#!/usr/bin/env python3
"""Count hammer/pull legato overlaps that survive the guitar export.

A hammer SOURCE is extended into its TARGET (next note on the same voice and
string) only when ``origin["tick"] >= on_tick`` in ``build_instrument_midi``;
otherwise the converter treats the pair as separated by a rest and leaves a
gap, and Hydra re-picks the target instead of playing legato.

Only pairs the score makes bridgeable are counted (the source still sounds at
the target's grid attack). Each score note is matched to the MIDI note with the
same pitch and the nearest attack, so strum/humanize shifts do not misalign it.

Usage:
    python tools/probe_legato_overlap.py FILE "TRACK NAME" [--seeds 7 11 23]
"""
from __future__ import annotations

import argparse
import statistics
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gp_to_shreddage as g  # noqa: E402
from gp_import import parse_song  # noqa: E402


def midi_notes(track):
    tick, sounding, out = 0, {}, []
    for msg in track:
        tick += msg.time
        if msg.type not in ("note_on", "note_off") or msg.note < 24:  # skip keyswitches
            continue
        if msg.type == "note_on" and msg.velocity > 0:
            sounding.setdefault(msg.note, []).append(tick)
        elif sounding.get(msg.note):
            out.append((sounding[msg.note].pop(0), tick, msg.note))
    return out


def bridgeable_pairs(track, cfg):
    strings = {s.number: s.value for s in track.strings}
    pending, pairs = {}, []
    for _m, _v, vi, _bi, beat, _mst, tick, dur in g.iter_voice_beats_with_canonical_ticks(track):
        if beat is None:
            continue
        for note in beat.notes:
            if note.type == g.NoteType.tie:
                continue
            pitch = g.shreddage_harmonic_pitch(note, strings, cfg)
            if pitch is None:
                pitch = g.clamp_note(strings.get(note.string, 0) + note.value)
            key = (vi, note.string)
            source = pending.pop(key, None)
            if source is not None and source[2] >= tick:
                pairs.append((source[0], source[1], pitch, tick))
            if note.effect.hammer:
                pending[key] = (pitch, tick, tick + dur)
    return pairs


def nearest(notes, pitch, tick):
    return min((n for n in notes if n[2] == pitch), key=lambda n: abs(n[0] - tick))


def probe(song, track, humanize, seed, **kwargs):
    cfg = g.config_for_track_type(g.TRACK_GUITAR)
    pairs = bridgeable_pairs(track, cfg)
    midi, _ = g.build_instrument_midi(song, track, g.TRACK_GUITAR, humanize=humanize,
                                      humanize_seed=seed, **kwargs)
    notes = midi_notes(midi)
    bpm = float(song.tempo) if song.tempo else 120.0
    gaps = []
    for s_pitch, s_tick, t_pitch, t_tick in pairs:
        src, tgt = nearest(notes, s_pitch, s_tick), nearest(notes, t_pitch, t_tick)
        if src[1] <= tgt[0]:
            gaps.append(g.ticks_to_ms(tgt[0] - src[1], bpm))
    return {"humanize": humanize, "seed": seed, "pairs": len(pairs), "broken": len(gaps),
            "gap_ms_median": round(statistics.median(gaps), 1) if gaps else None,
            "gap_ms_max": round(max(gaps), 1) if gaps else None}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", type=Path)
    ap.add_argument("track")
    ap.add_argument("--seeds", type=int, nargs="+", default=[7, 11, 23])
    args = ap.parse_args(argv)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        song = parse_song(str(args.file))
    track = next(t for t in song.tracks if t.name == args.track)
    rows = [probe(song, track, False, args.seeds[0])]
    rows += [probe(song, track, True, seed) for seed in args.seeds]
    for row in rows:
        print(row)
    return rows


if __name__ == "__main__":
    main()
