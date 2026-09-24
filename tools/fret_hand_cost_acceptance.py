#!/usr/bin/env python3
"""Acceptance numbers for --fret-hand-cost on real Guitar Pro files.

For every GUITAR track, three renders with the same seed:
  plain     -- no flags (quantized notation)
  humanize  -- --humanize (the feature OFF)
  hand      -- --humanize --fret-hand-cost

Reported per render: the differential check (hand_cost_check.differential),
onset_std_ms (deviation from the notated attack), jitter_ms (LESSONS.md p.3,
kept for continuity; it counts quantized 32nds as jitter) and verify_midi
findings. Between humanize and hand: invariants that must hold by
construction (same notes, same note-offs, attacks only later, no new
overlaps, identical fret-noise events). Across seeds: how often the check
passes with and without the feature.

Usage:
    python tools/fret_hand_cost_acceptance.py SONG.gp [...] --json OUT [--seeds N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import statistics
import sys
import tempfile
import warnings
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mido import MidiFile  # noqa: E402

import gp_to_shreddage as g  # noqa: E402
import hand_cost_check as hc  # noqa: E402
from articulation_config import config_for_track_type  # noqa: E402
from gp_import import parse_song  # noqa: E402
from verify_midi import smoke_check  # noqa: E402

SEED = 7
VARIANTS = {
    "plain": {},
    "humanize": {"humanize": True},
    "hand": {"humanize": True, "fret_hand_cost": True},
}


def notes(midi_track, ks_notes):
    tick, sounding, out = 0, {}, []
    for msg in midi_track:
        tick += msg.time
        if msg.type not in ("note_on", "note_off") or msg.note in ks_notes:
            continue
        if msg.type == "note_on" and msg.velocity > 0:
            sounding.setdefault(msg.note, []).append(tick)
        elif sounding.get(msg.note):
            out.append((sounding[msg.note].pop(0), tick, msg.note))
    return sorted(out)


def overlaps(rows):
    """Consecutive attacks that start before the previous note released."""
    return sum(1 for a, b in zip(rows, rows[1:]) if b[0] < a[1])


def note_events(midi_track, pitch):
    tick, out = 0, []
    for msg in midi_track:
        tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and msg.note == pitch:
            out.append(tick)
    return out


def smoke_codes(midi_track):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.mid"
        midi = MidiFile(type=0, ticks_per_beat=g.TICKS_PER_BEAT)
        midi.tracks.append(midi_track)
        midi.save(path)
        return dict(Counter(code for _sev, code, _msg in smoke_check(path, g.TRACK_GUITAR)))


def track_report(song, track, cfg, seeds):
    ks = hc._keyswitch_notes(cfg)
    built = {name: g.build_instrument_midi(song, track, g.TRACK_GUITAR, humanize_seed=SEED, **kw)
             for name, kw in VARIANTS.items()}
    report = {"track": track.name, "variants": {}}
    for name, (midi_track, stats) in built.items():
        diff = hc.differential(song, track, midi_track, cfg)
        report["variants"][name] = {
            "differential": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in diff.items()},
            "jitter_ms": hc.jitter_ms(midi_track, cfg),
            "smoke": smoke_codes(midi_track),
            "notes": stats["notes"],
            **{k: v for k, v in stats.items() if k.startswith("fret_hand_cost")},
        }
    before, after = notes(built["humanize"][0], ks), notes(built["hand"][0], ks)
    shifts = [b[0] - a[0] for a, b in zip(before, after)]
    fret_note = cfg["fx_keyswitches"]["fret_noise"]
    fn_off, fn_off_stats = g.build_instrument_midi(song, track, g.TRACK_GUITAR, humanize=True,
                                                   humanize_seed=SEED, fret_noise_on_hand_shift=True)
    fn_on, fn_on_stats = g.build_instrument_midi(song, track, g.TRACK_GUITAR, humanize=True,
                                                 humanize_seed=SEED, fret_noise_on_hand_shift=True,
                                                 fret_hand_cost=True)
    report["invariants_humanize_vs_hand"] = {
        "same_pitches_in_order": [p for *_x, p in before] == [p for *_x, p in after],
        "same_note_offs": [off for _on, off, _p in before] == [off for _on, off, _p in after],
        "min_attack_shift_ticks": min(shifts) if shifts else 0,
        "attacks_delayed": sum(1 for s in shifts if s > 0),
        "overlaps_humanize": overlaps(before),
        "overlaps_hand": overlaps(after),
        "fret_noise_events": [fn_off_stats["fret_noise_events"], fn_on_stats["fret_noise_events"]],
        "fret_noise_ticks_identical": note_events(fn_off, fret_note) == note_events(fn_on, fret_note),
    }
    passes = {"humanize": 0, "hand": 0}
    stds = {"humanize": [], "hand": []}
    for seed in seeds:
        for name in passes:
            midi_track, _ = g.build_instrument_midi(song, track, g.TRACK_GUITAR,
                                                    humanize_seed=seed, **VARIANTS[name])
            result = hc.differential(song, track, midi_track, cfg)
            passes[name] += result["passed"]
            stds[name].append(result["onset_std_ms"])
    report["seeds"] = {
        "n": len(seeds),
        "check_passes_feature_off": passes["humanize"],
        "check_passes_feature_on": passes["hand"],
        "onset_std_ms_mean_feature_off": round(statistics.mean(stds["humanize"]), 2),
        "onset_std_ms_mean_feature_on": round(statistics.mean(stds["hand"]), 2),
    }
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--json", type=Path, required=True)
    ap.add_argument("--seeds", type=int, default=60)
    args = ap.parse_args(argv)
    logging.disable(logging.INFO)
    cfg = config_for_track_type(g.TRACK_GUITAR)
    seeds = list(range(1, args.seeds + 1))
    files = []
    for path in args.files:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            song = parse_song(str(path))
        tracks = [track_report(song, t, cfg, seeds) for t in song.tracks
                  if g.resolve_track_type(t) == g.TRACK_GUITAR]
        files.append({"file": path.name,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "tracks": tracks})
        for tr in tracks:
            v = tr["variants"]
            print(f"{path.name[:16]:16} {tr['track'][:16]:16} "
                  + " ".join(f"{n}:pass={v[n]['differential']['passed']!s:5}"
                             f"/std={v[n]['differential']['onset_std_ms']}" for n in VARIANTS)
                  + f" seeds on/off={tr['seeds']['check_passes_feature_on']}"
                    f"/{tr['seeds']['check_passes_feature_off']}")
    args.json.write_text(json.dumps({"seed": SEED, "files": files}, indent=1,
                                    ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
