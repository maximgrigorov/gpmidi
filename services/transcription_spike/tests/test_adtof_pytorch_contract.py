from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

SERVICE_ROOT = Path(__file__).parents[1]
RUNTIME = SERVICE_ROOT / "runtimes" / "adtof-pytorch" / "run.py"


def load_runtime():
    spec = importlib.util.spec_from_file_location("adtof_pytorch_runtime", RUNTIME)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_events_use_source_seconds_general_midi_and_fixed_velocity() -> None:
    runtime = load_runtime()

    events = runtime.events_from_peaks({35: [0.2], 38: [0.05], 49: [1.0]})

    assert events == [
        {"instrument": "snare", "midi_note": 38, "time_seconds": 0.05, "velocity": 100},
        {"instrument": "kick", "midi_note": 35, "time_seconds": 0.2, "velocity": 100},
        {"instrument": "cymbal", "midi_note": 49, "time_seconds": 1.0, "velocity": 100},
    ]


def test_transcribe_verifies_checkpoint_and_writes_machine_readable_events(tmp_path: Path) -> None:
    runtime = load_runtime()
    audio = tmp_path / "drums.wav"
    audio.write_bytes(b"RIFF-audio")
    checkpoint = tmp_path / "adtof.pth"
    checkpoint.write_bytes(b"checkpoint")
    output = tmp_path / "events.json"

    summary = runtime.transcribe(
        audio,
        output,
        checkpoint_path=checkpoint,
        expected_checkpoint_sha256=hashlib.sha256(b"checkpoint").hexdigest(),
        infer_fn=lambda _audio, _checkpoint: {35: [0.125], 42: [0.25]},
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["events"] == [
        {"instrument": "kick", "midi_note": 35, "time_seconds": 0.125, "velocity": 100},
        {"instrument": "hi_hat", "midi_note": 42, "time_seconds": 0.25, "velocity": 100},
    ]
    assert payload["thresholds"] == [0.22, 0.24, 0.32, 0.22, 0.30]
    assert summary["adapter"] == "adtof-pytorch"
    assert summary["event_count"] == 2
    assert summary["upstream_commit"] == runtime.UPSTREAM_COMMIT


def test_transcribe_rejects_checkpoint_digest_mismatch(tmp_path: Path) -> None:
    runtime = load_runtime()
    audio = tmp_path / "drums.wav"
    audio.write_bytes(b"audio")
    checkpoint = tmp_path / "adtof.pth"
    checkpoint.write_bytes(b"unexpected")

    with pytest.raises(RuntimeError, match="checkpoint digest mismatch"):
        runtime.transcribe(
            audio,
            tmp_path / "events.json",
            checkpoint_path=checkpoint,
            expected_checkpoint_sha256="0" * 64,
            infer_fn=lambda *_args: {},
        )


def test_upstream_source_checkpoint_and_taxonomy_are_pinned() -> None:
    runtime = load_runtime()

    assert runtime.UPSTREAM_COMMIT == "85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9"
    assert runtime.UPSTREAM_SOURCE_SHA256 == "28602a3bd89836240d519396b566966c52b6439e2f3cda61d8a674433b350b56"
    assert runtime.CHECKPOINT_SHA256 == "1bc986e596ec47ba0b44916f87cd4a39f0b2bec23596df3fb5d0e87749217320"
    assert runtime.LABELS == (35, 38, 47, 42, 49)
    assert runtime.THRESHOLDS == (0.22, 0.24, 0.32, 0.22, 0.30)


def test_container_contract_pins_source_and_runs_non_root() -> None:
    dockerfile = (RUNTIME.parent / "Dockerfile").read_text(encoding="utf-8")

    assert "python:3.11.13-slim-bookworm@sha256:" in dockerfile
    assert "28602a3bd89836240d519396b566966c52b6439e2f3cda61d8a674433b350b56" in dockerfile
    assert "1bc986e596ec47ba0b44916f87cd4a39f0b2bec23596df3fb5d0e87749217320" in dockerfile
    assert "https://download.pytorch.org/whl/cpu" in dockerfile
    assert "torch==2.7.1+cpu" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert '["python", "/app/run.py"]' in dockerfile


def test_transcribe_exports_activations_confidence_custom_thresholds_and_fixed_audio_correction(
    tmp_path: Path,
) -> None:
    runtime = load_runtime()
    audio = tmp_path / "drums.wav"
    audio.write_bytes(b"RIFF-audio")
    checkpoint = tmp_path / "adtof.pth"
    checkpoint.write_bytes(b"checkpoint")
    output = tmp_path / "events.json"
    activations_output = tmp_path / "activations.npz"
    activations = np.zeros((1, 40, 5), dtype=np.float32)
    activations[0, 20, 0] = 0.9

    runtime.transcribe(
        audio,
        output,
        checkpoint_path=checkpoint,
        expected_checkpoint_sha256=hashlib.sha256(b"checkpoint").hexdigest(),
        infer_fn=lambda _audio, _checkpoint: activations,
        thresholds=(0.5, 0.5, 0.5, 0.5, 0.5),
        timestamp_correction_seconds=-0.14,
        activations_output=activations_output,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["timestamp_correction_seconds"] == -0.14
    assert payload["thresholds"] == [0.5] * 5
    assert payload["events"] == [
        {
            "confidence": pytest.approx(0.9),
            "instrument": "kick",
            "midi_note": 35,
            "time_seconds": pytest.approx(0.06),
            "velocity": 100,
        }
    ]
    stored = np.load(activations_output)
    assert np.array_equal(stored["activations"], activations)
    assert stored["fps"].item() == 100
    assert stored["labels"].tolist() == [35, 38, 47, 42, 49]


def test_timestamp_correction_cannot_move_events_before_zero() -> None:
    runtime = load_runtime()
    activations = np.zeros((1, 20, 5), dtype=np.float32)
    activations[0, 5, 0] = 0.9

    events = runtime.events_from_activations(
        activations,
        thresholds=(0.5,) * 5,
        timestamp_correction_seconds=-0.14,
    )

    assert events == []
