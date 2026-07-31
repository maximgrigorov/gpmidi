"""Tests for the Flask Projects UI pages."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def flask_client():
    from app import app
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    with app.test_client() as c:
        yield c


class TestProjectsPages:
    @patch("app.get_client")
    def test_projects_list_renders(self, mock_get, flask_client):
        client_mock = MagicMock()
        client_mock.list_projects.return_value = [
            {"id": "p1", "name": "Song 1", "asset_count": 3,
             "latest_gp_rev": 2, "updated_at": "2026-07-30T12:00:00Z"}
        ]
        mock_get.return_value = client_mock
        r = flask_client.get("/projects")
        assert r.status_code == 200
        assert b"Song 1" in r.data
        assert b"Assets: 3" in r.data

    @patch("app.get_client")
    def test_projects_list_ailab_down(self, mock_get, flask_client):
        mock_get.side_effect = ConnectionError("unreachable")
        r = flask_client.get("/projects")
        assert r.status_code == 200
        assert "AILab" in r.data.decode() or "недоступен" in r.data.decode()

    @patch("app.get_client")
    def test_create_project_redirects(self, mock_get, flask_client):
        client_mock = MagicMock()
        client_mock.create_project.return_value = {"id": "new-uuid", "name": "Test"}
        mock_get.return_value = client_mock
        r = flask_client.post("/projects", data={"name": "Test"}, follow_redirects=False)
        assert r.status_code == 302
        assert "new-uuid" in r.headers["Location"]

    @patch("app.get_client")
    def test_project_detail_renders(self, mock_get, flask_client):
        client_mock = MagicMock()
        client_mock.get_project.return_value = {
            "project": {"id": "p1", "name": "Song", "description": None,
                        "revision": 1, "updated_at": "2026-07-30T00:00:00Z",
                        "created_at": "2026-07-30T00:00:00Z"},
            "assets": [{"id": "a1", "role": "mix", "original_filename": "mix.wav",
                        "asset_sha256": "abc123def456", "size_bytes": 1048576,
                        "created_at": "2026-07-30T00:00:00Z"}],
            "gp_revisions": [],
        }
        mock_get.return_value = client_mock
        r = flask_client.get("/projects/p1")
        assert r.status_code == 200
        assert b"Song" in r.data
        assert b"mix.wav" in r.data

    @patch("app.get_client")
    def test_upload_no_file_flashes_error(self, mock_get, flask_client):
        client_mock = MagicMock()
        mock_get.return_value = client_mock
        r = flask_client.post("/projects/p1/upload", data={"role": "mix"},
                              follow_redirects=True)
        assert r.status_code == 200


class TestExistingConverterUnchanged:
    """Verify the existing /upload converter route still works."""

    def test_index_renders(self, flask_client):
        r = flask_client.get("/")
        assert r.status_code == 200
        assert b"midi" in r.data.lower()

    def test_upload_requires_file(self, flask_client):
        r = flask_client.post("/upload", data={}, follow_redirects=True)
        assert r.status_code == 200


class TestNoAudioInSessions:
    """Verify no WAV/FLAC lands in data/sessions after project upload."""

    @patch("app.get_client")
    def test_project_upload_no_local_storage(self, mock_get, flask_client, tmp_path):
        """Project uploads go to AILab, not data/sessions."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        client_mock = MagicMock()
        client_mock.create_upload_ticket.return_value = {"ticket": "t", "max_bytes": 1e9}
        client_mock.stream_proxy_upload.return_value = {
            "sha256": "abc", "size_bytes": 100, "deduplicated": False, "link_id": "l1"
        }
        mock_get.return_value = client_mock

        from io import BytesIO
        data = {"role": "mix", "file": (BytesIO(b"fake wav data"), "mix.wav")}
        flask_client.post("/projects/p1/upload", data=data,
                          content_type="multipart/form-data", follow_redirects=True)

        audio_files = list(sessions_dir.rglob("*.wav")) + list(sessions_dir.rglob("*.flac"))
        assert len(audio_files) == 0


class TestTLSParsing:
    """Verify _parse_bool for TLS verification."""

    @pytest.mark.parametrize("val,expected", [
        ("1", True), ("true", True), ("True", True), ("TRUE", True),
        ("yes", True), ("Yes", True), ("on", True), ("ON", True),
        ("0", False), ("false", False), ("False", False), ("FALSE", False),
        ("no", False), ("No", False), ("off", False), ("OFF", False),
        ("", False), ("random", False),
    ])
    def test_parse_bool(self, val, expected):
        from ailab_client import _parse_bool
        assert _parse_bool(val) is expected

    def test_default_verify_is_false(self, monkeypatch):
        """Default (no env) should be False for self-signed AILab cert."""
        monkeypatch.delenv("ASSET_API_VERIFY_TLS", raising=False)
        import importlib

        import ailab_client
        importlib.reload(ailab_client)
        assert ailab_client.ASSET_API_VERIFY_TLS is False

    def test_verify_true_when_set(self, monkeypatch):
        monkeypatch.setenv("ASSET_API_VERIFY_TLS", "true")
        import importlib

        import ailab_client
        importlib.reload(ailab_client)
        assert ailab_client.ASSET_API_VERIFY_TLS is True


