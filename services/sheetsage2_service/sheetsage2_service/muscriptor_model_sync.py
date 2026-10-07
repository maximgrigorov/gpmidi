from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from . import MUSCRIPTOR_LARGE_REVISION

MUSCRIPTOR_REPO = "MuScriptor/muscriptor-large"
MUSCRIPTOR_LARGE_BYTES = 5_465_642_136


def is_verified(directory: Path) -> bool:
    directory = Path(directory)
    try:
        weights = directory / "model.safetensors"
        return (
            weights.is_file()
            and weights.stat().st_size == MUSCRIPTOR_LARGE_BYTES
            and (directory / "PINNED_REVISION").read_text(encoding="utf-8").strip()
            == MUSCRIPTOR_LARGE_REVISION
        )
    except OSError:
        return False


def sync_model(root: Path) -> None:
    from huggingface_hub import snapshot_download

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    final = root / "large"
    if is_verified(final):
        return
    staging = root / f".staging-{uuid.uuid4().hex}"
    backup = root / ".large.old"
    try:
        snapshot_download(
            repo_id=MUSCRIPTOR_REPO,
            revision=MUSCRIPTOR_LARGE_REVISION,
            local_dir=staging,
            local_dir_use_symlinks=False,
            allow_patterns=["model.safetensors", "config.json", "README.md"],
            token=os.environ.get("HF_TOKEN"),
        )
        weights = staging / "model.safetensors"
        if not weights.is_file() or weights.stat().st_size != MUSCRIPTOR_LARGE_BYTES:
            raise RuntimeError("MuScriptor large checkpoint size/integrity check failed")
        shutil.rmtree(staging / ".cache", ignore_errors=True)
        (staging / "PINNED_REVISION").write_text(
            MUSCRIPTOR_LARGE_REVISION + "\n", encoding="utf-8"
        )
        shutil.rmtree(backup, ignore_errors=True)
        if final.exists():
            final.rename(backup)
        staging.rename(final)
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    sync_model(Path(os.environ.get("MUSCRIPTOR_MODEL_ROOT", "/muscriptor-models")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
