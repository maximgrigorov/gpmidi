"""Review-first transfer of source-time events onto a Guitar Pro tick grid.

This module never mutates a Guitar Pro revision.  It emits an immutable candidate
patch and compiles a separate Type-1 MIDI overlay only for explicitly approved
measures.  The legacy GP-to-MIDI path therefore remains the default and rollback.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any

import mido
from pydantic import BaseModel, ConfigDict, Field

from .models import TranscriptionEvent


class CandidateStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class RestorationCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str
    source_measure_index: int
    gp_measure_index: int
    gp_measure_number: int
    source_onset_seconds: float
    target_tick: int
    target_duration_ticks: int
    instrument: str
    midi_pitch: int
    velocity: int
    articulation: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    mapping_confidence: float = Field(ge=0.0, le=1.0)
    event_confidence: float = Field(ge=0.0, le=1.0)
    status: CandidateStatus = CandidateStatus.PROPOSED


class RestorationPatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "gpmidi-restoration-patch-v1"
    analysis_id: str
    gp_revision_sha256: str
    source_identity: str
    default_action: str = "review_required"
    velocity_strategy: str = "preserve"
    minimum_mapping_confidence: float
    source_event_count: int
    rejected_event_count: int
    candidates: list[RestorationCandidate]


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    gp_measure_number: int
    approved: bool


def _candidate_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def _event_mapping(event: TranscriptionEvent, mappings: list[dict[str, Any]]) -> dict[str, Any] | None:
    for mapping in mappings:
        if mapping.get("mapping_type") not in {"one_to_one", "repeat"}:
            continue
        start = float(mapping.get("source_seconds_start", 0.0))
        end = float(mapping.get("source_seconds_end", 0.0))
        if start <= event.onset_seconds < end:
            return mapping
    return None


def build_candidate_patch(
    analysis: dict[str, Any],
    events: list[TranscriptionEvent],
    *,
    source_identity: str,
    minimum_mapping_confidence: float = 0.5,
    velocity_strategy: str = "preserve",
) -> RestorationPatch:
    """Map source-time events into destination ticks without applying them."""
    if velocity_strategy not in {"preserve", "confidence"}:
        raise ValueError("velocity_strategy must be preserve or confidence")
    candidates: list[RestorationCandidate] = []
    rejected = 0
    mappings = list(analysis.get("mappings", []))
    for event in sorted(events, key=lambda item: (item.onset_seconds, item.match_key)):
        mapping = _event_mapping(event, mappings)
        if mapping is None or float(mapping.get("confidence", 0.0)) < minimum_mapping_confidence:
            rejected += 1
            continue
        if event.midi_pitch is None:
            rejected += 1
            continue
        source_start = float(mapping["source_seconds_start"])
        source_end = float(mapping["source_seconds_end"])
        gp_start = int(mapping["gp_tick_start"])
        gp_end = int(mapping["gp_tick_end"])
        if source_end <= source_start or gp_end <= gp_start:
            rejected += 1
            continue
        scale = (gp_end - gp_start) / (source_end - source_start)
        target_tick = gp_start + round((event.onset_seconds - source_start) * scale)
        if event.offset_seconds is not None:
            duration = max(1, round((event.offset_seconds - event.onset_seconds) * scale))
        else:
            duration = max(1, round((gp_end - gp_start) / 8))
        event_confidence = event.confidence if event.confidence is not None else 1.0
        velocity = event.velocity if event.velocity is not None else 100
        if velocity_strategy == "confidence":
            velocity = min(127, max(1, round(30 + 97 * event_confidence)))
        mapping_confidence = float(mapping["confidence"])
        identity = {
            "source_identity": source_identity,
            "source_onset_seconds": round(event.onset_seconds, 9),
            "midi_pitch": event.midi_pitch,
            "gp_measure_number": int(mapping["gp_measure_number"]),
            "target_tick": target_tick,
        }
        candidates.append(
            RestorationCandidate(
                candidate_id=_candidate_id(identity),
                source_measure_index=int(mapping["source_measure_index"]),
                gp_measure_index=int(mapping["gp_measure_index"]),
                gp_measure_number=int(mapping["gp_measure_number"]),
                source_onset_seconds=event.onset_seconds,
                target_tick=target_tick,
                target_duration_ticks=duration,
                instrument=event.instrument,
                midi_pitch=event.midi_pitch,
                velocity=velocity,
                articulation=event.event_class,
                confidence=event_confidence * mapping_confidence,
                mapping_confidence=mapping_confidence,
                event_confidence=event_confidence,
            )
        )
    candidates.sort(
        key=lambda item: (
            item.gp_measure_number,
            item.target_tick,
            item.midi_pitch,
            item.candidate_id,
        )
    )
    return RestorationPatch(
        analysis_id=str(analysis["analysis_id"]),
        gp_revision_sha256=str(analysis["gp_revision_sha256"]),
        source_identity=source_identity,
        velocity_strategy=velocity_strategy,
        minimum_mapping_confidence=minimum_mapping_confidence,
        source_event_count=len(events),
        rejected_event_count=rejected,
        candidates=candidates,
    )


def _midi_track(name: str, candidates: list[RestorationCandidate]) -> mido.MidiTrack:
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name=name, time=0))
    absolute: list[tuple[int, int, mido.Message]] = []
    channel = 9 if name == "drums" else 0
    for candidate in candidates:
        absolute.append(
            (
                candidate.target_tick,
                1,
                mido.Message(
                    "note_on",
                    note=candidate.midi_pitch,
                    velocity=candidate.velocity,
                    channel=channel,
                    time=0,
                ),
            )
        )
        absolute.append(
            (
                candidate.target_tick + candidate.target_duration_ticks,
                0,
                mido.Message(
                    "note_off",
                    note=candidate.midi_pitch,
                    velocity=0,
                    channel=channel,
                    time=0,
                ),
            )
        )
    previous = 0
    for tick, _, message in sorted(
        absolute, key=lambda item: (item[0], item[1], int(getattr(item[2], "note", 0)))
    ):
        message.time = tick - previous
        track.append(message)
        previous = tick
    track.append(mido.MetaMessage("end_of_track", time=0))
    return track


def _meta_track(
    *,
    ticks_per_beat: int,
    tempo_source: Path | None,
) -> tuple[mido.MidiTrack, int]:
    """Build a target-grid tempo track, optionally copied from a reference MIDI."""
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name="gpmidi restoration overlay", time=0))
    if tempo_source is None:
        track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0))
        track.append(mido.MetaMessage("end_of_track", time=0))
        return track, 1

    source = mido.MidiFile(tempo_source)
    absolute: list[tuple[int, int, Any]] = []
    copied_types = {"set_tempo", "time_signature", "key_signature"}
    for source_track_index, source_track in enumerate(source.tracks):
        source_tick = 0
        for message in source_track:
            source_tick += message.time
            if message.type not in copied_types:
                continue
            target_tick = round(source_tick * ticks_per_beat / source.ticks_per_beat)
            absolute.append((target_tick, source_track_index, message.copy(time=0)))
    if not any(message.type == "set_tempo" for _, _, message in absolute):
        absolute.append(
            (0, -1, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0))
        )
    previous = 0
    for tick, _, message in sorted(
        absolute, key=lambda item: (item[0], item[1], item[2].type)
    ):
        message.time = tick - previous
        track.append(message)
        previous = tick
    track.append(mido.MetaMessage("end_of_track", time=0))
    return track, sum(message.type == "set_tempo" for _, _, message in absolute)


def compile_approved_overlay(
    patch: RestorationPatch,
    decisions: list[ApprovalDecision],
    output_path: Path,
    *,
    ticks_per_beat: int = 960,
    tempo_source: Path | None = None,
) -> dict[str, int | str]:
    """Compile approved measures to a separate Type-1 MIDI overlay."""
    approved_measures = {
        decision.gp_measure_number for decision in decisions if decision.approved
    }
    selected = [
        candidate
        for candidate in patch.candidates
        if candidate.gp_measure_number in approved_measures
    ]
    midi = mido.MidiFile(type=1, ticks_per_beat=ticks_per_beat)
    meta, tempo_event_count = _meta_track(
        ticks_per_beat=ticks_per_beat,
        tempo_source=tempo_source,
    )
    midi.tracks.append(meta)
    by_instrument: dict[str, list[RestorationCandidate]] = {}
    for candidate in selected:
        by_instrument.setdefault(candidate.instrument, []).append(candidate)
    if by_instrument:
        for instrument in sorted(by_instrument):
            midi.tracks.append(_midi_track(instrument, by_instrument[instrument]))
    else:
        midi.tracks.append(_midi_track("empty review", []))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(output_path)
    return {
        "output": str(output_path),
        "approved_measure_count": len(approved_measures),
        "written_event_count": len(selected),
        "ticks_per_beat": ticks_per_beat,
        "tempo_event_count": tempo_event_count,
    }
