from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .models import EventKind, TranscriptionEvent
from .restoration import (
    ApprovalDecision,
    build_candidate_patch,
    compile_approved_overlay,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_events(path: Path) -> list[TranscriptionEvent]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: list[TranscriptionEvent] = []
    for item in payload["events"]:
        result.append(
            TranscriptionEvent(
                onset_seconds=float(item["time_seconds"]),
                kind=EventKind.PERCUSSIVE,
                instrument="drums",
                midi_pitch=int(item["midi_note"]),
                event_class=str(item["instrument"]),
                velocity=int(item.get("velocity", 100)),
                confidence=(
                    float(item["confidence"])
                    if item.get("confidence") is not None
                    else None
                ),
            )
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and review a Phase 4 MIDI restoration patch")
    parser.add_argument("--analysis-json", required=True, type=Path)
    parser.add_argument("--events-json", required=True, type=Path)
    parser.add_argument("--patch-output", required=True, type=Path)
    parser.add_argument("--decisions-json", type=Path)
    parser.add_argument("--midi-output", type=Path)
    parser.add_argument(
        "--tempo-midi",
        type=Path,
        help="Copy tempo/time-signature metadata into the standalone audition overlay",
    )
    parser.add_argument("--minimum-mapping-confidence", type=float, default=0.5)
    parser.add_argument(
        "--velocity-strategy", choices=("preserve", "confidence"), default="preserve"
    )
    parser.add_argument("--ticks-per-beat", type=int, default=960)
    args = parser.parse_args()

    analysis = json.loads(args.analysis_json.read_text(encoding="utf-8"))
    events = _load_events(args.events_json)
    patch = build_candidate_patch(
        analysis,
        events,
        source_identity=_sha256(args.events_json),
        minimum_mapping_confidence=args.minimum_mapping_confidence,
        velocity_strategy=args.velocity_strategy,
    )
    args.patch_output.parent.mkdir(parents=True, exist_ok=True)
    args.patch_output.write_text(
        patch.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    summary: dict[str, object] = {
        "candidate_count": len(patch.candidates),
        "rejected_event_count": patch.rejected_event_count,
        "patch_output": str(args.patch_output),
        "review_required": True,
    }
    if args.decisions_json is not None or args.midi_output is not None:
        if args.decisions_json is None or args.midi_output is None:
            parser.error("--decisions-json and --midi-output must be supplied together")
        raw_decisions = json.loads(args.decisions_json.read_text(encoding="utf-8"))
        decisions = [ApprovalDecision(**item) for item in raw_decisions["decisions"]]
        summary["overlay"] = compile_approved_overlay(
            patch,
            decisions,
            args.midi_output,
            ticks_per_beat=args.ticks_per_beat,
            tempo_source=args.tempo_midi,
        )
    print(json.dumps(summary, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
