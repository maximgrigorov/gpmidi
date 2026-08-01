from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .adapters import run_external_adapter
from .basic_pitch_adapter import basic_pitch_docker_spec
from .evaluation import evaluate_events
from .midi_io import read_pitched_midi_events
from .models import TranscriptionEvent


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _onset_only(events: list[TranscriptionEvent], instrument: str) -> list[TranscriptionEvent]:
    return [event.model_copy(update={"instrument": f"{instrument}-onset", "midi_pitch": 0}) for event in events]


def _onset_pairs(
    reference: list[TranscriptionEvent],
    prediction: list[TranscriptionEvent],
    tolerance_seconds: float,
) -> dict[str, Any]:
    references = sorted(reference, key=lambda event: event.onset_seconds)
    predictions = sorted(prediction, key=lambda event: event.onset_seconds)
    ref_index = 0
    pred_index = 0
    pairs: list[dict[str, int | float]] = []
    while ref_index < len(references) and pred_index < len(predictions):
        ref = references[ref_index]
        pred = predictions[pred_index]
        difference = pred.onset_seconds - ref.onset_seconds
        if abs(difference) <= tolerance_seconds + 1e-12:
            assert ref.midi_pitch is not None and pred.midi_pitch is not None
            pairs.append(
                {
                    "reference_pitch": ref.midi_pitch,
                    "prediction_pitch": pred.midi_pitch,
                    "interval": pred.midi_pitch - ref.midi_pitch,
                    "onset_error_seconds": difference,
                }
            )
            ref_index += 1
            pred_index += 1
        elif pred.onset_seconds < ref.onset_seconds:
            pred_index += 1
        else:
            ref_index += 1
    histogram = Counter(int(pair["interval"]) for pair in pairs)
    return {
        "count": len(pairs),
        "interval_histogram": {str(interval): count for interval, count in sorted(histogram.items())},
        "pairs": pairs,
    }


def evaluate_pitched_midi_artifacts(
    *,
    reference_midi: Path,
    prediction_midi: Path,
    instrument: str,
    prediction_offset_seconds: float,
    window_start_seconds: float,
    window_end_seconds: float,
    tolerances_seconds: tuple[float, ...] = (0.025, 0.05, 0.1),
) -> dict[str, Any]:
    """Reproduce exact-pitch and onset-only metrics on one source-seconds window."""
    reference_midi = Path(reference_midi)
    prediction_midi = Path(prediction_midi)
    reference = read_pitched_midi_events(
        reference_midi,
        instrument=instrument,
        window_start_seconds=window_start_seconds,
        window_end_seconds=window_end_seconds,
    )
    prediction = read_pitched_midi_events(
        prediction_midi,
        instrument=instrument,
        timeline_offset_seconds=prediction_offset_seconds,
        window_start_seconds=window_start_seconds,
        window_end_seconds=window_end_seconds,
    )
    onset_reference = _onset_only(reference, instrument)
    onset_prediction = _onset_only(prediction, instrument)
    metrics: dict[str, Any] = {}
    report: dict[str, Any] = {
        "schema_version": "phase3-real-input-evaluation-v1",
        "source_timeline": {
            "start_seconds": window_start_seconds,
            "end_seconds": window_end_seconds,
            "prediction_offset_seconds": prediction_offset_seconds,
            "gp_grid_used": False,
        },
        "reference": {
            "events": len(reference),
            "midi_sha256": _sha256_file(reference_midi),
        },
        "prediction": {
            "events": len(prediction),
            "midi_sha256": _sha256_file(prediction_midi),
        },
        "metrics": metrics,
    }
    for tolerance in tolerances_seconds:
        milliseconds = round(tolerance * 1000)
        metrics[f"exact_pitch_{milliseconds}ms"] = evaluate_events(
            reference, prediction, tolerance_seconds=tolerance
        ).model_dump(mode="json")
        metrics[f"onset_only_{milliseconds}ms"] = evaluate_events(
            onset_reference, onset_prediction, tolerance_seconds=tolerance
        ).model_dump(mode="json")
        report[f"onset_pairs_{milliseconds}ms"] = _onset_pairs(reference, prediction, tolerance)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reproduce Phase 3 real-input transcription metrics")
    parser.add_argument("--reference-midi", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prediction-midi", type=Path)
    source.add_argument("--audio", type=Path)
    parser.add_argument("--image", help="repository-qualified image digest for --audio")
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--instrument", default="bass")
    parser.add_argument("--prediction-offset", type=float, required=True)
    parser.add_argument("--window-start", type=float, required=True)
    parser.add_argument("--window-end", type=float, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    prediction_midi = args.prediction_midi
    adapter_manifest: Path | None = None
    if args.audio is not None:
        if args.image is None or args.artifact_dir is None:
            raise SystemExit("--audio requires --image and --artifact-dir")
        adapter_result = run_external_adapter(
            basic_pitch_docker_spec(args.image),
            audio_path=args.audio,
            artifact_dir=args.artifact_dir,
        )
        prediction_midi = adapter_result.artifact_path
        adapter_manifest = adapter_result.manifest_path
    assert prediction_midi is not None
    report = evaluate_pitched_midi_artifacts(
        reference_midi=args.reference_midi,
        prediction_midi=prediction_midi,
        instrument=args.instrument,
        prediction_offset_seconds=args.prediction_offset,
        window_start_seconds=args.window_start,
        window_end_seconds=args.window_end,
    )
    if adapter_manifest is not None:
        report["adapter_manifest"] = {
            "filename": adapter_manifest.name,
            "sha256": _sha256_file(adapter_manifest),
        }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
