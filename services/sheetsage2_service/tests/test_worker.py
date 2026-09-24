from __future__ import annotations

import json
import wave
from pathlib import Path

from sheetsage2_service.failures import GPU_BUSY_MESSAGE
from sheetsage2_service.worker import run_worker


def _wav(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * 2400)


def test_worker_publishes_success_only_after_verified_archive(tmp_path: Path, midi_factory):
    job_dir = tmp_path / "jobs" / "job1"
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True)
    input_path = input_dir / "song.wav"
    _wav(input_path)
    (job_dir / "state.json").write_text(
        json.dumps(
            {
                "status": "queued",
                "original_filename": "song.wav",
                "owner_token_sha256": "abc",
            }
        ),
        encoding="utf-8",
    )

    def transcriber(_input: Path, output: Path) -> dict:
        output.mkdir(parents=True)
        (output / "transcription.mid").write_bytes(midi_factory())
        (output / "events.json").write_text("[]", encoding="utf-8")
        return {"elapsed_seconds": 0.25}

    assert run_worker(job_dir=job_dir, transcriber=transcriber, model_revision="rev") == 0
    state = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "succeeded"
    assert state["original_filename"] == "song.wav"
    assert state["owner_token_sha256"] == "abc"
    assert (job_dir / "result" / state["archive_name"]).is_file()


def test_worker_converts_cuda_oom_to_user_facing_state(tmp_path: Path):
    job_dir = tmp_path / "jobs" / "job2"
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True)
    _wav(input_dir / "song.wav")

    def transcriber(_input: Path, _output: Path) -> dict:
        raise RuntimeError("CUDA out of memory")

    assert run_worker(job_dir=job_dir, transcriber=transcriber, model_revision="rev") == 20
    state = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    assert state == {
        "status": "failed",
        "error_code": "gpu_vram_exhausted",
        "message": GPU_BUSY_MESSAGE,
    }
    assert not (job_dir / "result").exists()
