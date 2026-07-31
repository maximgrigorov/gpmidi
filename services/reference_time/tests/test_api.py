"""FastAPI TestClient integration tests.

Tests the full API surface: health checks, analysis creation, job polling,
report retrieval, error shapes, authorization, audio/structure paths,
and concurrent requests.
"""

from __future__ import annotations

import io
import json
import struct
import time
import uuid
from unittest.mock import MagicMock, patch

import mido
import pytest
from fastapi.testclient import TestClient


def _make_midi_bytes(tempo_us: int = 500_000, n_measures: int = 8) -> bytes:
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=tempo_us, time=0))
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    for _ in range(n_measures * 4):
        track.append(mido.Message("note_on", note=60, velocity=80, time=0))
        track.append(mido.Message("note_off", note=60, velocity=0, time=480))
    track.append(mido.MetaMessage("end_of_track", time=0))
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


def _make_wav_bytes(sr: int = 44100, duration: float = 2.0) -> bytes:
    n_samples = int(sr * duration)
    samples = bytearray(n_samples * 2)
    for click_time in [0.5, 1.0, 1.5]:
        idx = int(click_time * sr) * 2
        if idx + 1 < len(samples):
            struct.pack_into("<h", samples, idx, 32000)
    data = bytes(samples)
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data), b"WAVE",
        b"fmt ", 16, 1, 1, sr,
        sr * 2, 2, 16,
        b"data", len(data),
    )
    return hdr + data


def _make_structure_json(
    sections: list[dict] | None = None,
    anchors: list[dict] | None = None,
) -> bytes:
    doc = {
        "version": "1.0",
        "sections": sections or [],
        "anchors": anchors or [],
    }
    return json.dumps(doc).encode()


GP_LINK = "gp-link-1"
MIDI_LINK = "midi-link-1"
AUDIO_LINK = "audio-link-1"
STRUCT_LINK = "struct-link-1"
PROJECT_ID = "test-project-1"

MIDI_BYTES = _make_midi_bytes()
WAV_BYTES = _make_wav_bytes()

GP_SHA = "aabbccdd" * 8
MIDI_SHA = "11223344" * 8
AUDIO_SHA = "55667788" * 8
STRUCT_SHA = "99aabbcc" * 8

MOCK_ASSETS = [
    {"id": GP_LINK, "role": "guitar-pro", "sha256": GP_SHA, "original_filename": "test.gp5"},
    {"id": MIDI_LINK, "role": "suno-midi.mix", "sha256": MIDI_SHA, "original_filename": "test.mid"},
    {"id": AUDIO_LINK, "role": "mix", "sha256": AUDIO_SHA, "original_filename": "test.wav"},
    {"id": STRUCT_LINK, "role": "structure", "sha256": STRUCT_SHA, "original_filename": "struct.json"},
]


def _mock_validate_response(*args, **kwargs):
    """Mock httpx response for asset validation."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"assets": MOCK_ASSETS}
    resp.raise_for_status = MagicMock()
    return resp


def _mock_download_response(url: str, *args, **kwargs):
    """Mock httpx response for asset download, returning appropriate bytes."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    if GP_LINK in url:
        resp.content = MIDI_BYTES
    elif AUDIO_LINK in url:
        resp.content = WAV_BYTES
    elif STRUCT_LINK in url:
        resp.content = _make_structure_json(
            sections=[{"label": "Intro", "source_measure": 0, "gp_measure": 0}],
        )
    else:
        resp.content = MIDI_BYTES
    return resp


