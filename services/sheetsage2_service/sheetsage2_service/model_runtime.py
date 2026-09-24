from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

_DYNAMIC_MODULE_NAME = "transformers_modules"


def stage_local_remote_code(snapshot: Path, *, modules_root: Path) -> Path:
    """Copy all top-level code of a local model snapshot into the HF modules cache.

    For a local directory, transformers 4.45 copies only the direct relative
    imports of the model file, then resolves relative imports transitively, so
    a module imported only by another module is missing. Staging every file
    first leaves transformers' own copy a no-op (identical files).
    """
    snapshot = Path(snapshot)
    modules_root = Path(modules_root)
    target = modules_root / _DYNAMIC_MODULE_NAME / snapshot.name
    target.mkdir(parents=True, exist_ok=True)
    for package in (modules_root, modules_root / _DYNAMIC_MODULE_NAME, target):
        (package / "__init__.py").touch(exist_ok=True)
    for source in sorted(snapshot.glob("*.py")):
        if source.name != "__init__.py":
            shutil.copyfile(source, target / source.name)
    return target


def transcribe(input_path: Path, output_path: Path) -> dict:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import torch
    from transformers import AutoModel
    from transformers.dynamic_module_utils import HF_MODULES_CACHE, TRANSFORMERS_DYNAMIC_MODULE_NAME

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is unavailable")
    if TRANSFORMERS_DYNAMIC_MODULE_NAME != _DYNAMIC_MODULE_NAME:
        raise RuntimeError("unexpected transformers dynamic module layout")
    model_root = Path(os.environ.get("SHEETSAGE2_MODEL_ROOT", "/models"))
    sheet_model = model_root / "sheetsage2"
    base_model = model_root / "mert"
    for path in (sheet_model, base_model):
        if not (path / "model.safetensors").is_file():
            raise RuntimeError(f"pinned model cache is incomplete: {path.name}")
        stage_local_remote_code(path, modules_root=Path(HF_MODULES_CACHE))
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
