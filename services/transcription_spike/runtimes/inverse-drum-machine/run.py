from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

UPSTREAM_REPOSITORY = "https://github.com/bernardo-torres/inverse-drum-machine"
UPSTREAM_COMMIT = "456656868538205ef756912c7cf5b0fd936de8af"
CHECKPOINT_PATH = (
    "pretrained/idm-44-train-kits/checkpoints/"
    "val-epoch=518-global_step=0.ckpt"
)
CHECKPOINT_SHA256 = "5856a9bee7c6d503842795756d238dc8470f6f3e010e9e4f33ede0362850cb4c"
TRAIN_CLASSES = (
    "CY_CR",
    "CY_RD",
    "HH_CHH",
    "HH_OHH",
    "KD",
    "SD",
    "TT_HFT",
    "TT_HMT",
    "TT_LMT",
)
GENERAL_MIDI_NOTES = {
    "CY_CR": 49,
    "CY_RD": 51,
    "HH_CHH": 42,
    "HH_OHH": 46,
    "KD": 36,
    "SD": 38,
    "TT_HFT": 48,
    "TT_HMT": 47,
    "TT_LMT": 45,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _numpy(value: Any) -> Any:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return value


def events_from_activations(
    onsets: Any,
    velocities: Any,
    classes: Sequence[str],
    *,
    frame_rate: float,
) -> list[dict[str, int | float | str]]:
    if frame_rate <= 0:
        raise ValueError("frame_rate must be positive")
    onset_values = _numpy(onsets)
    velocity_values = _numpy(velocities)
    events: list[dict[str, int | float | str]] = []
    for class_index, instrument in enumerate(classes):
        if instrument not in GENERAL_MIDI_NOTES:
            raise ValueError(f"unsupported drum class: {instrument}")
        for frame_index, active in enumerate(onset_values[0, class_index]):
            if float(active) <= 0.0:
                continue
            velocity = max(1, min(127, round(float(velocity_values[0, class_index, frame_index]) * 127)))
            events.append(
                {
                    "instrument": instrument,
                    "midi_note": GENERAL_MIDI_NOTES[instrument],
                    "time_seconds": frame_index / frame_rate,
                    "velocity": velocity,
                }
            )
    return sorted(events, key=lambda event: (float(event["time_seconds"]), int(event["midi_note"])))


def _infer(input_path: Path, checkpoint_path: Path) -> tuple[Any, Any, float]:
    import soundfile as sf
    import torch
    import torchaudio
    from idm.inference import load_model

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model, _name = load_model("idm-44-train-kits", device, log_dir=checkpoint_path.parents[2])
    audio, sample_rate = sf.read(input_path, always_2d=True)
    waveform = torch.from_numpy(audio.T).float().mean(dim=0, keepdim=True)
    model_rate = int(model.sampling_rate)
    if sample_rate != model_rate:
        waveform = torchaudio.functional.resample(waveform, sample_rate, model_rate)
    with torch.no_grad():
        encoded = model.encoder(waveform.to(device))
        activations = encoded["activations"]
        onset_probabilities = torch.sigmoid(activations["onset"])
        onsets = model.decoder.peak_picking_val(
            onset_probabilities,
            activation_rate=float(encoded["activation_rate"]),
        )
    return onsets, activations["velocity"], float(encoded["activation_rate"])


def transcribe(
    input_path: Path,
    output_path: Path,
    *,
    checkpoint_path: Path,
    expected_checkpoint_sha256: str = CHECKPOINT_SHA256,
    infer_fn: Callable[[Path, Path], tuple[Any, Any, float]] = _infer,
) -> dict[str, Any]:
    input_path = Path(input_path)
    output_path = Path(output_path)
    checkpoint_path = Path(checkpoint_path)
    if not input_path.is_file():
        raise FileNotFoundError("input audio is unavailable")
    if not checkpoint_path.is_file():
        raise FileNotFoundError("IDM checkpoint is unavailable")
    if output_path.suffix.lower() != ".json":
        raise ValueError("output must be a JSON path")
    if sha256_file(checkpoint_path) != expected_checkpoint_sha256:
        raise RuntimeError("checkpoint digest mismatch")

    onsets, velocities, frame_rate = infer_fn(input_path, checkpoint_path)
    events = events_from_activations(onsets, velocities, TRAIN_CLASSES, frame_rate=frame_rate)
    payload = {
        "adapter": "inverse-drum-machine",
        "checkpoint_sha256": expected_checkpoint_sha256,
        "events": events,
        "frame_rate": frame_rate,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_repository": UPSTREAM_REPOSITORY,
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
    parser = argparse.ArgumentParser(description="Pinned Inverse Drum Machine transcription spike")
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
