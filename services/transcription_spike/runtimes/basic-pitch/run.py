from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

BASIC_PITCH_VERSION = "0.4.0"
MODEL_SHA256 = "3db297d54af8e01c6e5618245c956b1d71b6a2b978cb2dedb527173186552676"
MIN_BASS_MIDI = 28
MAX_BASS_MIDI = 67


def midi_to_hz(midi_pitch: int) -> float:
    return 440.0 * (2.0 ** ((midi_pitch - 69) / 12.0))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_runtime() -> tuple[Path, Callable[..., Any]]:
    from basic_pitch import FilenameSuffix, build_icassp_2022_model_path
    from basic_pitch.inference import predict

    model_path = Path(build_icassp_2022_model_path(FilenameSuffix["tflite"]))
    return model_path, predict


def transcribe(
    input_path: Path,
    output_path: Path,
    *,
    model_path: Path | None = None,
    expected_model_sha256: str = MODEL_SHA256,
    predict_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.is_file():
        raise FileNotFoundError("input audio is unavailable")
    if output_path.suffix.lower() not in {".mid", ".midi"}:
        raise ValueError("output must be a MIDI path")
    if model_path is None or predict_fn is None:
        runtime_model, runtime_predict = _load_runtime()
        model_path = runtime_model if model_path is None else model_path
        predict_fn = runtime_predict if predict_fn is None else predict_fn
    model_path = Path(model_path)
    if sha256_file(model_path) != expected_model_sha256:
        raise RuntimeError("model digest mismatch")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _model_output, midi_data, note_events = predict_fn(
        input_path,
        model_path,
        onset_threshold=0.5,
        frame_threshold=0.3,
        minimum_note_length=127.70,
        minimum_frequency=midi_to_hz(MIN_BASS_MIDI),
        maximum_frequency=midi_to_hz(MAX_BASS_MIDI),
        multiple_pitch_bends=False,
        melodia_trick=True,
        midi_tempo=120,
    )
    midi_data.write(str(output_path))
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("Basic Pitch produced no MIDI output")
    return {
        "adapter": "basic-pitch",
        "version": BASIC_PITCH_VERSION,
        "model_sha256": expected_model_sha256,
        "minimum_midi": MIN_BASS_MIDI,
        "maximum_midi": MAX_BASS_MIDI,
        "note_events": len(note_events),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded Basic Pitch bass transcription runtime")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    # Basic Pitch and its dependencies emit progress messages to stdout.
    # Keep stdout machine-readable for the pipeline's runtime-summary.json.
    with contextlib.redirect_stdout(sys.stderr):
        summary = transcribe(args.input, args.output)
    print(json.dumps(summary, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
