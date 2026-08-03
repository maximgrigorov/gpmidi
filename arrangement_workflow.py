#!/usr/bin/env python3
"""Reproducible Hermes draft/apply workflow for target-library MIDI.

Prepare renders the accepted Guitar Pro score through the normal Shreddage
converter and writes mapped MIDI plus a compact context. Apply requires an
explicit --approved flag and a reviewed JSON plan. It rebuilds from Guitar Pro,
optionally humanizes only a Guitar Solo/Lead before keyswitch emission, applies
velocity expression, checks preservation invariants, and writes a separate
package without overwriting the prepared baseline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from mido import MidiFile, MidiTrack

from articulation_config import config_for_track_type
from arrangement_processing import (
    apply_expression_plan,
    build_arrangement_context,
    measure_spans,
    musical_note_signature,
    validate_plan,
)
from gp_import import parse_song
from gp_to_shreddage import (
    TICKS_PER_BEAT,
    build_combined_midi,
    build_drum_midi,
    build_instrument_midi,
    build_other_midi,
    resolve_track_type,
    safe_filename,
)
from verify_midi import smoke_check


def require_apply_approval(approved: bool) -> None:
    if not approved:
        raise ValueError("apply is blocked without explicit --approved")


def is_solo_track(name: str, track_type: str) -> bool:
    lowered = str(name or "").casefold()
    return track_type == "GUITAR" and any(word in lowered for word in ("solo", "lead"))


def service_notes_for_type(track_type: str) -> set[int]:
    if track_type not in {"GUITAR", "BASS"}:
        return set()
    cfg = config_for_track_type(track_type) or {}
    notes: set[int] = set()
    for spec in (cfg.get("keyswitches") or {}).values():
        if isinstance(spec, dict) and spec.get("note") is not None:
            notes.add(int(spec["note"]))
    for value in (cfg.get("fx_keyswitches") or {}).values():
        if isinstance(value, int):
            notes.add(value)
        elif isinstance(value, dict) and value.get("note") is not None:
            notes.add(int(value["note"]))
    return notes


def musical_identity(
    midi_track: Iterable[Any], service_notes: set[int] | None = None,
) -> Counter[tuple[str, int]]:
    service_notes = set(service_notes or set())
    identity: Counter[tuple[str, int]] = Counter()
    for message in midi_track:
        msg_type = getattr(message, "type", None)
        if msg_type not in {"note_on", "note_off"}:
            continue
        note = int(getattr(message, "note", -1))
        if note in service_notes:
            continue
        velocity = int(getattr(message, "velocity", 0) or 0)
        semantic = "off" if msg_type == "note_off" or velocity == 0 else "on"
        identity[(semantic, note)] += 1
    return identity


def assert_preservation(
    baseline: Iterable[Any], enriched: Iterable[Any], *, track_type: str,
    allow_timing: bool, service_notes: set[int] | None = None,
) -> None:
    """Fail if notes were added/removed/repitched or forbidden timing moved."""
    baseline_identity = musical_identity(baseline, service_notes)
    enriched_identity = musical_identity(enriched, service_notes)
    if baseline_identity != enriched_identity:
        raise RuntimeError(f"{track_type} note identity changed (added/removed/repitched events)")
    if not allow_timing:
        before = musical_note_signature(baseline, service_notes)
        after = musical_note_signature(enriched, service_notes)
        if before != after:
            raise RuntimeError(f"{track_type} timing changed outside approved Solo humanization")


def _copy_track(track: Iterable[Any]) -> MidiTrack:
    return MidiTrack(message.copy() for message in track)


def _render_track(song: Any, source_track: Any, track_type: str, *, solo_humanize: bool, seed: int):
    if track_type == "DRUMS":
        return build_drum_midi(song, source_track, humanize=False, ghost_notes=None)
    if track_type == "OTHER":
        return build_other_midi(song, source_track)
    return build_instrument_midi(
        song, source_track, track_type,
        humanize=bool(solo_humanize), humanize_seed=seed,
        auto_sustain_vibrato=False,
        fret_noise_on_hand_shift=False,
        performance_seed=seed,
        expand_gp_hidden_32nds=False,
        preserve_gp_played_offsets=False,
    )


def _render_baseline(song: Any) -> list[dict[str, Any]]:
    rendered = []
    used: dict[str, int] = {}
    for index, source_track in enumerate(song.tracks, start=1):
        track_type = resolve_track_type(source_track)
        midi_track, stats = _render_track(
            song, source_track, track_type, solo_humanize=False, seed=7)
        base = safe_filename(source_track.name) or f"Track_{index}"
        used[base] = used.get(base, 0) + 1
        basename = base if used[base] == 1 else f"{base}_{used[base]}"
        rendered.append({
            "track_name": source_track.name or f"Track {index}",
            "track_type": track_type,
            "basename": basename,
            "source_track": source_track,
            "midi_track": midi_track,
            "stats": stats,
        })
    return rendered


def _save_midi(track: MidiTrack, path: Path) -> None:
    midi = MidiFile(type=0, ticks_per_beat=TICKS_PER_BEAT)
    midi.tracks.append(track)
    midi.save(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reset_output(output_dir: Path) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)


def _smoke_errors(path: Path, track_type: str) -> list[dict[str, str]]:
    findings = smoke_check(str(path), track_type)
    return [
        {"severity": severity, "code": code, "message": message}
        for severity, code, message in findings
        if severity == "ERROR"
    ]


def prepare(source: Path, output_dir: Path) -> dict[str, Any]:
    _reset_output(output_dir)
    song = parse_song(source)
    rendered = _render_baseline(song)
    service_by_type = {
        track_type: service_notes_for_type(track_type)
        for track_type in {row["track_type"] for row in rendered}
    }
    context = build_arrangement_context(song, rendered, service_notes=service_by_type)
    stem = safe_filename(source.stem) or "song"
    artifacts = []
    all_tracks = []
    for row in rendered:
        path = output_dir / f"{row['basename']}.mid"
        _save_midi(row["midi_track"], path)
        errors = _smoke_errors(path, row["track_type"])
        if errors:
            raise RuntimeError(f"MIDI smoke failed for {path.name}: {errors}")
        artifacts.append({
            "name": path.name,
            "track": row["track_name"],
            "type": row["track_type"],
            "sha256": _sha256(path),
            "notes": int(row["stats"].get("notes", 0)),
        })
        all_tracks.append(row["midi_track"])
    combined = output_dir / f"{stem}_ALL.mid"
    build_combined_midi(all_tracks).save(combined)
    artifacts.append({"name": combined.name, "type": "TYPE_1_ALL", "sha256": _sha256(combined)})

    context_path = output_dir / f"{stem}_arrangement-context.json"
    context_payload = {
        "schema_version": 1,
        "stage": "awaiting_hermes_draft",
        "approved": False,
        "source_midi_state": "target_library_mapped",
        "source": {"name": source.name, "sha256": _sha256(source)},
        "context": context,
    }
    context_path.write_text(json.dumps(context_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append({"name": context_path.name, "type": "HERMES_CONTEXT", "sha256": _sha256(context_path)})
    manifest = {
        "schema_version": 1,
        "mode": "prepare",
        "enrichment_applied": False,
        "source_midi_state": "target_library_mapped",
        "source": context_payload["source"],
        "artifacts": artifacts,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def apply(source: Path, plan_path: Path, output_dir: Path, *, approved: bool, seed: int) -> dict[str, Any]:
    require_apply_approval(approved)
    raw_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if isinstance(raw_plan.get("plan"), dict):
        raw_plan = raw_plan["plan"]
    song = parse_song(source)
    baseline = _render_baseline(song)
    track_names = [row["track_name"] for row in baseline]
    plan = validate_plan(
        raw_plan,
        track_names=track_names,
        measure_count=len(song.measureHeaders),
        allow_solo_humanization=True,
    )
    _reset_output(output_dir)
    spans = measure_spans(song)
    artifacts = []
    enriched_tracks = []
    track_stats = []
    solo_enabled = bool(plan["solo_humanization"]["enabled"])

    for row in baseline:
        allow_timing = solo_enabled and is_solo_track(row["track_name"], row["track_type"])
        if allow_timing:
            enriched, render_stats = _render_track(
                song, row["source_track"], row["track_type"],
                solo_humanize=True, seed=seed,
            )
        else:
            enriched = _copy_track(row["midi_track"])
            render_stats = row["stats"]
        service_notes = service_notes_for_type(row["track_type"])
        expression_stats = apply_expression_plan(
            enriched, row["track_name"], row["track_type"], plan, spans,
            seed=seed, service_notes=service_notes,
        )
        assert_preservation(
            row["midi_track"], enriched,
            track_type=row["track_type"], allow_timing=allow_timing,
            service_notes=service_notes,
        )
        path = output_dir / f"{row['basename']}_expression.mid"
        _save_midi(enriched, path)
        errors = _smoke_errors(path, row["track_type"])
        if errors:
            raise RuntimeError(f"MIDI smoke failed for {path.name}: {errors}")
        artifacts.append({
            "name": path.name,
            "track": row["track_name"],
            "type": row["track_type"],
            "sha256": _sha256(path),
        })
        track_stats.append({
            "track": row["track_name"],
            "type": row["track_type"],
            "solo_pre_mapping_humanization": allow_timing,
            "source_notes": int(row["stats"].get("notes", 0)),
            "rendered_notes": int(render_stats.get("notes", 0)),
            **expression_stats,
            "identity_preserved": True,
            "timing_preserved": not allow_timing,
        })
        enriched_tracks.append(enriched)

    stem = safe_filename(source.stem) or "song"
    combined = output_dir / f"{stem}_expression_ALL.mid"
    build_combined_midi(enriched_tracks).save(combined)
    artifacts.append({"name": combined.name, "type": "TYPE_1_ALL", "sha256": _sha256(combined)})
    approved_plan = output_dir / f"{stem}_approved-expression-plan.json"
    approved_plan.write_text(json.dumps({
        "schema_version": 1,
        "approved": True,
        "seed": seed,
        "plan": plan,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append({"name": approved_plan.name, "type": "APPROVED_PLAN", "sha256": _sha256(approved_plan)})
    manifest = {
        "schema_version": 1,
        "mode": "approved_apply",
        "approved": True,
        "enrichment_applied": True,
        "source_midi_state": "target_library_mapped_before_expression",
        "source": {"name": source.name, "sha256": _sha256(source)},
        "plan": {"name": plan_path.name, "sha256": _sha256(plan_path)},
        "seed": seed,
        "safety": {
            "notes_added_or_removed": False,
            "drum_timing_changed": False,
            "post_mapping_timing_changed": False,
            "solo_timing_stage": "pre_mapping" if solo_enabled else "disabled",
            "hydra_reserved_velocity_zone_preserved": True,
        },
        "track_stats": track_stats,
        "artifacts": artifacts,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare", help="render mapped baseline + Hermes context")
    prepare_parser.add_argument("source", type=Path)
    prepare_parser.add_argument("output", type=Path)
    apply_parser = sub.add_parser("apply", help="apply a reviewed plan to a fresh mapped render")
    apply_parser.add_argument("source", type=Path)
    apply_parser.add_argument("plan", type=Path)
    apply_parser.add_argument("output", type=Path)
    apply_parser.add_argument("--approved", action="store_true")
    apply_parser.add_argument("--seed", type=int, default=7)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        result = prepare(args.source.resolve(), args.output.resolve())
    else:
        result = apply(
            args.source.resolve(), args.plan.resolve(), args.output.resolve(),
            approved=args.approved, seed=args.seed,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
