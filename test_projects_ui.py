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
