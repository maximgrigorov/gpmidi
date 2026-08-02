from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

SERVICE_ROOT = Path(__file__).parents[1]
RUNTIME = SERVICE_ROOT / "runtimes" / "inverse-drum-machine" / "run.py"


def load_runtime():
    spec = importlib.util.spec_from_file_location("idm_drum_runtime", RUNTIME)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_events_use_source_seconds_general_midi_and_velocity() -> None:
    runtime = load_runtime()
    onsets = np.zeros((1, 9, 8), dtype=np.float32)
    velocities = np.zeros_like(onsets)
    onsets[0, 4, 2] = 1.0  # kick
    velocities[0, 4, 2] = 0.5
    onsets[0, 5, 5] = 1.0  # snare
    velocities[0, 5, 5] = 1.0

    events = runtime.events_from_activations(
        onsets,
        velocities,
        runtime.TRAIN_CLASSES,
        frame_rate=100.0,
    )

    assert events == [
        {"instrument": "KD", "midi_note": 36, "time_seconds": 0.02, "velocity": 64},
        {"instrument": "SD", "midi_note": 38, "time_seconds": 0.05, "velocity": 127},
    ]


def test_transcribe_verifies_checkpoint_and_writes_machine_readable_events(tmp_path: Path) -> None:
    runtime = load_runtime()
    audio = tmp_path / "drums.wav"
    audio.write_bytes(b"RIFF-audio")
    checkpoint = tmp_path / "idm.ckpt"
    checkpoint.write_bytes(b"checkpoint")
    output = tmp_path / "events.json"

    onsets = np.zeros((1, 9, 4), dtype=np.float32)
    velocities = np.zeros_like(onsets)
    onsets[0, 0, 1] = 1.0
    velocities[0, 0, 1] = 0.25

    summary = runtime.transcribe(
        audio,
        output,
        checkpoint_path=checkpoint,
        expected_checkpoint_sha256=hashlib.sha256(b"checkpoint").hexdigest(),
        infer_fn=lambda _audio, _checkpoint: (onsets, velocities, 50.0),
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["events"] == [
        {"instrument": "CY_CR", "midi_note": 49, "time_seconds": 0.02, "velocity": 32}
    ]
    assert summary["adapter"] == "inverse-drum-machine"
    assert summary["event_count"] == 1
    assert summary["upstream_commit"] == runtime.UPSTREAM_COMMIT


def test_transcribe_rejects_checkpoint_digest_mismatch(tmp_path: Path) -> None:
    runtime = load_runtime()
    audio = tmp_path / "drums.wav"
    audio.write_bytes(b"audio")
    checkpoint = tmp_path / "idm.ckpt"
    checkpoint.write_bytes(b"unexpected")

    with pytest.raises(RuntimeError, match="checkpoint digest mismatch"):
        runtime.transcribe(
            audio,
            tmp_path / "events.json",
            checkpoint_path=checkpoint,
            expected_checkpoint_sha256="0" * 64,
            infer_fn=lambda *_args: None,
        )


def test_upstream_source_and_checkpoint_are_pinned() -> None:
    runtime = load_runtime()

    assert runtime.UPSTREAM_COMMIT == "456656868538205ef756912c7cf5b0fd936de8af"
    assert len(runtime.CHECKPOINT_SHA256) == 64
    assert runtime.CHECKPOINT_PATH.endswith("val-epoch=518-global_step=0.ckpt")
    assert runtime.TRAIN_CLASSES == (
        "CY_CR",
        "CY_RD",
        "HH_CHH",
        "HH_OHH",
        "KD",
        "SD",
        "TT_HFT",
        "TT_HMT",
        "TT_LMT",
    )


def test_container_contract_pins_upstream_and_runs_non_root() -> None:
    dockerfile = (RUNTIME.parent / "Dockerfile").read_text(encoding="utf-8")

    assert "python:3.10.19-slim-bookworm@sha256:23f63358" in dockerfile
    assert "d209125b1053dfa1dea19deba0e3bc908761326c5a7253f77b525e34e7cf7827" in dockerfile
    assert "5856a9bee7c6d503842795756d238dc8470f6f3e010e9e4f33ede0362850cb4c" in dockerfile
    assert "https://download.pytorch.org/whl/cpu" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert '["python", "/app/run.py"]' in dockerfile
