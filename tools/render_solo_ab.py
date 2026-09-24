#!/usr/bin/env python3
"""Render one guitar track as A/B/C WAVs for a listening check (macOS only).

  A  notation   -- default CLI render options, no humanize
  B  humanize + fret-hand cost
  C  humanize only

Render options follow the CLI defaults (hidden GP 32nds expanded, GP played
offsets kept on solo tracks). Keyswitch notes and program change are stripped,
the rest is played by the macOS GS sampler (GM program, default 30 = Distortion
Guitar) via tools/render_midi_offline.swift. Silences longer than MAX_SILENCE_S
are shortened IDENTICALLY in all three files so A/B/C stay aligned; one gain
for all three so loudness compares.

Reported per variant: verify_midi findings and 50 ms windows where A sounds and
the variant is silent (a lost note is audible there even when the note list is
intact -- see KEY_RETRIGGER).

Usage:
    python tools/render_solo_ab.py SONG.gp "Guitar (Solo)" OUT_DIR [--seed 7] [--program 30]
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import tempfile
import warnings
import wave
from pathlib import Path

import mido
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gp_to_shreddage as g  # noqa: E402
import hand_cost_check as hc  # noqa: E402
from articulation_config import config_for_track_type  # noqa: E402
from gp_import import parse_song  # noqa: E402
from verify_midi import smoke_check  # noqa: E402

RENDERER = Path(__file__).resolve().parent / "render_midi_offline.swift"
VARIANTS = {
    "A_notation": {},
    "B_humanize+hand-cost": {"humanize": True, "fret_hand_cost": True},
    "C_humanize-only": {"humanize": True},
}
MAX_SILENCE_S = 3.0        # longer silences are shortened to 2 * KEEP_S
KEEP_S = 0.75              # kept around every note span
TAIL_S = 2.5               # rendered after the last event
WINDOW_S = 0.05            # dropout window
SILENT_RMS = 100           # int16 RMS below this counts as silence (~ -50 dBFS)
PEAK_DBFS = -1.0


def strip_for_render(midi_track, ks_notes):
    out, pending = mido.MidiTrack(), 0
    for msg in midi_track:
        pending += msg.time
        if (msg.type in ("note_on", "note_off") and msg.note in ks_notes) or msg.type == "program_change":
            continue
        out.append(msg.copy(time=pending))
        pending = 0
    return out


def note_spans_s(path):
    t, sounding, out = 0.0, {}, []
    for msg in mido.MidiFile(path):
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            sounding.setdefault(msg.note, []).append(t)
        elif msg.type in ("note_on", "note_off") and sounding.get(msg.note):
            out.append((sounding[msg.note].pop(0), t))
    return out


def keep_regions(spans):
    regions = []
    for on, off in sorted(spans):
        a, b = max(0.0, on - KEEP_S), off + KEEP_S
        if regions and a <= regions[-1][1] + (MAX_SILENCE_S - 2 * KEEP_S):
            regions[-1][1] = max(regions[-1][1], b)
        else:
            regions.append([a, b])
    return regions


def read_mono(path):
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        return x.reshape(-1, w.getnchannels()).astype(np.float32).mean(axis=1), rate


def rms_windows(x, rate):
    n = int(WINDOW_S * rate)
    return np.sqrt((x[: len(x) // n * n].reshape(-1, n) ** 2).mean(axis=1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("score", type=Path)
    ap.add_argument("track")
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--program", type=int, default=30)
    args = ap.parse_args(argv)
    logging.disable(logging.INFO)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        song = parse_song(str(args.score))
    track = next(t for t in song.tracks if t.name == args.track)
    track_type = g.resolve_track_type(track)
    cfg = config_for_track_type(track_type)
    render = g.resolve_track_render_options(track, track_type, g.parse_cli_options(["prog", "song.gp"]))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = "".join(ch if ch.isalnum() else "_" for ch in f"{args.score.stem}_{args.track}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        binary = tmp / "render"
        subprocess.run(["swiftc", "-O", str(RENDERER), "-o", str(binary)], check=True)
        spans, audio, findings = [], {}, {}
        for name, kwargs in VARIANTS.items():
            midi_track, _ = g.build_instrument_midi(song, track, track_type, humanize_seed=args.seed,
                                                    **render, **kwargs)
            full = tmp / f"{name}_full.mid"
            midi = mido.MidiFile(type=0, ticks_per_beat=g.TICKS_PER_BEAT)
            midi.tracks.append(midi_track)
            midi.save(full)
            findings[name] = [(sev, code) for sev, code, _m in smoke_check(full, track_type)]
            playable = tmp / f"{name}.mid"
            out = mido.MidiFile(type=0, ticks_per_beat=g.TICKS_PER_BEAT)
            out.tracks.append(strip_for_render(midi_track, hc._keyswitch_notes(cfg)))
            out.save(playable)
            spans += note_spans_s(playable)
            raw = tmp / f"{name}.wav"
            subprocess.run([str(binary), str(playable), str(raw), str(args.program), str(TAIL_S)],
                           check=True, capture_output=True)
            audio[name] = read_mono(raw)

    regions = keep_regions(spans)
    rate = next(iter(audio.values()))[1]
    gain = (10 ** (PEAK_DBFS / 20) * 32767) / max(np.abs(x).max() for x, _ in audio.values())
    cut = {}
    for name, (x, _) in audio.items():
        y = np.concatenate([x[int(a * rate):int(b * rate)] for a, b in regions]) * gain
        cut[name] = y
        with wave.open(str(args.out_dir / f"{stem}_{name}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(np.clip(y, -32768, 32767).astype(np.int16).tobytes())
    reference = rms_windows(cut["A_notation"], rate)
    print(f"{args.score.name} / {args.track}: render options {render}, "
          f"{len(cut['A_notation']) / rate:.0f} s after shortening silences")
    for name, y in cut.items():
        silent = int(((reference > SILENT_RMS) & (rms_windows(y, rate) <= SILENT_RMS)).sum())
        print(f"  {name:22} dropouts vs A: {silent:3} windows   verify: {findings[name] or 'clean'}")


if __name__ == "__main__":
    main()