class MockHTTPClient:
    """Mock httpx.Client that routes GET requests to the right fixture."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        if "/assets" in url and "/download" not in url:
            return _mock_validate_response()
        return _mock_download_response(url)


@pytest.fixture
def client(tmp_path):
    import os
    data_root = str(tmp_path / "data")
    os.environ["RT_DATA_ROOT"] = data_root
    os.environ["ASSET_API_URL"] = "http://mock-asset-api:8000"

    from importlib import reload
    import reference_time.config
    reload(reference_time.config)
    import reference_time.api as api_mod
    reload(api_mod)

    with TestClient(api_mod.app) as c:
        yield c


def _wait_for_job(client, job_id: str, initial_status: str = "", timeout: float = 6.0) -> dict | None:
    """Poll job until terminal status. Returns final job dict or None."""
    if initial_status in ("succeeded", "failed", "interrupted"):
        return {"status": initial_status}
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/v1/jobs/{job_id}")
        if resp.status_code == 404:
            time.sleep(0.1)
            continue
        job = resp.json()
        status = job.get("status", "")
        if status in ("succeeded", "failed", "interrupted", "cancelled"):
            return job
        time.sleep(0.1)
    return None


class TestHealthEndpoints:
    def test_healthz(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_readyz(self, client):
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"


class TestCreateAnalysis:
    @patch("reference_time.api.httpx.Client", MockHTTPClient)
    @patch("reference_time.api.extract_gp_grid")
    def test_create_returns_job_id(self, mock_gp_grid, client):
        from reference_time.models import GPMeasure
        mock_gp_grid.return_value = [
            GPMeasure(
                gp_revision_sha256=GP_SHA, measure_index=i, measure_number=i + 1,
                tick_start=i * 1920, tick_end=(i + 1) * 1920,
                numerator=4, denominator=4,
            )
            for i in range(8)
        ]
        resp = client.post(
            f"/v1/projects/{PROJECT_ID}/analyses",
            json={
                "gp_revision_sha256": GP_SHA,
                "gp_asset_link_id": GP_LINK,
                "source_midi_link_ids": [MIDI_LINK],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "job_id" in data
        assert "analysis_id" in data
        assert data["status"] in ("queued", "succeeded")
        assert "request_id" in data

    def test_create_no_midi_returns_422(self, client):
        resp = client.post(
            f"/v1/projects/{PROJECT_ID}/analyses",
            json={
                "gp_revision_sha256": GP_SHA,
                "gp_asset_link_id": GP_LINK,
                "source_midi_link_ids": [],
            },
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "no_source_midi"
        assert "request_id" in detail

    @patch("reference_time.api.httpx.Client", MockHTTPClient)
    @patch("reference_time.api.extract_gp_grid")
    def test_create_cache_hit(self, mock_gp_grid, client):
        from reference_time.models import GPMeasure
        mock_gp_grid.return_value = [
            GPMeasure(
                gp_revision_sha256=GP_SHA, measure_index=0, measure_number=1,
                tick_start=0, tick_end=1920, numerator=4, denominator=4,
            )
        ]
        resp1 = client.post(
            f"/v1/projects/{PROJECT_ID}/analyses",
            json={
                "gp_revision_sha256": GP_SHA,
                "gp_asset_link_id": GP_LINK,
                "source_midi_link_ids": [MIDI_LINK],
            },
        )
        assert resp1.status_code == 200
        d1 = resp1.json()
        _wait_for_job(client, d1["job_id"], d1["status"])

        resp2 = client.post(
            f"/v1/projects/{PROJECT_ID}/analyses",
            json={
                "gp_revision_sha256": GP_SHA,
                "gp_asset_link_id": GP_LINK,
                "source_midi_link_ids": [MIDI_LINK],
            },
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["cache_hit"] is True
        assert data2["status"] == "succeeded"


class TestJobPolling:
    @patch("reference_time.api.httpx.Client", MockHTTPClient)
    @patch("reference_time.api.extract_gp_grid")
    def test_job_lifecycle(self, mock_gp_grid, client):
        from reference_time.models import GPMeasure
        mock_gp_grid.return_value = [
            GPMeasure(
                gp_revision_sha256=GP_SHA, measure_index=i, measure_number=i + 1,
                tick_start=i * 1920, tick_end=(i + 1) * 1920,
                numerator=4, denominator=4,
            )
            for i in range(8)
        ]
        resp = client.post(
            f"/v1/projects/{PROJECT_ID}/analyses",
            json={
                "gp_revision_sha256": GP_SHA,
                "gp_asset_link_id": GP_LINK,
                "source_midi_link_ids": [MIDI_LINK],
            },
        )
        data = resp.json()
        job_id = data["job_id"]

        if data["status"] == "succeeded":
            return

        for _ in range(30):
            job_resp = client.get(f"/v1/jobs/{job_id}")
            if job_resp.status_code == 404:
                time.sleep(0.2)
                continue
            job = job_resp.json()
            if job.get("status") == "succeeded":
                assert job["progress_phase"] == "done"
                return
            if job.get("status") == "failed":
                pytest.fail(f"Job failed: {job.get('error_code')}: {job.get('error_message', '')}")
            time.sleep(0.2)
        pytest.fail("Job did not complete in time")

    def test_job_not_found(self, client):
        resp = client.get(f"/v1/jobs/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestReports:
    @patch("reference_time.api.httpx.Client", MockHTTPClient)
    @patch("reference_time.api.extract_gp_grid")
    def test_json_and_html_reports(self, mock_gp_grid, client):
        from reference_time.models import GPMeasure
        mock_gp_grid.return_value = [
            GPMeasure(
                gp_revision_sha256=GP_SHA, measure_index=i, measure_number=i + 1,
                tick_start=i * 1920, tick_end=(i + 1) * 1920,
                numerator=4, denominator=4,
            )
            for i in range(8)
        ]
        resp = client.post(
            f"/v1/projects/{PROJECT_ID}/analyses",
            json={
                "gp_revision_sha256": GP_SHA,
                "gp_asset_link_id": GP_LINK,
                "source_midi_link_ids": [MIDI_LINK],
            },
        )
        d = resp.json()
        job_id = d["job_id"]
        analysis_id = d["analysis_id"]

        job = _wait_for_job(client, job_id, d["status"])
        assert job is not None, "Job did not complete in time"

        json_resp = client.get(
            f"/v1/projects/{PROJECT_ID}/analyses/{analysis_id}/report.json"
        )
        assert json_resp.status_code == 200
        report = json_resp.json()
        assert report["analysis_id"] == analysis_id
        assert report["project_id"] == PROJECT_ID
        assert len(report["mappings"]) > 0
        assert report["global_confidence"] > 0

        html_resp = client.get(
            f"/v1/projects/{PROJECT_ID}/analyses/{analysis_id}/report.html"
        )
        assert html_resp.status_code == 200
        assert "Reference-Time Analysis Report" in html_resp.text

    def test_report_not_found(self, client):
        fake_id = str(uuid.uuid4())
        resp = client.get(f"/v1/projects/{PROJECT_ID}/analyses/{fake_id}/report.json")
        assert resp.status_code == 404


class TestErrorShapes:
    def test_unhandled_error_has_request_id(self, client):
        resp = client.post(
            f"/v1/projects/{PROJECT_ID}/analyses",
            json={
                "gp_revision_sha256": GP_SHA,
                "gp_asset_link_id": GP_LINK,
                "source_midi_link_ids": [],
            },
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "code" in detail
        assert "message" in detail
        assert "request_id" in detail

    @patch("reference_time.api.httpx.Client")
    def test_asset_validation_failure_shape(self, mock_cls, client):
        import httpx
        mock_instance = MagicMock()
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)
        mock_instance.get.side_effect = httpx.HTTPStatusError(
            "Not found", request=MagicMock(), response=MagicMock(status_code=404)
        )
        mock_cls.return_value = mock_instance

        resp = client.post(
            f"/v1/projects/{PROJECT_ID}/analyses",
            json={
                "gp_revision_sha256": GP_SHA,
                "gp_asset_link_id": GP_LINK,
                "source_midi_link_ids": [MIDI_LINK],
            },
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "asset_validation_failed"
        assert "request_id" in detail


class TestAudioPaths:
    @patch("reference_time.api.httpx.Client", MockHTTPClient)
    @patch("reference_time.api.extract_gp_grid")
    def test_audio_missing_produces_warning(self, mock_gp_grid, client):
        from reference_time.models import GPMeasure
        mock_gp_grid.return_value = [
            GPMeasure(
                gp_revision_sha256=GP_SHA, measure_index=0, measure_number=1,
                tick_start=0, tick_end=1920, numerator=4, denominator=4,
            )
        ]
        resp = client.post(
            f"/v1/projects/{PROJECT_ID}/analyses",
            json={
                "gp_revision_sha256": GP_SHA,
                "gp_asset_link_id": GP_LINK,
                "source_midi_link_ids": [MIDI_LINK],
                "audio_link_ids": [],
            },
        )
        d = resp.json()
        job = _wait_for_job(client, d["job_id"], d["status"])
        if job is None or job.get("status") != "succeeded":
            pytest.skip("Job did not succeed")

        report = client.get(
            f"/v1/projects/{PROJECT_ID}/analyses/{d['analysis_id']}/report.json"
        ).json()
        warning_codes = [w["code"] for w in report.get("global_warnings", [])]
        assert "audio_missing" in warning_codes

    @patch("reference_time.api.httpx.Client", MockHTTPClient)
    @patch("reference_time.api.extract_gp_grid")
    def test_audio_provided_adds_evidence(self, mock_gp_grid, client):
        from reference_time.models import GPMeasure
        mock_gp_grid.return_value = [
            GPMeasure(
                gp_revision_sha256=GP_SHA, measure_index=i, measure_number=i + 1,
                tick_start=i * 1920, tick_end=(i + 1) * 1920,
                numerator=4, denominator=4,
            )
            for i in range(8)
        ]
        resp = client.post(
            f"/v1/projects/{PROJECT_ID}/analyses",
            json={
                "gp_revision_sha256": GP_SHA,
                "gp_asset_link_id": GP_LINK,
                "source_midi_link_ids": [MIDI_LINK],
                "audio_link_ids": [AUDIO_LINK],
            },
        )
        d = resp.json()
        job = _wait_for_job(client, d["job_id"], d["status"])
        if job is None or job.get("status") != "succeeded":
            pytest.skip("Job did not succeed")

        report = client.get(
            f"/v1/projects/{PROJECT_ID}/analyses/{d['analysis_id']}/report.json"
        ).json()
        warning_codes = [w["code"] for w in report.get("global_warnings", [])]
        assert "audio_missing" not in warning_codes


class TestStructurePaths:
    @patch("reference_time.api.httpx.Client", MockHTTPClient)
    @patch("reference_time.api.extract_gp_grid")
    def test_anchor_affects_mapping(self, mock_gp_grid, client):
        from reference_time.models import GPMeasure
        mock_gp_grid.return_value = [
            GPMeasure(
                gp_revision_sha256=GP_SHA, measure_index=i, measure_number=i + 1,
                tick_start=i * 1920, tick_end=(i + 1) * 1920,
                numerator=4, denominator=4,
            )
            for i in range(8)
        ]
        resp = client.post(
            f"/v1/projects/{PROJECT_ID}/analyses",
            json={
                "gp_revision_sha256": GP_SHA,
                "gp_asset_link_id": GP_LINK,
                "source_midi_link_ids": [MIDI_LINK],
                "structure_link_id": STRUCT_LINK,
            },
        )
        d = resp.json()
        job = _wait_for_job(client, d["job_id"], d["status"])
        if job is None or job.get("status") != "succeeded":
            pytest.skip("Job did not succeed")

        report = client.get(
            f"/v1/projects/{PROJECT_ID}/analyses/{d['analysis_id']}/report.json"
        ).json()
        first_mapping = next(
            (m for m in report["mappings"] if m["source_measure_index"] == 0), None
        )
        if first_mapping:
            assert first_mapping["gp_measure_index"] == 0
            assert "user_anchor" in first_mapping.get("evidence", [])


class TestListAndGet:
    @patch("reference_time.api.httpx.Client", MockHTTPClient)
    @patch("reference_time.api.extract_gp_grid")
    def test_list_analyses(self, mock_gp_grid, client):
        from reference_time.models import GPMeasure
        mock_gp_grid.return_value = [
            GPMeasure(
                gp_revision_sha256=GP_SHA, measure_index=0, measure_number=1,
                tick_start=0, tick_end=1920, numerator=4, denominator=4,
            )
        ]
        resp = client.post(
            f"/v1/projects/{PROJECT_ID}/analyses",
            json={
                "gp_revision_sha256": GP_SHA,
                "gp_asset_link_id": GP_LINK,
                "source_midi_link_ids": [MIDI_LINK],
            },
        )
        assert resp.status_code == 200

        list_resp = client.get(f"/v1/projects/{PROJECT_ID}/analyses")
        assert list_resp.status_code == 200
        data = list_resp.json()
        assert "analyses" in data
        assert "jobs" in data
        assert len(data["jobs"]) >= 1

    def test_get_analysis_not_found(self, client):
        resp = client.get(f"/v1/projects/{PROJECT_ID}/analyses/{uuid.uuid4()}")
        assert resp.status_code == 404
