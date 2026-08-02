from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import mido

from .evaluation import evaluate_events
from .models import EventKind, TranscriptionEvent

CLASS_BY_NOTE = {
    **{note: "kick" for note in (35, 36)},
    **{note: "snare" for note in (37, 38, 39, 40)},
    **{note: "tom" for note in (41, 43, 45, 47, 48, 50)},
    **{note: "hi_hat" for note in (42, 44, 46)},
    **{note: "cymbal" for note in (49, 51, 52, 53, 55, 57, 59)},
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _event(time_seconds: float, event_class: str, velocity: int) -> TranscriptionEvent:
    return TranscriptionEvent(
        onset_seconds=time_seconds,
        kind=EventKind.PERCUSSIVE,
        instrument="drums",
        event_class=event_class,
        velocity=velocity,
    )


def _reference_events(path: Path) -> tuple[list[TranscriptionEvent], Counter[int], Counter[int]]:
    absolute_seconds = 0.0
    events: list[TranscriptionEvent] = []
    excluded: Counter[int] = Counter()
    note_counts: Counter[int] = Counter()
    for message in mido.MidiFile(path):
        absolute_seconds += message.time
        if message.type != "note_on" or message.velocity <= 0:
            continue
        note_counts[message.note] += 1
        event_class = CLASS_BY_NOTE.get(message.note)
        if event_class is None:
            excluded[message.note] += 1
        else:
            events.append(_event(absolute_seconds, event_class, message.velocity))
    return events, excluded, note_counts


def _prediction_events(payload: dict[str, Any]) -> tuple[list[TranscriptionEvent], Counter[int]]:
    events: list[TranscriptionEvent] = []
    excluded: Counter[int] = Counter()
    for item in payload["events"]:
        midi_note = int(item["midi_note"])
        event_class = CLASS_BY_NOTE.get(midi_note)
        if event_class is None:
            excluded[midi_note] += 1
        else:
            events.append(_event(float(item["time_seconds"]), event_class, int(item["velocity"])))
    return events, excluded


def evaluate_drum_artifacts(
    *,
    reference_midi: Path,
    prediction_json: Path,
    tolerances_seconds: tuple[float, ...] = (0.025, 0.05, 0.1),
    offset_range_seconds: tuple[float, float] = (-0.2, 0.2),
    offset_step_seconds: float = 0.005,
) -> dict[str, Any]:
    reference_midi = Path(reference_midi)
    prediction_json = Path(prediction_json)
    reference, excluded_reference, note_counts = _reference_events(reference_midi)
    payload = json.loads(prediction_json.read_text(encoding="utf-8"))
    prediction, excluded_prediction = _prediction_events(payload)
    report: dict[str, Any] = {
        "schema_version": "phase3-drum-evaluation-v1",
        "adapter": payload["adapter"],
        "taxonomy": {str(note): label for note, label in sorted(CLASS_BY_NOTE.items())},
        "reference": {
            "sha256": _sha256(reference_midi),
            "events_total": sum(note_counts.values()),
            "events_evaluated": len(reference),
            "note_counts": {str(note): count for note, count in sorted(note_counts.items())},
            "excluded_note_counts": {
                str(note): count for note, count in sorted(excluded_reference.items())
            },
        },
        "prediction": {
            "sha256": _sha256(prediction_json),
            "events_total": len(payload["events"]),
            "events_evaluated": len(prediction),
            "excluded_note_counts": {
                str(note): count for note, count in sorted(excluded_prediction.items())
            },
            "class_counts": dict(sorted(Counter(event.event_class for event in prediction).items())),
        },
        "metrics": {},
    }
    for tolerance in tolerances_seconds:
        name = f"onset_class_{round(tolerance * 1000)}ms"
        report["metrics"][name] = evaluate_events(
            reference, prediction, tolerance_seconds=tolerance
        ).model_dump(mode="json")

    start, end = offset_range_seconds
    count = round((end - start) / offset_step_seconds)
    candidates = []
    for index in range(count + 1):
        offset = round(start + index * offset_step_seconds, 12)
        shifted = [
            event.model_copy(update={"onset_seconds": event.onset_seconds + offset})
            for event in prediction
            if event.onset_seconds + offset >= 0
        ]
        metric = evaluate_events(reference, shifted, tolerance_seconds=0.05)
        p50 = metric.onset_error_p50_seconds
        candidates.append((metric.f1, -(p50 if p50 is not None else float("inf")), -abs(offset), offset, metric))
    _, _, _, best_offset, best_metric = max(candidates)
    report["offset_sensitivity"] = {
        "range_seconds": [start, end],
        "step_seconds": offset_step_seconds,
        "best_offset_seconds": best_offset,
        "best_50ms": best_metric.model_dump(mode="json"),
        "note": "Raw source-seconds metrics remain authoritative.",
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate drum-event JSON against reference MIDI")
    parser.add_argument("--reference-midi", required=True, type=Path)
    parser.add_argument("--prediction-json", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    args = parser.parse_args()
    report = evaluate_drum_artifacts(
        reference_midi=args.reference_midi,
        prediction_json=args.prediction_json,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
