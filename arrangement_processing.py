"""Safe Hermes draft/apply primitives for whole-arrangement expression.

The caller must first render Guitar Pro through the target-library converter.
This module never calls a model and never invents notes. It can:

1. summarize already mapped Shreddage/Hydra/Darkwall MIDI into a compact draft
   context without mutating the tracks;
2. validate a human/Hermes-authored expression plan as untrusted JSON;
3. apply bounded velocity changes while preserving timing, pitches, service
   events, and Hydra's reserved high-velocity articulation zone.

Solo microtiming is intentionally not a post-pass. When an approved plan enables
it, the workflow must rebuild that Solo/Lead track with the existing pre-mapping
humanizer so keyswitches, bends, ties and note boundaries inherit the same shift.
"""
from __future__ import annotations

import hashlib
import random
from collections import defaultdict
from typing import Any, Iterable

from mido import MidiTrack

TICKS_PER_BEAT = 960
GP_START_TICK = 960


def _clamp(value: Any, lower: int | float, upper: int | float, default: int | float = 0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return max(lower, min(upper, number))


def _measure_length(header: Any) -> int:
    signature = getattr(header, "timeSignature", None)
    numerator = int(getattr(signature, "numerator", 4) or 4)
    denominator = int(getattr(getattr(signature, "denominator", None), "value", 4) or 4)
    return int(round(numerator * TICKS_PER_BEAT * 4.0 / denominator))


def measure_spans(song: Any) -> dict[int, tuple[int, int]]:
    """Return 1-based measure spans in the converter's normalized MIDI ticks."""
    headers = list(getattr(song, "measureHeaders", []) or [])
    spans: dict[int, tuple[int, int]] = {}
    cursor = 0
    for index, header in enumerate(headers):
        raw_start = getattr(header, "start", None)
        start = max(0, int(raw_start) - GP_START_TICK) if raw_start is not None else cursor
        if index + 1 < len(headers) and getattr(headers[index + 1], "start", None) is not None:
            end = max(start + 1, int(headers[index + 1].start) - GP_START_TICK)
        else:
            end = start + _measure_length(header)
        spans[index + 1] = (start, end)
        cursor = end
    return spans


def _absolute_messages(track: Iterable[Any]) -> list[dict[str, Any]]:
    tick = 0
    rows = []
    for index, message in enumerate(track):
        tick += int(getattr(message, "time", 0) or 0)
        rows.append({"tick": tick, "message": message, "index": index})
    return rows


def musical_note_signature(
    midi_track: Iterable[Any], service_notes: set[int] | None = None,
) -> tuple[tuple[int, str, int], ...]:
    """Return a velocity-independent identity/timing signature for musical notes."""
    service_notes = set(service_notes or set())
    signature = []
    for row in _absolute_messages(midi_track):
        message = row["message"]
        msg_type = getattr(message, "type", None)
        if msg_type not in {"note_on", "note_off"}:
            continue
        note = int(getattr(message, "note", -1))
        if note in service_notes:
            continue
        velocity = int(getattr(message, "velocity", 0) or 0)
        semantic_type = "off" if msg_type == "note_off" or velocity == 0 else "on"
        signature.append((int(row["tick"]), semantic_type, note))
    return tuple(signature)


def _measure_for_tick(tick: int, spans: dict[int, tuple[int, int]]) -> tuple[int, int, int]:
    for measure, (start, end) in spans.items():
        if start <= tick < end:
            return measure, start, end
    if not spans:
        return 1, 0, TICKS_PER_BEAT * 4
    measure = max(spans)
    start, end = spans[measure]
    return measure, start, end


def build_arrangement_context(
    song: Any,
    rendered_tracks: list[dict[str, Any]],
    *,
    service_notes: dict[str, set[int]] | None = None,
) -> dict[str, Any]:
    """Summarize already target-mapped tracks without mutating their MIDI."""
    spans = measure_spans(song)
    service_notes = service_notes or {}
    tracks = []
    for rendered in rendered_tracks:
        name = str(rendered["track_name"])
        track_type = str(rendered["track_type"])
        service = set(service_notes.get(track_type, set()))
        by_measure: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"velocities": [], "onsets": set()})
        for row in _absolute_messages(rendered["midi_track"]):
            message = row["message"]
            if getattr(message, "type", None) != "note_on" or int(getattr(message, "velocity", 0) or 0) <= 0:
                continue
            if int(getattr(message, "note", -1)) in service:
                continue
            measure, start, _end = _measure_for_tick(row["tick"], spans)
            beat = 1.0 + ((row["tick"] - start) / TICKS_PER_BEAT)
            by_measure[measure]["velocities"].append(int(message.velocity))
            by_measure[measure]["onsets"].add(round(beat, 3))
        measures = []
        for measure in sorted(by_measure):
            velocities = by_measure[measure]["velocities"]
            measures.append({
                "measure": measure,
                "notes": len(velocities),
                "mean_velocity": int(round(sum(velocities) / len(velocities))),
                "onset_beats": sorted(by_measure[measure]["onsets"]),
            })
        tracks.append({"name": name, "type": track_type, "measures": measures})
    return {
        "schema_version": 1,
        "tempo_bpm": round(float(getattr(song, "tempo", 120) or 120), 3),
        "measure_count": len(spans),
        "processing_order": "target_articulations_then_hermes_draft",
        "contract": {
            "notes_added_or_removed": False,
            "rhythm_rewrite": False,
            "post_mapping_timing_changes": False,
            "drum_hits_added": False,
            "rewrite_is_proposal_only": True,
        },
        "tracks": tracks,
    }


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def validate_plan(
    plan: dict[str, Any], *, track_names: list[str], measure_count: int,
    allow_solo_humanization: bool,
) -> dict[str, Any]:
    """Treat a Hermes draft as untrusted data and return a bounded apply schema."""
    if not isinstance(plan, dict):
        raise ValueError("expression plan must be a JSON object")
    allowed_tracks = set(track_names)
    measures = []
    seen_measures = set()
    raw_measures = plan.get("measure_energy", [])
    for item in raw_measures if isinstance(raw_measures, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            start_measure = int(item.get("measure", item.get("measure_start")))
            end_measure = int(item.get("measure", item.get("measure_end", start_measure)))
        except (TypeError, ValueError):
            continue
        start_measure = max(1, start_measure)
        end_measure = min(measure_count, max(start_measure, end_measure))
        if start_measure > measure_count:
            continue
        accents = []
        raw_accents = item.get("accents", [])
        if isinstance(raw_accents, list):
            for accent in raw_accents[:8]:
                try:
                    beat = float(accent)
                except (TypeError, ValueError):
                    continue
                if 1.0 <= beat <= 16.0:
                    accents.append(round(beat, 3))
        for measure in range(start_measure, end_measure + 1):
            if measure in seen_measures:
                continue
            measures.append({
                "measure": measure,
                "velocity_shift": int(round(_clamp(item.get("velocity_shift"), -12, 12))),
                "accents": sorted(set(accents)),
            })
            seen_measures.add(measure)

    tracks = []
    seen_tracks = set()
    raw_tracks = plan.get("tracks", [])
    for item in raw_tracks if isinstance(raw_tracks, list) else []:
        if not isinstance(item, dict):
            continue
        track = str(item.get("track", ""))
        if track not in allowed_tracks or track in seen_tracks:
            continue
        profile = {
            "track": track,
            "role": _clean_text(item.get("role", "support"), 32) or "support",
            "velocity_shift": int(round(_clamp(item.get("velocity_shift"), -16, 16))),
            "variance": int(round(_clamp(item.get("variance"), 0, 12))),
        }
        if "velocity_processing" in item:
            profile["velocity_processing"] = bool(item.get("velocity_processing"))
        if "minimum_velocity" in item:
            profile["minimum_velocity"] = int(round(_clamp(item.get("minimum_velocity"), 1, 127, 1)))
        tracks.append(profile)
        seen_tracks.add(track)

    raw_solo = plan.get("solo_humanization", {})
    solo_enabled = (
        bool(raw_solo.get("enabled")) if isinstance(raw_solo, dict) else False
    ) and bool(allow_solo_humanization)
    return {
        "schema_version": 1,
        "summary": _clean_text(plan.get("summary"), 240),
        "global_velocity_shift": int(round(_clamp(plan.get("global_velocity_shift"), -12, 12))),
        "measure_energy": sorted(measures, key=lambda row: row["measure"]),
        "tracks": tracks,
        "solo_humanization": {"enabled": solo_enabled},
    }


def _stable_rng(seed: int, track_name: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{track_name}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _nonzero_jitter(rng: random.Random, amount: int) -> int:
    if amount <= 0:
        return 0
    value = rng.randint(-amount, amount)
    if value == 0:
        value = 1 if rng.random() >= 0.5 else -1
    return value


def apply_expression_plan(
    midi_track: MidiTrack,
    track_name: str,
    track_type: str,
    plan: dict[str, Any],
    spans: dict[int, tuple[int, int]],
    *,
    seed: int,
    service_notes: set[int] | None = None,
) -> dict[str, Any]:
    """Apply velocity only to already mapped MIDI; timing and note identity stay fixed."""
    service_notes = set(service_notes or set())
    before_signature = musical_note_signature(midi_track, service_notes)
    profile = next((row for row in plan.get("tracks", []) if row.get("track") == track_name), {})
    measure_plan = {int(row["measure"]): row for row in plan.get("measure_energy", [])}
    global_shift = int(plan.get("global_velocity_shift", 0) or 0)
    track_shift = int(profile.get("velocity_shift", 0) or 0)
    variance = int(profile.get("variance", 0) or 0)
    velocity_processing = bool(profile.get("velocity_processing", True))
    minimum_velocity = (
        int(_clamp(profile.get("minimum_velocity"), 1, 127, 1))
        if track_type == "DRUMS" else 1
    )
    rng = _stable_rng(seed, track_name)

    tick = 0
    changed = 0
    service_count = 0
    reserved_count = 0
    before_velocities = []
    after_velocities = []
    for index, message in enumerate(midi_track):
        tick += int(getattr(message, "time", 0) or 0)
        if getattr(message, "type", None) != "note_on" or int(getattr(message, "velocity", 0) or 0) <= 0:
            continue
        note = int(getattr(message, "note", -1))
        velocity = int(message.velocity)
        if note in service_notes:
            service_count += 1
            continue
        measure, start, _end = _measure_for_tick(tick, spans)
        expression = measure_plan.get(measure, {})
        beat = 1.0 + ((tick - start) / TICKS_PER_BEAT)
        accent = 6 if any(abs(beat - float(value)) <= 0.10 for value in expression.get("accents", [])) else 0
        jitter = _nonzero_jitter(rng, variance)
        is_reserved = track_type == "GUITAR" and velocity >= 120
        before_velocities.append(velocity)
        if not velocity_processing:
            new_velocity = velocity
        elif is_reserved:
            new_velocity = velocity
            reserved_count += 1
        else:
            cap = 119 if track_type == "GUITAR" else 127
            new_velocity = int(_clamp(
                velocity + global_shift + track_shift
                + int(expression.get("velocity_shift", 0) or 0) + accent + jitter,
                minimum_velocity, cap, velocity,
            ))
            if new_velocity != velocity:
                changed += 1
            midi_track[index] = message.copy(velocity=new_velocity)
        after_velocities.append(new_velocity)

    if musical_note_signature(midi_track, service_notes) != before_signature:
        raise RuntimeError("expression plan changed note identity or timing")
    return {
        "changed_notes": changed,
        "velocity_processing": velocity_processing,
        "service_events_preserved": service_count,
        "reserved_notes_preserved": reserved_count,
        "mean_velocity_before": round(sum(before_velocities) / len(before_velocities), 2) if before_velocities else 0.0,
        "mean_velocity_after": round(sum(after_velocities) / len(after_velocities), 2) if after_velocities else 0.0,
    }
