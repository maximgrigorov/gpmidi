from __future__ import annotations

from pathlib import Path

from sheetsage2_service import MUSCRIPTOR_LARGE_REVISION
from sheetsage2_service.muscriptor_model_sync import (
    MUSCRIPTOR_LARGE_BYTES,
    is_verified,
)


def test_muscriptor_cache_requires_exact_revision_and_weight_size(tmp_path: Path):
    cache = tmp_path / "large"
    cache.mkdir()
    weights = cache / "model.safetensors"
    weights.write_bytes(b"wrong")
    (cache / "PINNED_REVISION").write_text(MUSCRIPTOR_LARGE_REVISION + "\n")
    assert not is_verified(cache)

    weights.unlink()
    with weights.open("wb") as handle:
        handle.truncate(MUSCRIPTOR_LARGE_BYTES)
    assert is_verified(cache)

    (cache / "PINNED_REVISION").write_text("wrong\n")
    assert not is_verified(cache)


def test_muscriptor_cache_size_constant_matches_published_large_checkpoint():
    assert MUSCRIPTOR_LARGE_BYTES == 5_465_642_136
