from __future__ import annotations

import os
import time
from pathlib import Path


def transcribe(input_path: Path, output_path: Path) -> dict:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import torch
    from transformers import AutoModel

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is unavailable")
    model_root = Path(os.environ.get("SHEETSAGE2_MODEL_ROOT", "/models"))
    sheet_model = model_root / "sheetsage2"
    base_model = model_root / "mert"
    for path in (sheet_model, base_model):
        if not (path / "model.safetensors").is_file():
            raise RuntimeError(f"pinned model cache is incomplete: {path.name}")
    started = time.monotonic()
    model = AutoModel.from_pretrained(
        str(sheet_model),
        base_model_path=str(base_model),
        trust_remote_code=True,
        local_files_only=True,
        torch_dtype=torch.bfloat16,
        device_map={"": "cuda:0"},
        attn_implementation="sdpa",
    ).eval()
    output_path.mkdir(parents=True, exist_ok=True)
    try:
        model.transcribe(str(input_path), output_dir=str(output_path))
    finally:
        del model
        torch.cuda.empty_cache()
    return {"elapsed_seconds": time.monotonic() - started}
