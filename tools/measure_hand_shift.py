#!/usr/bin/env python3
"""Step-0 input measurement for the fret-hand cost task.

Distribution of |hand_position - previous_hand_position| on GUITAR tracks,
measured the way ``build_instrument_midi`` sees it:

- hand_position is ``gp_to_shreddage.beat_hand_position``: the median fret of a
  beat's fretted (> 0), non-tie notes;
- beats without fretted notes leave the previous position unchanged;
- previous_hand_position is ONE variable per track, carried across voices in
  the converter's iteration order (measure -> voice -> beat).

The previous-position chaining is replicated here and cross-checked against
the converter itself: with the Hydra
``fret_noise_on_hand_shift.probability`` forced to 1.0, ``fret_noise_events``
must equal the number of beats at or above ``min_fret_shift``. A mismatch
aborts the run -- a measurement of something other than what the converter
does is worthless.

Usage:
    python tools/measure_hand_shift.py FILE [FILE ...] [--json OUT]
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import warnings
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gp_to_shreddage as g  # noqa: E402
from articulation_config import config_for_track_type  # noqa: E402
from gp_import import parse_song  # noqa: E402

FAST_IOI_MS = 150.0
IOI_EDGES_MS = (150.0, 300.0, 600.0)


def track_transitions(song, track):
    """Yield one record per beat that has both a hand position and a predecessor."""
    base_bpm = float(song.tempo) if song.tempo else 120.0
    last_bpm = cur_bpm = base_bpm
    bar_of = {id(m): i for i, m in enumerate(track.measures, start=1)}
    prev = None  # (hand_position, tick, voice_index, end_tick, legato_source)
    for measure, _voice, vi, bi, beat, _mst, tick, dur in \
            g.iter_voice_beats_with_canonical_ticks(track):
        if beat is None:
            continue
        mtc = getattr(beat.effect, "mixTableChange", None)
        if mtc is not None and getattr(mtc, "tempo", None):
            new_bpm = mtc.tempo.value
            if new_bpm and new_bpm != last_bpm:
                last_bpm = cur_bpm = float(new_bpm)
        if not beat.notes:
            continue
        pos = g.beat_hand_position(beat)
        if pos is None:
            continue
        if prev is not None:
            p_pos, p_tick, p_vi, p_end, p_legato = prev
            yield {
                "measure": bar_of[id(measure)], "voice": vi, "beat": bi, "tick": tick,
                "delta": abs(pos - p_pos),
                "ioi_ms": g.ticks_to_ms(tick - p_tick, cur_bpm),
                # silence between the previous fretted beat's grid end and this attack
                "gap_ms": g.ticks_to_ms(max(0, tick - p_end), cur_bpm),
                # previous beat slides or hammers INTO this one: the move is the
                # legato itself, this attack is not re-picked
                "legato_target": p_legato,
                "cross_voice": vi != p_vi,
                "backwards_in_time": tick <= p_tick,
            }
        legato = any(n.effect.slides or n.effect.hammer for n in beat.notes
                     if n.type != g.NoteType.tie)
        prev = (pos, tick, vi, tick + dur, legato)


def converter_shift_count(song, track, track_type):
    """fret_noise_events with probability 1.0 == beats with delta >= threshold."""
    cfg = copy.deepcopy(config_for_track_type(track_type))
    cfg["performance_life"]["fret_noise_on_hand_shift"]["probability"] = 1.0
    _, stats = g.build_instrument_midi(song, track, track_type, cfg=cfg,
                                       fret_noise_on_hand_shift=True)
    return stats["fret_noise_events"]


def summarize(rows, threshold):
    n = len(rows)
    zero = sum(1 for r in rows if r["delta"] == 0)
    shift = sum(1 for r in rows if r["delta"] >= threshold)
    small = n - zero - shift

    def pct(x, d):
        return round(100.0 * x / d, 2) if d else None

    return {"beats": n, "zero": zero, "small": small, "shift": shift,
            "zero_pct": pct(zero, n), "small_pct": pct(small, n),
            "shift_pct": pct(shift, n)}


def ioi_bucket(ms):
    for edge in IOI_EDGES_MS:
        if ms < edge:
            return f"<{int(edge)}"
    return f">={int(IOI_EDGES_MS[-1])}"


def analyze(path, threshold):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        song = parse_song(str(path))
    out = {"file": path.name,
           "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
           "tracks": []}
    for track in song.tracks:
        if g.resolve_track_type(track) != g.TRACK_GUITAR:
            continue
        rows = list(track_transitions(song, track))
        if not rows:
            continue
        measured = sum(1 for r in rows if r["delta"] >= threshold)
        converter = converter_shift_count(song, track, g.TRACK_GUITAR)
        if measured != converter:
            raise SystemExit(f"{path.name} / {track.name}: replica counts {measured} "
                             f"shift beats, converter emits {converter} fret noises")
        shifts = [r for r in rows if r["delta"] >= threshold]
        out["tracks"].append({
            "name": track.name,
            "all": summarize(rows, threshold),
            "fast": summarize([r for r in rows if r["ioi_ms"] < FAST_IOI_MS], threshold),
            "same_voice_forward": summarize(
                [r for r in rows if not r["cross_voice"]], threshold),
            "cross_voice_pairs": sum(1 for r in rows if r["cross_voice"]),
            "backwards_in_time_pairs": sum(1 for r in rows if r["backwards_in_time"]),
            "shift_magnitude": dict(sorted(Counter(
                min(int(r["delta"]), 12) for r in shifts).items())),
            "shift_ioi_ms": dict(Counter(ioi_bucket(r["ioi_ms"]) for r in shifts)),
            "shift_after_silence": sum(1 for r in shifts if r["gap_ms"] > 0),
            "shift_legato_or_slide_target": sum(1 for r in shifts if r["legato_target"]),
            "converter_fret_noise_events_at_p1": converter,
        })
    return out


def total(results, key):
    agg = Counter()
    for res in results:
        for tr in res["tracks"]:
            for field in ("beats", "zero", "small", "shift"):
                agg[field] += tr[key][field]
    n = agg["beats"]
    return {**agg, "shift_pct": round(100.0 * agg["shift"] / n, 2) if n else None,
            "zero_pct": round(100.0 * agg["zero"] / n, 2) if n else None}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)

    cfg = config_for_track_type(g.TRACK_GUITAR)
    threshold = float(cfg["performance_life"]["fret_noise_on_hand_shift"]["min_fret_shift"])
    results = [analyze(p, threshold) for p in args.files]

    print(f"threshold min_fret_shift = {threshold:g}  fast = IOI < {FAST_IOI_MS:g} ms")
    hdr = f"{'file / track':44} {'beats':>6} {'zero%':>7} {'1-3%':>7} {'>=4%':>7}" \
          f" {'fast':>6} {'fast>=4%':>9} {'xvoice':>7}"
    print(hdr)
    for res in results:
        for tr in res["tracks"]:
            a, f = tr["all"], tr["fast"]
            label = f"{res['file'][:22]} / {tr['name'][:18]}"
            print(f"{label:44} {a['beats']:6} {a['zero_pct']:7} {a['small_pct']:7} "
                  f"{a['shift_pct']:7} {f['beats']:6} {str(f['shift_pct']):>9} "
                  f"{tr['cross_voice_pairs']:7}")
    report = {
        "threshold_min_fret_shift": threshold,
        "fast_ioi_ms": FAST_IOI_MS,
        "total_all": total(results, "all"),
        "total_fast": total(results, "fast"),
        "total_same_voice_forward": total(results, "same_voice_forward"),
        "files": results,
    }
    print("TOTAL all :", report["total_all"])
    print("TOTAL fast:", report["total_fast"])
    print("TOTAL same-voice:", report["total_same_voice_forward"])
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    return report


if __name__ == "__main__":
    main()
