from __future__ import annotations

import os
import time
from pathlib import Path


def transcribe(input_path: Path, output_path: Path) -> dict:
    import torch
    from muscriptor import TranscriptionModel

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is unavailable")
    weights = Path(
        os.environ.get(
            "MUSCRIPTOR_MODEL_PATH", "/muscriptor-models/large/model.safetensors"
        )
    )
    if not weights.is_file():
        raise RuntimeError("pinned MuScriptor model cache is incomplete")
    started = time.monotonic()
    model = TranscriptionModel.load_model(
        weights_path=weights,
        device="cuda:0",
        dtype=torch.bfloat16,
    )
    output_path.mkdir(parents=True, exist_ok=True)
    instruments = [
        value for value in os.environ.get("MUSCRIPTOR_INSTRUMENTS", "").split(",") if value
    ]
    try:
        midi_bytes = model.transcribe_to_midi(
            input_path,
            detect_tempo=False,
            instruments=instruments or None,
        )
        (output_path / "transcription.mid").write_bytes(midi_bytes)
    finally:
        del model
        torch.cuda.empty_cache()
    return {
        "elapsed_seconds": time.monotonic() - started,
        "model": "MuScriptor Large",
        "device": "cuda",
        "dtype": "bfloat16",
    }
