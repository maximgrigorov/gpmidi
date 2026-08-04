#!/usr/bin/env python3
"""Opt-in expression humanization for an already accepted mapped Solo MIDI.

This deliberately runs on the accepted target-library MIDI rather than rebuilding
from Guitar Pro. Musical note numbers and all note-on/off ticks remain immutable.
The three dimensions are independent:

* velocity: correlated pick variation on musical attacks only;
* pitch bend: smooth interpolation between existing immutable bend anchors;
* modulation: per-envelope CC1 peak and curvature variation at existing ticks.

Hydra service notes are never edited. With no dimension flag the command fails
instead of silently producing a misleading "humanized" copy.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from mido import Message, MidiFile, MidiTrack

HYDRA_SERVICE_NOTES = frozenset(
    set(range(0, 7))
    | {10, 11}
    | set(range(12, 22))
    | set(range(23, 28))
    | {114, 115}
)
VELOCITY_FLOOR = 20
VELOCITY_CEILING = 119
PITCH_STEPS_PER_BEAT = 40


def _absolute_events(track: Iterable[Any]) -> list[tuple[int, int, Any]]:
    tick = 0
    events = []
    for index, message in enumerate(track):
        tick += int(message.time)
        events.append((tick, index, message))
    return events


def _note_signature(track: Iterable[Any]) -> list[tuple[int, str, int, int]]:
    signature = []
    for tick, _index, message in _absolute_events(track):
        if message.type not in {"note_on", "note_off"}:
            continue
        velocity = int(getattr(message, "velocity", 0) or 0)
        semantic = "off" if message.type == "note_off" or velocity == 0 else "on"
        signature.append((tick, semantic, int(message.note), int(getattr(message, "channel", 0))))
    return signature


def _service_signature(track: Iterable[Any]) -> list[tuple[int, str, int, int, int]]:
    return [
        (tick, message.type, int(message.note), int(message.velocity), int(message.channel))
        for tick, _index, message in _absolute_events(track)
        if message.type in {"note_on", "note_off"}
        and int(message.note) in HYDRA_SERVICE_NOTES
    ]


def _pitch_anchors(track: Iterable[Any]) -> list[tuple[int, int, int]]:
    return [
        (tick, int(message.pitch), int(message.channel))
        for tick, _index, message in _absolute_events(track)
        if message.type == "pitchwheel"
    ]


def _semantic_signature(track: Iterable[Any]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (tick, {key: value for key, value in message.dict().items() if key != "time"})
        for tick, _index, message in _absolute_events(track)
    ]


def _rebuild_track(
    originals: list[tuple[int, int, Any]], extras: list[tuple[int, int, Any]],
) -> MidiTrack:
    # Existing same-tick ordering is immutable. Interpolated PB events receive a
    # negative ordering key and therefore establish the intended bend before an
    # original note that happens to start at that intermediate tick.
    merged = [(tick, 0, index, message.copy(time=0)) for tick, index, message in originals]
    merged.extend((tick, -1, index, message.copy(time=0)) for tick, index, message in extras)
    merged.sort(key=lambda row: (row[0], row[1], row[2]))
    result = MidiTrack()
    previous_tick = 0
    for tick, _kind, _index, message in merged:
        result.append(message.copy(time=tick - previous_tick))
        previous_tick = tick
    return result


def _humanize_velocity(
    events: list[tuple[int, int, Any]], seed: int, ticks_per_beat: int,
) -> int:
    rng = random.Random(f"solo-velocity:{seed}")
    drift = 0.0
    changed = 0
    for tick, _index, message in events:
        if (
            message.type != "note_on"
            or int(message.velocity) <= 0
            or int(message.note) in HYDRA_SERVICE_NOTES
        ):
            continue
        phase = tick % ticks_per_beat
        eighth = round(ticks_per_beat / 2)
        sixteenth = round(ticks_per_beat / 4)
        three_sixteenths = round(3 * ticks_per_beat / 4)
        if phase == 0:
            accent = 3.0
        elif phase == eighth:
            accent = 1.0
        elif phase in {sixteenth, three_sixteenths}:
            accent = -1.0
        else:
            accent = 0.0
        drift = (0.68 * drift) + rng.gauss(0.0, 2.4)
        before = int(message.velocity)
        after = max(VELOCITY_FLOOR, min(VELOCITY_CEILING, round(before + accent + drift)))
        if after == before:
            # A zero rounded delta creates a visible flat spot. A deterministic
            # one-step pick change is still far below an articulation boundary.
            after = before + (1 if rng.random() >= 0.5 else -1)
            after = max(VELOCITY_FLOOR, min(VELOCITY_CEILING, after))
        if after != before:
            message.velocity = after
            changed += 1
    return changed


def _pitch_interpolations(
    events: list[tuple[int, int, Any]], seed: int, ticks_per_beat: int,
) -> list[tuple[int, int, Any]]:
    anchors = [event for event in events if event[2].type == "pitchwheel"]
    rng = random.Random(f"solo-pitch:{seed}")
    extras: list[tuple[int, int, Any]] = []
    serial = 0
    step_ticks = max(1, round(ticks_per_beat / PITCH_STEPS_PER_BEAT))
    for (left_tick, _left_index, left), (right_tick, _right_index, right) in zip(anchors, anchors[1:]):
        gap = right_tick - left_tick
        if gap < step_ticks * 2 or int(left.pitch) == int(right.pitch):
            continue
        exponent = rng.uniform(0.88, 1.12)
        for tick in range(left_tick + step_ticks, right_tick, step_ticks):
            fraction = (tick - left_tick) / gap
            smooth = fraction * fraction * (3.0 - 2.0 * fraction)
            shaped = smooth**exponent
            pitch = round(int(left.pitch) + (int(right.pitch) - int(left.pitch)) * shaped)
            if pitch in {int(left.pitch), int(right.pitch)}:
                continue
            extras.append((tick, serial, Message("pitchwheel", channel=int(left.channel), pitch=pitch)))
            serial += 1
    return extras


def _humanize_modulation(events: list[tuple[int, int, Any]], seed: int) -> int:
    cc1 = [event for event in events if event[2].type == "control_change" and int(event[2].control) == 1]
    positive_runs: list[list[tuple[int, int, Any]]] = []
    current: list[tuple[int, int, Any]] = []
    for event in cc1:
        if int(event[2].value) > 0:
            current.append(event)
        elif current:
            positive_runs.append(current)
            current = []
    if current:
        positive_runs.append(current)

    rng = random.Random(f"solo-modulation:{seed}")
    changed = 0
    for run in positive_runs:
        peak = max(int(event[2].value) for event in run)
        scale = rng.uniform(0.82, 1.08)
        exponent = rng.uniform(0.86, 1.18)
        new_peak = max(1, min(127, round(peak * scale)))
        for _tick, _index, message in run:
            before = int(message.value)
            normalized = before / peak
            after = max(1, min(127, round(new_peak * (normalized**exponent))))
            if after != before:
                message.value = after
                changed += 1
    return changed


def humanize_solo_track(
    track: MidiTrack,
    *,
    ticks_per_beat: int,
    seed: int,
    velocity: bool = False,
    pitch_bend: bool = False,
    modulation: bool = False,
) -> tuple[MidiTrack, dict[str, Any]]:
    """Return an enriched copy and fail closed if any immutable changed."""
    if not any((velocity, pitch_bend, modulation)):
        raise ValueError("at least one Solo humanization dimension must be enabled")
    if ticks_per_beat <= 0:
        raise ValueError("ticks_per_beat must be positive")

    baseline_notes = _note_signature(track)
    baseline_service = _service_signature(track)
    baseline_anchors = _pitch_anchors(track)
    events = [(tick, index, message.copy(time=0)) for tick, index, message in _absolute_events(track)]

    velocity_changed = _humanize_velocity(events, seed, ticks_per_beat) if velocity else 0
    modulation_changed = _humanize_modulation(events, seed) if modulation else 0
    extras = _pitch_interpolations(events, seed, ticks_per_beat) if pitch_bend else []
    enriched = _rebuild_track(events, extras)

    if _note_signature(enriched) != baseline_notes:
        raise RuntimeError("Solo note timing, pitch, duration, or order changed")
    if _service_signature(enriched) != baseline_service:
        raise RuntimeError("Hydra service notes changed")
    enriched_anchors = Counter(_pitch_anchors(enriched))
    if any(enriched_anchors[anchor] < count for anchor, count in Counter(baseline_anchors).items()):
        raise RuntimeError("existing pitch-bend anchor changed")

    return enriched, {
        "velocity_events_changed": velocity_changed,
        "pitch_events_added": len(extras),
        "modulation_events_changed": modulation_changed,
        "note_timing_preserved": True,
        "note_pitches_preserved": True,
        "note_durations_preserved": True,
        "hydra_service_notes_preserved": True,
        "pitch_anchors_preserved": True,
    }


def _track_name(track: Iterable[Any]) -> str:
    for message in track:
        if message.type == "track_name":
            return str(message.name)
    return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def process_file(
    source: Path,
    output: Path,
    *,
    seed: int,
    velocity: bool = False,
    pitch_bend: bool = False,
    modulation: bool = False,
    track_name: str | None = None,
) -> dict[str, Any]:
    if source.resolve() == output.resolve():
        raise ValueError("source and output paths must be distinct")
    if not any((velocity, pitch_bend, modulation)):
        raise ValueError("at least one Solo humanization dimension must be enabled")
    baseline = MidiFile(source)
    candidate = copy.deepcopy(baseline)
    candidates = [
        index for index, track in enumerate(baseline.tracks)
        if (track_name and _track_name(track) == track_name)
        or (not track_name and any(word in _track_name(track).casefold() for word in ("solo", "lead")))
    ]
    if len(candidates) != 1:
        raise ValueError(f"expected exactly one Solo track, found {len(candidates)}")
    target_index = candidates[0]
    before_other = {
        index: _semantic_signature(track)
        for index, track in enumerate(baseline.tracks)
        if index != target_index
    }
    enriched, stats = humanize_solo_track(
        baseline.tracks[target_index],
        ticks_per_beat=baseline.ticks_per_beat,
        seed=seed,
        velocity=velocity,
        pitch_bend=pitch_bend,
        modulation=modulation,
    )
    candidate.tracks[target_index] = enriched
    for index, signature in before_other.items():
        if _semantic_signature(candidate.tracks[index]) != signature:
            raise RuntimeError(f"non-Solo track {index} changed")

    output.parent.mkdir(parents=True, exist_ok=True)
    candidate.save(output)
    return {
        "schema_version": 1,
        "mode": "solo_expression_humanization",
        "source": str(source),
        "output": str(output),
        "source_sha256": _sha256(source),
        "output_sha256": _sha256(output),
        "target_track": _track_name(baseline.tracks[target_index]),
        "target_track_index": target_index,
        "seed": seed,
        "dimensions": {
            "velocity": velocity,
            "pitch_bend": pitch_bend,
            "modulation": modulation,
        },
        "stats": stats,
        "safety": {
            "note_timing_preserved": True,
            "note_pitches_preserved": True,
            "note_durations_preserved": True,
            "hydra_service_notes_preserved": True,
            "non_solo_tracks_preserved": True,
            "pitch_anchors_preserved": True,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--track-name")
    parser.add_argument("--velocity", action="store_true")
    parser.add_argument("--pitch-bend", action="store_true")
    parser.add_argument("--modulation", action="store_true")
    parser.add_argument("--manifest", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = process_file(
        args.source.resolve(), args.output.resolve(),
        seed=args.seed,
        velocity=args.velocity,
        pitch_bend=args.pitch_bend,
        modulation=args.modulation,
        track_name=args.track_name,
    )
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.manifest:
        args.manifest.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
