from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

UPSTREAM_REPOSITORY = "https://github.com/xavriley/ADTOF-pytorch"
UPSTREAM_COMMIT = "85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9"
UPSTREAM_SOURCE_SHA256 = "28602a3bd89836240d519396b566966c52b6439e2f3cda61d8a674433b350b56"
CHECKPOINT_SHA256 = "1bc986e596ec47ba0b44916f87cd4a39f0b2bec23596df3fb5d0e87749217320"
LABELS = (35, 38, 47, 42, 49)
THRESHOLDS = (0.22, 0.24, 0.32, 0.22, 0.30)
INSTRUMENTS = {
    35: "kick",
    38: "snare",
    47: "tom",
    42: "hi_hat",
    49: "cymbal",
}
FIXED_VELOCITY = 100
FPS = 100


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def events_from_peaks(peaks: Mapping[int, Sequence[float]]) -> list[dict[str, int | float | str]]:
    events: list[dict[str, int | float | str]] = []
    for midi_note, times in peaks.items():
        if midi_note not in INSTRUMENTS:
            raise ValueError(f"unsupported drum note: {midi_note}")
        for time_seconds in times:
            value = float(time_seconds)
            if value < 0:
                raise ValueError("event time must be non-negative")
            events.append(
                {
                    "instrument": INSTRUMENTS[midi_note],
                    "midi_note": midi_note,
                    "time_seconds": value,
                    "velocity": FIXED_VELOCITY,
                }
            )
    return sorted(events, key=lambda event: (float(event["time_seconds"]), int(event["midi_note"])))


def _infer(input_path: Path, checkpoint_path: Path) -> Mapping[int, Sequence[float]]:
    import torch
    from adtof_pytorch import (
        PeakPicker,
        calculate_n_bins,
        create_frame_rnn_model,
        load_audio_for_model,
        load_pytorch_weights,
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = create_frame_rnn_model(calculate_n_bins())
    model = load_pytorch_weights(model, str(checkpoint_path), strict=False)
    model.eval().to(device)
    audio = load_audio_for_model(str(input_path)).to(device)
    with torch.no_grad():
        activations = model(audio).cpu().numpy()
    return PeakPicker(thresholds=THRESHOLDS, fps=FPS).pick(activations, labels=LABELS)[0]


def transcribe(
    input_path: Path,
    output_path: Path,
    *,
    checkpoint_path: Path,
    expected_checkpoint_sha256: str = CHECKPOINT_SHA256,
    infer_fn: Callable[[Path, Path], Mapping[int, Sequence[float]]] = _infer,
) -> dict[str, Any]:
    input_path = Path(input_path)
    output_path = Path(output_path)
    checkpoint_path = Path(checkpoint_path)
    if not input_path.is_file():
        raise FileNotFoundError("input audio is unavailable")
    if not checkpoint_path.is_file():
        raise FileNotFoundError("ADTOF-pytorch checkpoint is unavailable")
    if output_path.suffix.lower() != ".json":
        raise ValueError("output must be a JSON path")
    if sha256_file(checkpoint_path) != expected_checkpoint_sha256:
        raise RuntimeError("checkpoint digest mismatch")

    events = events_from_peaks(infer_fn(input_path, checkpoint_path))
    payload = {
        "adapter": "adtof-pytorch",
        "checkpoint_sha256": expected_checkpoint_sha256,
        "events": events,
        "fixed_velocity": FIXED_VELOCITY,
        "fps": FPS,
        "labels": list(LABELS),
        "thresholds": list(THRESHOLDS),
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_repository": UPSTREAM_REPOSITORY,
        "upstream_source_sha256": UPSTREAM_SOURCE_SHA256,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return {
        "adapter": payload["adapter"],
        "checkpoint_sha256": expected_checkpoint_sha256,
        "event_count": len(events),
        "upstream_commit": UPSTREAM_COMMIT,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pinned ADTOF-pytorch research transcription spike")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = transcribe(args.input, args.output, checkpoint_path=args.checkpoint)
    print(json.dumps(summary, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
