from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import wave
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).parents[1]
REPO_ROOT = Path(__file__).parents[3]
RUNTIME = SERVICE_ROOT / "runtimes" / "basic-pitch" / "run.py"
REAL_INPUT_FIXTURES = SERVICE_ROOT / "fixtures" / "real-input"
PIPELINE_RUNNER = REPO_ROOT / "infra" / "ailab" / "scripts" / "run-transcription-pipeline.sh"


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

    assert "python:3.10.19-slim-bookworm@sha256:23f63358" in dockerfile
    assert "libgnutls30=3.7.9-2+deb12u7" in dockerfile
    assert "libssl3=3.0.20-1~deb12u2" in dockerfile
    assert "openssl=3.0.20-1~deb12u2" in dockerfile
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


def test_cli_keeps_dependency_progress_out_of_json_stdout(monkeypatch, capsys, tmp_path: Path) -> None:
    runtime = load_runtime()
    input_path = tmp_path / "input.wav"
    output_path = tmp_path / "output.mid"
    input_path.touch()

    def noisy_transcribe(input_arg: Path, output_arg: Path) -> dict[str, object]:
        print("Predicting MIDI for input.wav...")
        assert (input_arg, output_arg) == (input_path, output_path)
        return {"adapter": "basic-pitch", "note_events": 78}

    monkeypatch.setattr(runtime, "transcribe", noisy_transcribe)
    monkeypatch.setattr(sys, "argv", [str(RUNTIME), "--input", str(input_path), "--output", str(output_path)])

    assert runtime.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"adapter": "basic-pitch", "note_events": 78}
    assert captured.out.count("\n") == 1
    assert "Predicting MIDI" in captured.err


def test_real_input_pipeline_uses_hash_pinned_source_fixtures() -> None:
    expected = {
        "bass-195-215.wav": "d3e5cec64a13fc8c35050f84b9e67f30124d8bb4010295ff7524fd9ccc0cbb1f",
        "reference-bass.mid": "82250c89783273ce847500a7f9a582e6df74e9e2ea8be3af296f480d2f1b8ed9",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((REAL_INPUT_FIXTURES / name).read_bytes()).hexdigest() == digest

    runner = PIPELINE_RUNNER.read_text(encoding="utf-8")
    assert "cp /workspace/source/services/transcription_spike/fixtures/real-input/bass-195-215.wav" in runner
    assert "cp /workspace/source/services/transcription_spike/fixtures/real-input/reference-bass.mid" in runner
    assert "curl -fsSLo" not in runner
    assert "name: NUMBA_CACHE_DIR" in runner
    assert "value: /workspace/evidence/phase3-real-input/.numba-cache" in runner
    assert "find . -type f ! -name SHA256SUMS -exec sha256sum {} + > SHA256SUMS" in runner

    tasks = (REPO_ROOT / "infra" / "ailab" / "tekton" / "tasks.yaml").read_text(encoding="utf-8")
    assert "find . -type f ! -name SHA256SUMS -exec sha256sum {} + > SHA256SUMS" in tasks
