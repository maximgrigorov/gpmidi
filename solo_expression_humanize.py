#!/usr/bin/env python3
"""Opt-in expression humanization for an already accepted mapped Solo MIDI.

This deliberately runs on the accepted target-library MIDI rather than rebuilding
from Guitar Pro. Musical note numbers and all note-on/off ticks remain immutable.
The four dimensions are independent:

* velocity: correlated pick variation on musical attacks only;
* pitch bend: smooth interpolation between existing immutable bend anchors;
* modulation: per-envelope CC1 peak and curvature variation at existing ticks.
* finger vibrato: deterministic, irregular micro-bends on eligible long,
  monophonic notes that do not already carry an authored pitch-bend curve.

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
DEFAULT_TEMPO_US_PER_BEAT = 500_000

FINGER_VIBRATO_STYLES: dict[str, dict[str, float | tuple[float, float]]] = {
    "subtle": {
        "min_note_ms": 900.0,
        "probability": 0.55,
        "delay_ms": (320.0, 520.0),
        "depth_cents": (8.0, 18.0),
        "rate_hz": (4.2, 5.2),
    },
    "natural": {
        "min_note_ms": 700.0,
        "probability": 0.75,
        "delay_ms": (220.0, 420.0),
        "depth_cents": (12.0, 26.0),
        "rate_hz": (4.5, 6.0),
    },
    "expressive": {
        "min_note_ms": 550.0,
        "probability": 0.90,
        "delay_ms": (140.0, 300.0),
        "depth_cents": (20.0, 38.0),
        "rate_hz": (4.8, 6.5),
    },
}


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
            or int(message.velocity) > VELOCITY_CEILING
            or int(message.note) in HYDRA_SERVICE_NOTES
        ):
            # velocity > VELOCITY_CEILING — это vel-зоны Hydra на sustain
            # (120-126 = Rake, 127 = Pinch): артикуляция, а не громкость.
            # Зажать такую атаку в 119 значит превратить pinch/rake в обычный
            # громкий sustain — оставляем 120-127 нетронутыми.
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


def _musical_note_spans(
    events: list[tuple[int, int, Any]],
) -> list[dict[str, int]]:
    """Pair musical note-on/off events without treating Hydra KS as notes."""
    active: dict[tuple[int, int], list[tuple[int, int]]] = {}
    spans: list[dict[str, int]] = []
    for tick, index, message in events:
        if message.type not in {"note_on", "note_off"}:
            continue
        note = int(message.note)
        if note in HYDRA_SERVICE_NOTES:
            continue
        velocity = int(getattr(message, "velocity", 0) or 0)
        key = (int(getattr(message, "channel", 0)), note)
        if message.type == "note_on" and velocity > 0:
            active.setdefault(key, []).append((tick, index))
            continue
        starts = active.get(key)
        if not starts:
            continue
        start, start_index = starts.pop(0)
        if not starts:
            active.pop(key, None)
        spans.append({
            "start": start,
            "end": tick,
            "start_index": start_index,
            "end_index": index,
            "channel": key[0],
            "note": note,
        })
    return spans


def _tempo_at_tick(events: list[tuple[int, int, Any]], tick: int) -> int:
    tempo = DEFAULT_TEMPO_US_PER_BEAT
    for event_tick, _index, message in events:
        if event_tick > tick:
            break
        if message.type == "set_tempo":
            tempo = int(message.tempo)
    return tempo


def _ticks_for_ms(milliseconds: float, tempo: int, ticks_per_beat: int) -> int:
    return max(1, round(milliseconds * ticks_per_beat * 1000.0 / tempo))


def _pitchwheel_for_cents(cents: float, pitch_bend_range: float) -> int:
    semitones = cents / 100.0
    value = round(8192.0 * semitones / pitch_bend_range)
    return max(-8192, min(8191, value))


def _finger_vibrato_events(
    events: list[tuple[int, int, Any]],
    *,
    tempo_events: list[tuple[int, int, Any]] | None,
    seed: int,
    ticks_per_beat: int,
    pitch_bend_range: float,
    style: str,
) -> tuple[list[tuple[int, int, Any]], int]:
    """Create small irregular PB motion without overwriting authored bends.

    MIDI pitch bend is channel-wide.  We therefore admit only monophonic spans,
    reject any span with an existing non-zero PB state/event, and reset to zero
    before note-off.  This is intentionally a post-mapped experiment path; it
    never changes notes, timings, service events, or existing bend anchors.
    """
    if pitch_bend_range <= 0:
        raise ValueError("pitch_bend_range must be positive")
    try:
        profile = FINGER_VIBRATO_STYLES[style]
    except KeyError as exc:
        raise ValueError(f"unknown finger vibrato style: {style}") from exc

    spans = _musical_note_spans(events)
    pitch_events = [event for event in events if event[2].type == "pitchwheel"]
    occupied_pitch_ticks = {tick for tick, _index, _message in pitch_events}
    rng = random.Random(f"solo-finger-vibrato:{style}:{seed}")
    extras: list[tuple[int, int, Any]] = []
    serial = 0
    changed_notes = 0

    def append_ramp(
        start_tick: int,
        end_tick: int,
        start_pitch: int,
        end_pitch: int,
        channel: int,
    ) -> int:
        """Append a smooth four-point ramp, avoiding plateaus and collisions."""
        nonlocal serial
        added = 0
        for step in range(1, 5):
            fraction = step / 4.0
            smooth = fraction * fraction * (3.0 - 2.0 * fraction)
            tick = round(start_tick + (end_tick - start_tick) * fraction)
            pitch = round(start_pitch + (end_pitch - start_pitch) * smooth)
            if tick <= start_tick or tick in occupied_pitch_ticks:
                continue
            extras.append((
                tick,
                serial,
                Message("pitchwheel", channel=channel, pitch=pitch),
            ))
            occupied_pitch_ticks.add(tick)
            serial += 1
            added += 1
        return added

    for span in spans:
        start = span["start"]
        end = span["end"]
        if end <= start:
            continue
        if any(
            other is not span and other["start"] < end and start < other["end"]
            for other in spans
        ):
            continue

        tempo = _tempo_at_tick(tempo_events or events, start)
        duration_ms = (end - start) * tempo / (ticks_per_beat * 1000.0)
        if duration_ms < float(profile["min_note_ms"]):
            continue

        pitch_state = 0
        expressive_bend = False
        for event_tick, _index, message in pitch_events:
            if event_tick <= start:
                pitch_state = int(message.pitch)
            if start <= event_tick < end and int(message.pitch) != 0:
                expressive_bend = True
                break
        if pitch_state != 0 or expressive_bend:
            continue
        if rng.random() >= float(profile["probability"]):
            continue

        delay_low, delay_high = profile["delay_ms"]
        depth_low, depth_high = profile["depth_cents"]
        rate_low, rate_high = profile["rate_hz"]
        delay_ms = rng.uniform(float(delay_low), float(delay_high))
        remaining_ms = duration_ms - delay_ms
        if remaining_ms < 180.0:
            continue
        cursor = start + _ticks_for_ms(delay_ms, tempo, ticks_per_beat)
        end_guard = max(cursor + 1, end - _ticks_for_ms(25.0, tempo, ticks_per_beat))
        direction = 1.0
        note_events = 0
        while cursor < end_guard:
            rate_hz = rng.uniform(float(rate_low), float(rate_high))
            quarter_cycle_ms = 250.0 / rate_hz
            quarter_cycle_ticks = _ticks_for_ms(quarter_cycle_ms, tempo, ticks_per_beat)
            peak_tick = min(end_guard, cursor + quarter_cycle_ticks)
            if peak_tick <= cursor:
                break
            depth = rng.uniform(float(depth_low), float(depth_high))
            # A fretted string is bent mainly sharp.  The return half-cycle may
            # dip only slightly below centre, avoiding synthetic symmetric LFO.
            signed_depth = depth if direction > 0 else -(depth * rng.uniform(0.08, 0.22))
            peak_value = _pitchwheel_for_cents(signed_depth, pitch_bend_range)
            if peak_value:
                note_events += append_ramp(
                    cursor, peak_tick, 0, peak_value, span["channel"],
                )
            return_tick = min(end_guard, peak_tick + quarter_cycle_ticks)
            if return_tick > peak_tick:
                note_events += append_ramp(
                    peak_tick, return_tick, peak_value, 0, span["channel"],
                )
            cursor = return_tick
            direction *= -1.0

        if note_events:
            if end not in occupied_pitch_ticks:
                extras.append((
                    end,
                    serial,
                    Message("pitchwheel", channel=span["channel"], pitch=0),
                ))
                serial += 1
            changed_notes += 1
    return extras, changed_notes


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
    finger_vibrato: bool = False,
    finger_vibrato_style: str = "natural",
    pitch_bend_range: float = 7.0,
    tempo_track: Iterable[Any] | None = None,
) -> tuple[MidiTrack, dict[str, Any]]:
    """Return an enriched copy and fail closed if any immutable changed."""
    if not any((velocity, pitch_bend, modulation, finger_vibrato)):
        raise ValueError("at least one Solo humanization dimension must be enabled")
    if ticks_per_beat <= 0:
        raise ValueError("ticks_per_beat must be positive")

    baseline_notes = _note_signature(track)
    baseline_service = _service_signature(track)
    baseline_anchors = _pitch_anchors(track)
    events = [(tick, index, message.copy(time=0)) for tick, index, message in _absolute_events(track)]
    tempo_events = _absolute_events(tempo_track) if tempo_track is not None else None

    velocity_changed = _humanize_velocity(events, seed, ticks_per_beat) if velocity else 0
    modulation_changed = _humanize_modulation(events, seed) if modulation else 0
    pitch_extras = _pitch_interpolations(events, seed, ticks_per_beat) if pitch_bend else []
    finger_extras, finger_notes = (
        _finger_vibrato_events(
            events,
            tempo_events=tempo_events,
            seed=seed,
            ticks_per_beat=ticks_per_beat,
            pitch_bend_range=pitch_bend_range,
            style=finger_vibrato_style,
        )
        if finger_vibrato else ([], 0)
    )
    extras = [*pitch_extras, *finger_extras]
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
        "pitch_interpolation_events_added": len(pitch_extras),
        "finger_vibrato_events_added": len(finger_extras),
        "finger_vibrato_notes": finger_notes,
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
    finger_vibrato: bool = False,
    finger_vibrato_style: str = "natural",
    pitch_bend_range: float = 7.0,
    changed_tracks_only: bool = False,
    track_name: str | None = None,
) -> dict[str, Any]:
    if source.resolve() == output.resolve():
        raise ValueError("source and output paths must be distinct")
    if not any((velocity, pitch_bend, modulation, finger_vibrato)):
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
        finger_vibrato=finger_vibrato,
        finger_vibrato_style=finger_vibrato_style,
        pitch_bend_range=pitch_bend_range,
        tempo_track=(
            baseline.tracks[0]
            if baseline.type == 1 and target_index != 0
            else None
        ),
    )
    candidate.tracks[target_index] = enriched
    for index, signature in before_other.items():
        if _semantic_signature(candidate.tracks[index]) != signature:
            raise RuntimeError(f"non-Solo track {index} changed")

    conductor_preserved = False
    omitted_track_indices: list[int] = []
    if changed_tracks_only:
        output_midi = MidiFile(
            type=1 if baseline.type == 1 else 0,
            ticks_per_beat=baseline.ticks_per_beat,
        )
        conductor_is_meta_only = (
            baseline.type == 1
            and target_index != 0
            and not any(
                message.type in {"note_on", "note_off"}
                for message in baseline.tracks[0]
            )
        )
        if conductor_is_meta_only:
            output_midi.tracks.append(copy.deepcopy(baseline.tracks[0]))
            conductor_preserved = True
        output_midi.tracks.append(enriched)
        if len(output_midi.tracks) == 1:
            output_midi.type = 0
        omitted_track_indices = [
            index for index in range(len(baseline.tracks))
            if index != target_index and not (conductor_preserved and index == 0)
        ]
    else:
        output_midi = candidate

    output.parent.mkdir(parents=True, exist_ok=True)
    output_midi.save(output)
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
            "finger_vibrato": finger_vibrato,
            "finger_vibrato_style": finger_vibrato_style if finger_vibrato else None,
        },
        "pitch_bend_range": pitch_bend_range,
        "output_mode": "changed_tracks_only" if changed_tracks_only else "full_type_1",
        "conductor_track_preserved": conductor_preserved,
        "omitted_track_indices": omitted_track_indices,
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
    parser.add_argument("--finger-vibrato", action="store_true")
    parser.add_argument(
        "--finger-vibrato-style",
        choices=sorted(FINGER_VIBRATO_STYLES),
        default="natural",
    )
    parser.add_argument("--pitch-bend-range", type=float, default=7.0)
    parser.add_argument("--changed-tracks-only", action="store_true")
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
        finger_vibrato=args.finger_vibrato,
        finger_vibrato_style=args.finger_vibrato_style,
        pitch_bend_range=args.pitch_bend_range,
        changed_tracks_only=args.changed_tracks_only,
        track_name=args.track_name,
    )
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.manifest:
        args.manifest.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
