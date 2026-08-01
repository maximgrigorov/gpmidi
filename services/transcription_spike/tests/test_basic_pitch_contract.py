from __future__ import annotations

import hashlib
import importlib.util
import sys
import wave
from pathlib import Path

import pytest

RUNTIME = Path(__file__).parents[1] / "runtimes" / "basic-pitch" / "run.py"


def load_runtime():
    spec = importlib.util.spec_from_file_location("basic_pitch_runtime", RUNTIME)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tiny_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22_050)
        wav.writeframes(b"\0\0" * 2_205)


def test_bass_frequency_bounds_are_explicit() -> None:
    runtime = load_runtime()

    assert runtime.midi_to_hz(runtime.MIN_BASS_MIDI) == pytest.approx(41.203444, rel=1e-6)
    assert runtime.midi_to_hz(runtime.MAX_BASS_MIDI) == pytest.approx(391.995436, rel=1e-6)


def test_transcribe_verifies_model_and_writes_exact_output(tmp_path: Path) -> None:
    runtime = load_runtime()
    audio = tmp_path / "bass.wav"
    tiny_wav(audio)
    model = tmp_path / "nmp.tflite"
    model.write_bytes(b"model")
    output = tmp_path / "result.mid"
    calls = []

    class FakeMidi:
        def write(self, path: str) -> None:
            Path(path).write_bytes(b"MThd-result")

    def fake_predict(path: Path, model_path: Path, **kwargs):
        calls.append((path, model_path, kwargs))
        return {}, FakeMidi(), [(0.0, 0.5, 40, 0.8, None)]

    summary = runtime.transcribe(
        audio,
        output,
        model_path=model,
        expected_model_sha256=hashlib.sha256(b"model").hexdigest(),
        predict_fn=fake_predict,
    )

    assert output.read_bytes() == b"MThd-result"
    assert summary["note_events"] == 1
    assert summary["minimum_midi"] == runtime.MIN_BASS_MIDI
    assert summary["maximum_midi"] == runtime.MAX_BASS_MIDI
    assert calls[0][0:2] == (audio, model)
    assert calls[0][2]["minimum_frequency"] == pytest.approx(runtime.midi_to_hz(28))
    assert calls[0][2]["maximum_frequency"] == pytest.approx(runtime.midi_to_hz(67))
    assert calls[0][2]["multiple_pitch_bends"] is False


def test_transcribe_rejects_model_digest_mismatch(tmp_path: Path) -> None:
    runtime = load_runtime()
    audio = tmp_path / "bass.wav"
    tiny_wav(audio)
    model = tmp_path / "nmp.tflite"
    model.write_bytes(b"unexpected")

    with pytest.raises(RuntimeError, match="model digest mismatch"):
        runtime.transcribe(
            audio,
            tmp_path / "result.mid",
            model_path=model,
            expected_model_sha256="0" * 64,
            predict_fn=lambda *args, **kwargs: None,
        )


def test_container_contract_is_pinned_and_non_root() -> None:
    runtime_dir = RUNTIME.parent
    dockerfile = (runtime_dir / "Dockerfile").read_text(encoding="utf-8")
    lock = (runtime_dir / "requirements.lock").read_text(encoding="utf-8")

    assert "python:3.10.14-slim-bookworm@sha256:45360d9e" in dockerfile
    assert "pip install --require-hashes" in dockerfile
    assert "MODEL_SHA256" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert '["python", "/app/run.py"]' in dockerfile
    assert "basic-pitch==0.4.0" in lock
    assert "numpy==1.26.4" in lock
    assert "tflite-runtime==2.14.0" in lock


def test_cli_help_does_not_require_basic_pitch_installed() -> None:
    import subprocess

    completed = subprocess.run(
        [sys.executable, str(RUNTIME), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--input" in completed.stdout
    assert "--output" in completed.stdout