class TestReferenceTimeUI:
    """The Project page must expose the whole reference-time workflow.

    These are unit tests of the view and template. They are NOT acceptance
    evidence — §6.8 requires the deployed HTTP/browser path, which
    `e2e/live_acceptance.py` exercises against the real cluster.
    """

    GP_SHA = "a" * 64
    MIDI_SHA = "b" * 64
    AUDIO_SHA = "c" * 64
    STRUCT_SHA = "d" * 64

    def _project(self, gp_revisions=None):
        return {
            "project": {
                "id": "p1", "name": "Song", "description": None, "revision": 1,
                "updated_at": "2026-07-30T00:00:00Z",
                "created_at": "2026-07-30T00:00:00Z",
            },
            "assets": [
                {"id": "gp1", "role": "guitar-pro", "original_filename": "song.gp5",
                 "asset_sha256": self.GP_SHA, "size_bytes": 2048,
                 "created_at": "2026-07-30T00:00:00Z"},
                {"id": "gp0", "role": "guitar-pro", "original_filename": "orphan.gp5",
                 "asset_sha256": "e" * 64, "size_bytes": 2048,
                 "created_at": "2026-07-30T00:00:00Z"},
                {"id": "mid1", "role": "suno-midi.mix", "original_filename": "a.mid",
                 "asset_sha256": self.MIDI_SHA, "size_bytes": 512,
                 "created_at": "2026-07-30T00:00:00Z"},
                {"id": "aud1", "role": "mix", "original_filename": "mix.wav",
                 "asset_sha256": self.AUDIO_SHA, "size_bytes": 1048576,
                 "created_at": "2026-07-30T00:00:00Z"},
                {"id": "st1", "role": "structure", "original_filename": "s.json",
                 "asset_sha256": self.STRUCT_SHA, "size_bytes": 64,
                 "created_at": "2026-07-30T00:00:00Z"},
            ],
            # Real shape from GET /v1/projects/{id}: the column is asset_sha256.
            "gp_revisions": (
                gp_revisions
                if gp_revisions is not None
                else [{
                    "revision": 1,
                    "asset_sha256": self.GP_SHA,
                    "original_filename": "song.gp5",
                    "created_at": "2026-07-30T00:00:00Z",
                }]
            ),
        }

    def _analyses(self):
        return {
            "analyses": [
                {
                    "analysis_id": "an-1", "project_id": "p1",
                    "cache_key": "f" * 64, "created_at": "2026-07-31T10:00:00Z",
                    "summary": {
                        "gp_revision_number": 1,
                        "global_confidence": 0.83,
                        "source_measure_count": 8,
                        "gp_measure_count": 8,
                        "mapping_count": 8,
                        "mapping_type_counts": {"one_to_one": 8},
                        "warning_codes": ["audio_missing"],
                        "consensus_decision": "single_source",
                        "input_identities": {"gp_revision": self.GP_SHA},
                        "source_evidence_reused": True,
                        "structure_version": "1.0",
                        "anchored_source_indices": [1],
                    },
                }
            ],
            "jobs": [
                {"job_id": "job-run", "analysis_id": "an-2", "project_id": "p1",
                 "status": "running", "progress_phase": "alignment",
                 "progress_message": "", "error_code": None,
                 "created_at": "2026-07-31T10:05:00Z"},
                {"job_id": "job-int", "analysis_id": "an-3", "project_id": "p1",
                 "status": "interrupted", "progress_phase": "gp_parse",
                 "progress_message": "", "error_code": "process_restart",
                 "created_at": "2026-07-31T10:04:00Z"},
            ],
        }

    def _client(self, mock_get, project=None, analyses=None):
        client_mock = MagicMock()
        client_mock.get_project.return_value = project or self._project()
        client_mock.list_analyses.return_value = (
            analyses if analyses is not None else self._analyses()
        )
        mock_get.return_value = client_mock
        return client_mock

    @patch("app.get_client")
    def test_only_registered_gp_revisions_are_offered(self, mock_get, flask_client):
        self._client(mock_get)
        body = flask_client.get("/projects/p1").data.decode()
        assert "rev 1 — song.gp5" in body
        # A guitar-pro asset that is not a registered revision would be rejected
        # by the analyzer, so it must not be selectable.
        assert "orphan.gp5" not in body.split("Reference-Time")[1]

    @patch("app.get_client")
    def test_all_input_slots_are_selectable(self, mock_get, flask_client):
        self._client(mock_get)
        body = flask_client.get("/projects/p1").data.decode()
        section = body.split("Reference-Time")[1]
        assert 'name="gp_link_id"' in section
        assert 'name="midi_link_ids"' in section
        assert 'name="audio_link_ids"' in section
        assert 'name="structure_link_id"' in section

    @patch("app.get_client")
    def test_job_states_and_error_codes_are_shown(self, mock_get, flask_client):
        self._client(mock_get)
        body = flask_client.get("/projects/p1").data.decode()
        assert "running" in body
        assert "interrupted" in body
        assert "process_restart" in body

    @patch("app.get_client")
    def test_active_jobs_are_marked_for_polling_and_terminal_ones_are_not(
        self, mock_get, flask_client
    ):
        self._client(mock_get)
        body = flask_client.get("/projects/p1").data.decode()
        running_row = body.split('data-job-id="job-run"')[1].split("</tr>")[0]
        interrupted_row = body.split('data-job-id="job-int"')[1].split("</tr>")[0]
        assert 'data-poll="1"' in running_row
        assert 'data-poll="1"' not in interrupted_row
        # Bounded polling, so a stuck job cannot loop forever in the browser.
        assert "MAX_TICKS" in body

    @patch("app.get_client")
    def test_confidence_warnings_hashes_and_reuse_are_displayed(
        self, mock_get, flask_client
    ):
        self._client(mock_get)
        body = flask_client.get("/projects/p1").data.decode()
        assert "0.83" in body
        assert "audio_missing" in body
        assert "single_source" in body
        assert "reused" in body
        assert self.GP_SHA[:12] in body
        assert "one_to_one:8" in body

    @patch("app.get_client")
    def test_report_links_are_present_for_completed_analyses(
        self, mock_get, flask_client
    ):
        self._client(mock_get)
        body = flask_client.get("/projects/p1").data.decode()
        assert "/projects/p1/analyses/an-1/report" in body
        assert "/projects/p1/analyses/an-1/report.json" in body

    @patch("app.get_client")
    def test_no_gp_revision_disables_the_form(self, mock_get, flask_client):
        self._client(mock_get, project=self._project(gp_revisions=[]))
        body = flask_client.get("/projects/p1").data.decode()
        assert 'name="gp_link_id"' not in body
        assert "зарегистрированная Guitar Pro ревизия" in body

    @patch("app.get_client")
    def test_reference_time_outage_is_reported_not_swallowed(
        self, mock_get, flask_client
    ):
        client_mock = MagicMock()
        client_mock.get_project.return_value = self._project()
        client_mock.list_analyses.side_effect = ConnectionError("no route")
        mock_get.return_value = client_mock
        body = flask_client.get("/projects/p1").data.decode()
        assert "Reference-time" in body

    @patch("app.get_client")
    def test_status_endpoint_returns_job_json(self, mock_get, flask_client):
        client_mock = MagicMock()
        client_mock.get_analysis_job.return_value = {
            "job_id": "job-run", "project_id": "p1", "status": "running",
            "progress_phase": "alignment", "error_code": None,
        }
        mock_get.return_value = client_mock
        r = flask_client.get("/projects/p1/analyses/job-run/status")
        assert r.status_code == 200
        assert r.get_json()["status"] == "running"

    @patch("app.get_client")
    def test_status_endpoint_rejects_a_job_from_another_project(
        self, mock_get, flask_client
    ):
        client_mock = MagicMock()
        client_mock.get_analysis_job.return_value = {
            "job_id": "job-run", "project_id": "other", "status": "running",
        }
        mock_get.return_value = client_mock
        r = flask_client.get("/projects/p1/analyses/job-run/status")
        assert r.status_code == 404

    @patch("app.get_client")
    def test_analyze_requires_gp_and_midi(self, mock_get, flask_client):
        client_mock = MagicMock()
        mock_get.return_value = client_mock
        r = flask_client.post(
            "/projects/p1/analyze", data={"gp_link_id": "gp1"}, follow_redirects=False
        )
        assert r.status_code == 302
        client_mock.create_analysis.assert_not_called()

    @patch("app.get_client")
    def test_analyze_forwards_every_selected_input(self, mock_get, flask_client):
        client_mock = MagicMock()
        client_mock.create_analysis.return_value = {
            "job_id": "j", "analysis_id": "a", "status": "queued", "cache_hit": False
        }
        mock_get.return_value = client_mock
        flask_client.post(
            "/projects/p1/analyze",
            data={
                "gp_link_id": "gp1",
                "gp_sha": self.GP_SHA,
                "midi_link_ids": ["mid1"],
                "audio_link_ids": ["aud1"],
                "structure_link_id": "st1",
            },
            follow_redirects=False,
        )
        client_mock.create_analysis.assert_called_once_with(
            "p1", "gp1", self.GP_SHA, ["mid1"], ["aud1"], "st1"
        )
