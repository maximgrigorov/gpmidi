from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path

from huggingface_hub import snapshot_download

from . import BASE_MODEL_REVISION, MODEL_REVISION

SHEETSAGE_REPO = "m-a-p/SheetSage2"
BASE_REPO = "m-a-p/MERT-v2-FullSong"
SHEETSAGE_WEIGHTS_SHA256 = "b235f68091a5f5b644000f2b5acb57d1e70432aca2b34ab1b9cf27236e1f4274"
BASE_WEIGHTS_SHA256 = "e6dd2ab187d6dd62b6521cd7d8f932e237acf0c5757745a7232082e28391350d"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(repo: str, revision: str, destination: Path, expected_sha256: str) -> None:
    snapshot_download(
        repo_id=repo,
        revision=revision,
        local_dir=destination,
        local_dir_use_symlinks=False,
    )
    weights = destination / "model.safetensors"
    if not weights.is_file() or _sha256(weights) != expected_sha256:
        raise RuntimeError(f"integrity check failed for {repo}@{revision}")
    shutil.rmtree(destination / ".cache", ignore_errors=True)
    (destination / "PINNED_REVISION").write_text(revision + "\n", encoding="utf-8")
    (destination / "VERIFIED_SHA256").write_text(expected_sha256 + "\n", encoding="utf-8")


def _is_verified(directory: Path, revision: str, expected_sha256: str) -> bool:
    try:
        return (
            (directory / "model.safetensors").is_file()
            and (directory / "PINNED_REVISION").read_text(encoding="utf-8").strip() == revision
            and (directory / "VERIFIED_SHA256").read_text(encoding="utf-8").strip() == expected_sha256
        )
    except OSError:
        return False


def sync_models(root: Path) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if _is_verified(root / "sheetsage2", MODEL_REVISION, SHEETSAGE_WEIGHTS_SHA256) and _is_verified(
        root / "mert", BASE_MODEL_REVISION, BASE_WEIGHTS_SHA256
    ):
        return
    staging = root / f".staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        _download(SHEETSAGE_REPO, MODEL_REVISION, staging / "sheetsage2", SHEETSAGE_WEIGHTS_SHA256)
        _download(BASE_REPO, BASE_MODEL_REVISION, staging / "mert", BASE_WEIGHTS_SHA256)
        for name in ("sheetsage2", "mert"):
            final = root / name
            backup = root / f".{name}.old"
            shutil.rmtree(backup, ignore_errors=True)
            if final.exists():
                final.rename(backup)
            (staging / name).rename(final)
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    sync_models(Path(os.environ.get("SHEETSAGE2_MODEL_ROOT", "/models")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
