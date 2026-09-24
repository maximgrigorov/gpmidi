from __future__ import annotations

import hashlib
import json
import wave
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from sheetsage2_service.api import create_app
from sheetsage2_service.failures import GPU_BUSY_MESSAGE
from sheetsage2_service.launcher import WorkerObservation

OWNER = "a" * 64
OTHER_OWNER = "b" * 64
OWNER_HEADERS = {"X-Owner-Token": OWNER}


class FakeLauncher:
    def __init__(self):
        self.launched: list[str] = []
        self.observation = WorkerObservation(phase="queued")

    def launch(self, job_id: str) -> None:
        self.launched.append(job_id)

    def observe(self, job_id: str) -> WorkerObservation:
        return self.observation

    def delete(self, job_id: str) -> None:
        pass


def _wav_bytes() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * 2400)
    return output.getvalue()


def _create(client: TestClient, filename: str = "demo.wav"):
    return client.post(
        "/v1/jobs",
        headers=OWNER_HEADERS,
        files={"file": (filename, _wav_bytes(), "audio/wav")},
    )


def test_upload_creates_persisted_queued_job_and_launches_worker(tmp_path: Path):
    launcher = FakeLauncher()
    client = TestClient(create_app(data_root=tmp_path, launcher=launcher))

    response = _create(client)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert launcher.launched == [body["id"]]
    state = json.loads((tmp_path / "jobs" / body["id"] / "state.json").read_text())
    assert state["original_filename"] == "demo.wav"
    assert state["owner_token_sha256"] == hashlib.sha256(OWNER.encode()).hexdigest()
    assert "owner_token_sha256" not in body
    assert (tmp_path / "jobs" / body["id"] / "input" / "demo.wav").is_file()


def test_status_maps_unschedulable_gpu_to_actionable_failure(tmp_path: Path):
    launcher = FakeLauncher()
    client = TestClient(create_app(data_root=tmp_path, launcher=launcher))
    job_id = _create(client).json()["id"]
    launcher.observation = WorkerObservation(
        phase="pending",
        pending_message="0/1 nodes are available: 1 Insufficient nvidia.com/gpu",
        pending_seconds=121,
    )

    status = client.get(f"/v1/jobs/{job_id}", headers=OWNER_HEADERS)
    assert status.status_code == 200
    assert status.json()["status"] == "failed"
    assert status.json()["error_code"] == "gpu_busy"
    assert status.json()["message"] == GPU_BUSY_MESSAGE


def test_status_fails_closed_when_worker_exits_zero_without_artifacts(tmp_path: Path):
    launcher = FakeLauncher()
    client = TestClient(create_app(data_root=tmp_path, launcher=launcher))
    job_id = _create(client).json()["id"]
    launcher.observation = WorkerObservation(phase="terminated", exit_code=0)

    status = client.get(f"/v1/jobs/{job_id}", headers=OWNER_HEADERS)
    assert status.status_code == 200
    assert status.json()["status"] == "failed"
    assert status.json()["error_code"] == "artifacts_missing"


def test_rejects_unsupported_or_fake_audio(tmp_path: Path):
    client = TestClient(create_app(data_root=tmp_path, launcher=FakeLauncher()))
    bad_ext = client.post(
        "/v1/jobs",
        headers=OWNER_HEADERS,
        files={"file": ("demo.txt", b"hello", "text/plain")},
    )
    assert bad_ext.status_code == 415
    fake_wav = client.post(
        "/v1/jobs",
        headers=OWNER_HEADERS,
        files={"file": ("demo.wav", b"not wav", "audio/wav")},
    )
    assert fake_wav.status_code == 415


def test_success_exposes_only_declared_downloads(tmp_path: Path):
    launcher = FakeLauncher()
    client = TestClient(create_app(data_root=tmp_path, launcher=launcher))
    job_id = _create(client).json()["id"]
    result = tmp_path / "jobs" / job_id / "result"
    result.mkdir()
    (result / "demo_ALL_TRACKS.mid").write_bytes(b"MThd")
    (result / "demo_SheetSage2.zip").write_bytes(b"PK")
    (result / "report.html").write_text("<!doctype html>")
    (tmp_path / "jobs" / job_id / "state.json").write_text(
        json.dumps(
            {
                "status": "succeeded",
                "archive_name": "demo_SheetSage2.zip",
                "midi_name": "demo_ALL_TRACKS.mid",
                "report_html_name": "report.html",
                "report_json_name": "report.json",
                "owner_token_sha256": hashlib.sha256(OWNER.encode()).hexdigest(),
            }
        )
    )

    status = client.get(f"/v1/jobs/{job_id}", headers=OWNER_HEADERS).json()
    assert set(status["downloads"]) == {"archive", "midi", "report"}
    assert (
        client.get(f"/v1/jobs/{job_id}/downloads/midi", headers=OWNER_HEADERS).content
        == b"MThd"
    )
    traversal = client.get(
        f"/v1/jobs/{job_id}/downloads/../../state.json", headers=OWNER_HEADERS
    )
    assert traversal.status_code == 404


def test_jobs_are_private_to_the_owner_token(tmp_path: Path):
    client = TestClient(create_app(data_root=tmp_path, launcher=FakeLauncher()))
    created = _create(client, "private.wav")
    assert created.status_code == 202
    job_id = created.json()["id"]

    jobs = client.get("/v1/jobs", headers=OWNER_HEADERS).json()["jobs"]
    assert jobs[0]["id"] == job_id
    other_headers = {"X-Owner-Token": OTHER_OWNER}
    assert client.get("/v1/jobs", headers=other_headers).json() == {"jobs": []}
    assert client.get(f"/v1/jobs/{job_id}", headers=other_headers).status_code == 404
    assert client.get(f"/v1/jobs/{job_id}").status_code == 401
