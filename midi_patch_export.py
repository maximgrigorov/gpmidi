#!/usr/bin/env python3
"""Export only tracks changed by an agent or a manual MIDI edit.

The baseline and candidate must retain the same track order and names.  The
output contains the candidate conductor (when track 0 is meta-only) followed by
only semantically changed tracks.  This keeps Logic imports small without
pretending that note edits are always forbidden: the manifest reports whether
musical note events changed, while expression-only callers may opt into a
fail-closed ``--require-note-identity`` check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from mido import MidiFile, MidiTrack


def _absolute_events(track: Iterable[Any]) -> list[tuple[int, Any]]:
    tick = 0
    events = []
    for message in track:
        tick += int(message.time)
        events.append((tick, message))
    return events


def _track_name(track: Iterable[Any]) -> str:
    for message in track:
        if message.type == "track_name":
            return str(message.name)
    return ""


def _semantic_signature(track: Iterable[Any]) -> tuple[tuple[int, tuple[tuple[str, Any], ...]], ...]:
    return tuple(
        (
            tick,
            tuple(sorted(
                (key, value)
                for key, value in message.dict().items()
                if key != "time"
            )),
        )
        for tick, message in _absolute_events(track)
    )


def _note_signature(track: Iterable[Any]) -> tuple[tuple[int, str, int, int], ...]:
    signature = []
    for tick, message in _absolute_events(track):
        if message.type not in {"note_on", "note_off"}:
            continue
        velocity = int(getattr(message, "velocity", 0) or 0)
        semantic = "off" if message.type == "note_off" or velocity == 0 else "on"
        signature.append((
            tick,
            semantic,
            int(message.note),
            int(getattr(message, "channel", 0)),
        ))
    return tuple(signature)


def _event_counts(track: Iterable[Any]) -> dict[str, int]:
    counts = {
        "events": 0,
        "note_attacks": 0,
        "pitchwheel": 0,
        "cc1": 0,
        "control_change": 0,
    }
    for message in track:
        counts["events"] += 1
        if message.type == "note_on" and int(message.velocity) > 0:
            counts["note_attacks"] += 1
        elif message.type == "pitchwheel":
            counts["pitchwheel"] += 1
        elif message.type == "control_change":
            counts["control_change"] += 1
            if int(message.control) == 1:
                counts["cc1"] += 1
    return counts


def _is_meta_only(track: Iterable[Any]) -> bool:
    return not any(message.type in {"note_on", "note_off"} for message in track)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_patch(
    baseline_path: Path,
    candidate_path: Path,
    output_path: Path,
    *,
    require_note_identity: bool = False,
) -> dict[str, Any]:
    """Write conductor + changed tracks and return a factual manifest."""
    resolved = {
        baseline_path.resolve(), candidate_path.resolve(), output_path.resolve(),
    }
    if len(resolved) != 3:
        raise ValueError("baseline, candidate, and output paths must be distinct")

    baseline = MidiFile(baseline_path)
    candidate = MidiFile(candidate_path)
    if baseline.ticks_per_beat != candidate.ticks_per_beat:
        raise ValueError("baseline and candidate ticks_per_beat differ")
    if len(baseline.tracks) != len(candidate.tracks):
        raise ValueError("baseline and candidate track counts differ")

    changed_indices = []
    track_reports = []
    note_identity_failures = []
    for index, (before, after) in enumerate(zip(baseline.tracks, candidate.tracks)):
        before_name = _track_name(before)
        after_name = _track_name(after)
        if before_name != after_name:
            raise ValueError(
                f"track {index} name changed: {before_name!r} -> {after_name!r}"
            )
        if _semantic_signature(before) == _semantic_signature(after):
            continue
        changed_indices.append(index)
        note_identity_changed = _note_signature(before) != _note_signature(after)
        if note_identity_changed:
            note_identity_failures.append(index)
        track_reports.append({
            "source_index": index,
            "track": after_name or f"Track {index}",
            "note_identity_changed": note_identity_changed,
            "before": _event_counts(before),
            "after": _event_counts(after),
        })

    if not changed_indices:
        raise ValueError("candidate contains no semantic track changes")
    if require_note_identity and note_identity_failures:
        joined = ", ".join(str(index) for index in note_identity_failures)
        raise RuntimeError(f"musical note identity changed in tracks: {joined}")

    conductor_preserved = (
        candidate.type == 1
        and len(candidate.tracks) > 1
        and _is_meta_only(candidate.tracks[0])
    )
    output_tracks: list[tuple[int, MidiTrack]] = []
    if conductor_preserved:
        output_tracks.append((0, candidate.tracks[0]))
    for index in changed_indices:
        if conductor_preserved and index == 0:
            continue
        output_tracks.append((index, candidate.tracks[index]))

    output = MidiFile(
        type=1 if len(output_tracks) > 1 else 0,
        ticks_per_beat=candidate.ticks_per_beat,
    )
    output.tracks.extend(MidiTrack(message.copy() for message in track) for _index, track in output_tracks)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output.save(output_path)

    return {
        "schema_version": 1,
        "mode": "changed_tracks_patch",
        "baseline": {"path": str(baseline_path), "sha256": _sha256(baseline_path)},
        "candidate": {"path": str(candidate_path), "sha256": _sha256(candidate_path)},
        "output": {"path": str(output_path), "sha256": _sha256(output_path)},
        "ticks_per_beat": candidate.ticks_per_beat,
        "conductor_track_preserved": conductor_preserved,
        "changed_source_track_indices": changed_indices,
        "output_source_track_indices": [index for index, _track in output_tracks],
        "require_note_identity": require_note_identity,
        "tracks": track_reports,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--require-note-identity", action="store_true")
    parser.add_argument("--manifest", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = export_patch(
        args.baseline.resolve(),
        args.candidate.resolve(),
        args.output.resolve(),
        require_note_identity=args.require_note_identity,
    )
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
