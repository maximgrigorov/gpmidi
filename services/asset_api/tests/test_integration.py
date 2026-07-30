"""Integration tests for Asset API using FastAPI TestClient."""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Each test gets its own fresh database and blob storage."""
    monkeypatch.setenv("ASSET_DATA_ROOT", str(tmp_path))
    import importlib
    import app.config
    importlib.reload(app.config)
    from app.config import DATA_ROOT
    assert DATA_ROOT == tmp_path
    (tmp_path / "db").mkdir()
    (tmp_path / "blobs" / "sha256").mkdir(parents=True)
    (tmp_path / "tmp" / "uploads").mkdir(parents=True)
    from app.database import init_db
    init_db(tmp_path / "db" / "projects.sqlite3")
    yield
    importlib.reload(app.config)


@pytest.fixture
def client(isolated_env):
    from app.main import app
    return TestClient(app)


def _wav_bytes(size: int = 100) -> bytes:
    """Generate a minimal valid WAV header + padding."""
    data_size = size - 44
    if data_size < 0:
        data_size = 0
    header = (
        b"RIFF"
        + struct.pack("<I", data_size + 36)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)
        + struct.pack("<HHIIHH", 1, 1, 44100, 44100 * 2, 2, 16)
        + b"data"
        + struct.pack("<I", data_size)
    )
    return header + b"\x00" * data_size


def _midi_bytes() -> bytes:
    """Minimal valid MIDI file."""
    return b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0MTrk\x00\x00\x00\x04\x00\xff/\x00"


def _gp5_bytes() -> bytes:
    """Fake GP5 header (just signature for validation)."""
    return b"FICHIER GUITAR PRO v5" + b"\x00" * 200


# --- Project CRUD ---

class TestProjectCRUD:
    def test_create_project(self, client):
        r = client.post("/v1/projects", json={"name": "My Song", "description": "A test"})
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "My Song"
        assert "id" in data

    def test_create_project_name_trimmed(self, client):
        r = client.post("/v1/projects", json={"name": "  Trimmed  "})
        assert r.status_code == 201
        assert r.json()["name"] == "Trimmed"

    def test_create_project_blank_name_fails(self, client):
        r = client.post("/v1/projects", json={"name": "   "})
        assert r.status_code == 422

    def test_list_projects(self, client):
        client.post("/v1/projects", json={"name": "P1"})
        client.post("/v1/projects", json={"name": "P2"})
        r = client.get("/v1/projects")
        assert r.status_code == 200
        assert len(r.json()["projects"]) == 2

    def test_get_project(self, client):
        cr = client.post("/v1/projects", json={"name": "Song"})
        pid = cr.json()["id"]
        r = client.get(f"/v1/projects/{pid}")
        assert r.status_code == 200
        assert r.json()["project"]["name"] == "Song"

    def test_get_nonexistent_project(self, client):
        r = client.get("/v1/projects/nonexistent-id")
        assert r.status_code == 404

    def test_update_project(self, client):
        cr = client.post("/v1/projects", json={"name": "Old"})
        pid = cr.json()["id"]
        r = client.patch(f"/v1/projects/{pid}", json={"name": "New", "revision": 1})
        assert r.status_code == 200
        assert r.json()["project"]["name"] == "New"
        assert r.json()["project"]["revision"] == 2

    def test_update_project_conflict(self, client):
        cr = client.post("/v1/projects", json={"name": "V1"})
        pid = cr.json()["id"]
        client.patch(f"/v1/projects/{pid}", json={"name": "V2", "revision": 1})
        r = client.patch(f"/v1/projects/{pid}", json={"name": "V3", "revision": 1})
        assert r.status_code == 409

    def test_delete_project(self, client):
        cr = client.post("/v1/projects", json={"name": "ToDelete"})
        pid = cr.json()["id"]
        r = client.delete(f"/v1/projects/{pid}")
        assert r.status_code == 204
        r2 = client.get(f"/v1/projects/{pid}")
        assert r2.status_code == 404


# --- Upload flow ---

class TestUpload:
    def _upload(self, client, project_id: str, filename: str, role: str, data: bytes):
        """Helper: create ticket then upload."""
        ticket_r = client.post(
            f"/v1/projects/{project_id}/upload-tickets",
            json={"role": role, "original_filename": filename},
        )
        assert ticket_r.status_code == 201, ticket_r.text
        ticket = ticket_r.json()["ticket"]
        upload_r = client.put(
            f"/v1/uploads/{ticket}",
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        return upload_r

    def test_upload_wav(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        wav = _wav_bytes(200)
        r = self._upload(client, pid, "mix.wav", "mix", wav)
        assert r.status_code == 201
        data = r.json()
        assert data["sha256"] == hashlib.sha256(wav).hexdigest()
        assert data["size_bytes"] == 200
        assert data["role"] == "mix"

    def test_dedup_same_bytes(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        wav = _wav_bytes(200)
        r1 = self._upload(client, pid, "mix.wav", "mix", wav)
        assert r1.status_code == 201
        r2 = self._upload(client, pid, "mix_copy.wav", "stem.drums", wav)
        assert r2.status_code == 201
        assert r2.json()["sha256"] == r1.json()["sha256"]
        assert r2.json()["deduplicated"] is True

    def test_same_blob_two_projects(self, client):
        cr1 = client.post("/v1/projects", json={"name": "P1"})
        cr2 = client.post("/v1/projects", json={"name": "P2"})
        pid1, pid2 = cr1.json()["id"], cr2.json()["id"]
        wav = _wav_bytes(150)
        r1 = self._upload(client, pid1, "mix.wav", "mix", wav)
        r2 = self._upload(client, pid2, "mix.wav", "mix", wav)
        assert r1.json()["sha256"] == r2.json()["sha256"]

    def test_oversized_upload_rejected(self, client, monkeypatch):
        monkeypatch.setenv("MAX_TEXT_BYTES", "50")
        import importlib, app.config
        importlib.reload(app.config)
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        r = self._upload(client, pid, "lyrics.txt", "lyrics", b"x" * 100)
        assert r.status_code == 413

    def test_invalid_signature_rejected(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        r = self._upload(client, pid, "bad.wav", "mix", b"NOT_A_WAV" + b"\x00" * 100)
        assert r.status_code == 415

    def test_wrong_extension_for_role(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        r = client.post(
            f"/v1/projects/{pid}/upload-tickets",
            json={"role": "mix", "original_filename": "song.mp3"},
        )
        assert r.status_code == 422

    def test_ticket_consumed_once(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        ticket_r = client.post(
            f"/v1/projects/{pid}/upload-tickets",
            json={"role": "mix", "original_filename": "mix.wav"},
        )
        ticket = ticket_r.json()["ticket"]
        wav = _wav_bytes(100)
        r1 = client.put(f"/v1/uploads/{ticket}", content=wav,
                        headers={"Content-Type": "application/octet-stream"})
        assert r1.status_code == 201
        r2 = client.put(f"/v1/uploads/{ticket}", content=wav,
                        headers={"Content-Type": "application/octet-stream"})
        assert r2.status_code == 410

    def test_expired_ticket(self, client, monkeypatch):
        monkeypatch.setenv("TICKET_TTL_SECONDS", "0")
        import importlib, app.config
        importlib.reload(app.config)
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        ticket_r = client.post(
            f"/v1/projects/{pid}/upload-tickets",
            json={"role": "lyrics", "original_filename": "lyrics.txt"},
        )
        ticket = ticket_r.json()["ticket"]
        import time
        time.sleep(0.1)
        r = client.put(f"/v1/uploads/{ticket}", content=b"hello",
                       headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 410

    def test_empty_upload_rejected(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        r = self._upload(client, pid, "lyrics.txt", "lyrics", b"")
        assert r.status_code == 422

    def test_failed_upload_no_blob(self, client):
        """After a failed upload, no blob or temp should remain."""
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        self._upload(client, pid, "bad.wav", "mix", b"NOT_WAV" + b"\x00" * 100)
        from app.config import BLOBS_DIR, TMP_UPLOADS_DIR
        blob_files = list(BLOBS_DIR.rglob("*"))
        blob_files = [f for f in blob_files if f.is_file()]
        tmp_files = list(TMP_UPLOADS_DIR.iterdir())
        assert len(blob_files) == 0
        assert len(tmp_files) == 0


# --- GP Revisions ---

class TestGPRevisions:
    def test_gp_upload_creates_revision(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        gp = _gp5_bytes()
        ticket_r = client.post(
            f"/v1/projects/{pid}/upload-tickets",
            json={"role": "guitar-pro", "original_filename": "song.gp5"},
        )
        ticket = ticket_r.json()["ticket"]
        r = client.put(f"/v1/uploads/{ticket}", content=gp,
                       headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 201
        proj = client.get(f"/v1/projects/{pid}").json()
        assert len(proj["gp_revisions"]) == 1
        assert proj["gp_revisions"][0]["revision"] == 1

    def test_gp_revision_sequence(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        for i in range(3):
            gp = _gp5_bytes() + bytes([i])  # different content each time
            ticket_r = client.post(
                f"/v1/projects/{pid}/upload-tickets",
                json={"role": "guitar-pro", "original_filename": f"song_v{i}.gp5"},
            )
            ticket = ticket_r.json()["ticket"]
            client.put(f"/v1/uploads/{ticket}", content=gp,
                       headers={"Content-Type": "application/octet-stream"})
        proj = client.get(f"/v1/projects/{pid}").json()
        revs = proj["gp_revisions"]
        assert len(revs) == 3
        assert [r["revision"] for r in revs] == [1, 2, 3]

    def test_same_gp_hash_no_new_revision(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        gp = _gp5_bytes()
        for _ in range(2):
            ticket_r = client.post(
                f"/v1/projects/{pid}/upload-tickets",
                json={"role": "guitar-pro", "original_filename": "song.gp5"},
            )
            ticket = ticket_r.json()["ticket"]
            client.put(f"/v1/uploads/{ticket}", content=gp,
                       headers={"Content-Type": "application/octet-stream"})
        proj = client.get(f"/v1/projects/{pid}").json()
        assert len(proj["gp_revisions"]) == 1


# --- Download ---

class TestDownload:
    def _upload_and_get_link(self, client, pid, filename, role, data):
        ticket_r = client.post(
            f"/v1/projects/{pid}/upload-tickets",
            json={"role": role, "original_filename": filename},
        )
        ticket = ticket_r.json()["ticket"]
        r = client.put(f"/v1/uploads/{ticket}", content=data,
                       headers={"Content-Type": "application/octet-stream"})
        return r.json()["link_id"]

    def test_download_by_link(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        wav = _wav_bytes(200)
        link_id = self._upload_and_get_link(client, pid, "mix.wav", "mix", wav)
        r = client.get(f"/v1/projects/{pid}/assets/{link_id}/download")
        assert r.status_code == 200
        assert r.content == wav
        assert "mix.wav" in r.headers["content-disposition"]

    def test_download_wrong_project(self, client):
        cr1 = client.post("/v1/projects", json={"name": "P1"})
        cr2 = client.post("/v1/projects", json={"name": "P2"})
        pid1, pid2 = cr1.json()["id"], cr2.json()["id"]
        wav = _wav_bytes(100)
        link_id = self._upload_and_get_link(client, pid1, "mix.wav", "mix", wav)
        r = client.get(f"/v1/projects/{pid2}/assets/{link_id}/download")
        assert r.status_code == 404

    def test_download_nonexistent_link(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        r = client.get(f"/v1/projects/{pid}/assets/fake-link/download")
        assert r.status_code == 404


# --- Delete link keeps blob ---

class TestDeleteLink:
    def test_delete_link_keeps_blob(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        wav = _wav_bytes(150)
        ticket_r = client.post(
            f"/v1/projects/{pid}/upload-tickets",
            json={"role": "mix", "original_filename": "mix.wav"},
        )
        ticket = ticket_r.json()["ticket"]
        up_r = client.put(f"/v1/uploads/{ticket}", content=wav,
                          headers={"Content-Type": "application/octet-stream"})
        link_id = up_r.json()["link_id"]
        sha = up_r.json()["sha256"]

        r = client.delete(f"/v1/projects/{pid}/assets/{link_id}")
        assert r.status_code == 204

        from app.storage import blob_abspath
        from app.config import BLOBS_DIR
        assert blob_abspath(sha, BLOBS_DIR).exists()


# --- Manifest ---

class TestManifest:
    def test_manifest_structure(self, client):
        cr = client.post("/v1/projects", json={"name": "ManifestTest"})
        pid = cr.json()["id"]
        wav = _wav_bytes(100)
        ticket_r = client.post(
            f"/v1/projects/{pid}/upload-tickets",
            json={"role": "mix", "original_filename": "mix.wav"},
        )
        ticket = ticket_r.json()["ticket"]
        client.put(f"/v1/uploads/{ticket}", content=wav,
                   headers={"Content-Type": "application/octet-stream"})

        r = client.get(f"/v1/projects/{pid}/manifest")
        assert r.status_code == 200
        m = r.json()
        assert m["schema_version"] == 1
        assert m["project"]["name"] == "ManifestTest"
        assert len(m["assets"]) == 1
        a = m["assets"][0]
        assert "sha256" in a
        assert "size_bytes" in a
        assert "role" in a
        assert "link_id" in a

    def test_manifest_no_absolute_paths(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        r = client.get(f"/v1/projects/{pid}/manifest")
        text = r.text
        assert "/var/lib" not in text
        assert "/tmp" not in text

    def test_manifest_stable_order(self, client):
        """Manifest order is deterministic across calls."""
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        wav = _wav_bytes(100)
        for name, role in [("drums.wav", "stem.drums"), ("bass.wav", "stem.bass")]:
            ticket_r = client.post(
                f"/v1/projects/{pid}/upload-tickets",
                json={"role": role, "original_filename": name},
            )
            ticket = ticket_r.json()["ticket"]
            client.put(f"/v1/uploads/{ticket}", content=wav + name.encode(),
                       headers={"Content-Type": "application/octet-stream"})
        m1 = client.get(f"/v1/projects/{pid}/manifest").json()
        m2 = client.get(f"/v1/projects/{pid}/manifest").json()
        assert m1 == m2


# --- Content-Disposition safety ---

class TestContentDisposition:
    def test_safe_disposition(self, client):
        cr = client.post("/v1/projects", json={"name": "P"})
        pid = cr.json()["id"]
        wav = _wav_bytes(100)
        ticket_r = client.post(
            f"/v1/projects/{pid}/upload-tickets",
            json={"role": "mix", "original_filename": "my song (final).wav"},
        )
        ticket = ticket_r.json()["ticket"]
        r = client.put(f"/v1/uploads/{ticket}", content=wav,
                       headers={"Content-Type": "application/octet-stream"})
        link_id = r.json()["link_id"]
        dl = client.get(f"/v1/projects/{pid}/assets/{link_id}/download")
        disp = dl.headers["content-disposition"]
        assert "attachment" in disp
        assert '"' not in disp.split("filename=")[1].strip('"')


# --- Health endpoints ---

class TestHealth:
    def test_healthz(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_readyz(self, client):
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"


# --- Persistence after restart ---

class TestPersistence:
    def test_data_survives_app_restart(self, tmp_path, monkeypatch):
        """Simulate pod restart: same DATA_ROOT, fresh app instance."""
        monkeypatch.setenv("ASSET_DATA_ROOT", str(tmp_path))
        import importlib, app.config
        importlib.reload(app.config)
        (tmp_path / "db").mkdir(exist_ok=True)
        (tmp_path / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
        (tmp_path / "tmp" / "uploads").mkdir(parents=True, exist_ok=True)
        from app.database import init_db
        init_db(tmp_path / "db" / "projects.sqlite3")

        from app.main import app as fastapi_app
        c1 = TestClient(fastapi_app)
        cr = c1.post("/v1/projects", json={"name": "Persistent"})
        pid = cr.json()["id"]
        wav = _wav_bytes(100)
        ticket_r = c1.post(f"/v1/projects/{pid}/upload-tickets",
                           json={"role": "mix", "original_filename": "m.wav"})
        ticket = ticket_r.json()["ticket"]
        c1.put(f"/v1/uploads/{ticket}", content=wav,
               headers={"Content-Type": "application/octet-stream"})

        c2 = TestClient(fastapi_app)
        r = c2.get(f"/v1/projects/{pid}")
        assert r.status_code == 200
        assert r.json()["project"]["name"] == "Persistent"
        assert len(r.json()["assets"]) == 1
