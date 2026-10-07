from __future__ import annotations

import sys
import types
from pathlib import Path

from sheetsage2_service.muscriptor_runtime import transcribe as transcribe_muscriptor


def test_runtime_uses_cuda_bfloat16_pinned_local_weights_and_writes_midi(
    tmp_path: Path, monkeypatch,
):
    weights = tmp_path / "large" / "model.safetensors"
    weights.parent.mkdir()
    weights.write_bytes(b"weights")
    source = tmp_path / "input.wav"
    source.write_bytes(b"audio")
    output = tmp_path / "output"
    calls: dict[str, object] = {}

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def empty_cache() -> None:
            calls["cache_cleared"] = True

    bfloat16 = object()
    fake_torch = types.SimpleNamespace(cuda=FakeCuda(), bfloat16=bfloat16)

    class FakeLoadedModel:
        def transcribe_and_postprocess(self, path, **kwargs):
            calls["transcribe"] = (path, kwargs)
            return b"MThd-midi", None

    class FakeTranscriptionModel:
        @classmethod
        def load_model(cls, **kwargs):
            calls["load"] = kwargs
            return FakeLoadedModel()

    fake_muscriptor = types.SimpleNamespace(TranscriptionModel=FakeTranscriptionModel)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "muscriptor", fake_muscriptor)
    monkeypatch.setenv("MUSCRIPTOR_MODEL_PATH", str(weights))

    metrics = transcribe_muscriptor(source, output)

    assert calls["load"] == {
        "weights_path": weights,
        "device": "cuda:0",
        "dtype": bfloat16,
    }
    assert calls["transcribe"] == (
        source,
        {"detect_tempo": "best-effort", "quantize": False},
    )
    assert (output / "transcription.mid").read_bytes() == b"MThd-midi"
    assert metrics["model"] == "MuScriptor Large"
    assert metrics["device"] == "cuda"
    assert metrics["dtype"] == "bfloat16"
    assert metrics["elapsed_seconds"] >= 0
