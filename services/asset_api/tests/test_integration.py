"""Integration tests for Asset API using FastAPI TestClient."""

from __future__ import annotations

import hashlib
import struct

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Each test gets its own fresh database and blob storage."""
    monkeypatch.setenv("ASSET_DATA_ROOT", str(tmp_path))
    import importlib
    import asset_api.config
    importlib.reload(asset_api.config)
    from asset_api.config import DATA_ROOT
    assert DATA_ROOT == tmp_path
    (tmp_path / "db").mkdir()
    (tmp_path / "blobs" / "sha256").mkdir(parents=True)
    (tmp_path / "tmp" / "uploads").mkdir(parents=True)
    from asset_api.database import init_db
    init_db(tmp_path / "db" / "projects.sqlite3")
    yield
    importlib.reload(asset_api.config)


@pytest.fixture
def client(isolated_env):
    from asset_api.main import app
    return TestClient(app)


@pytest.fixture
def project_id(client):
    """Create a project and return its id."""
    r = client.post("/v1/projects", json={"name": "Test Project"})
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture(autouse=True)
def reset_limiters():
    """Reset rate/concurrency limiters between tests."""
    yield
    from asset_api.main import ticket_rate_limiter, upload_concurrency
    ticket_rate_limiter.reset()
    upload_concurrency.reset()


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
    """Fake GP5 header (length-prefixed signature for validation)."""
    return b"\x18FICHIER GUITAR PRO v5.10" + b"\x00" * 200


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
        import importlib
        import asset_api.config
        importlib.reload(asset_api.config)
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
        import importlib
        import asset_api.config
        importlib.reload(asset_api.config)
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
        from asset_api.config import BLOBS_DIR, TMP_UPLOADS_DIR
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

        from asset_api.storage import blob_abspath
        from asset_api.config import BLOBS_DIR
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
        import importlib
        import asset_api.config
        importlib.reload(asset_api.config)
        (tmp_path / "db").mkdir(exist_ok=True)
        (tmp_path / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
        (tmp_path / "tmp" / "uploads").mkdir(parents=True, exist_ok=True)
        from asset_api.database import init_db
        init_db(tmp_path / "db" / "projects.sqlite3")

        from asset_api.main import app as fastapi_app
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


class TestConcurrentTicket:
    """Verify atomic ticket claim prevents concurrent consumption."""

    def test_same_ticket_concurrent_upload(self, client, project_id):
        """Two concurrent uploads with the same ticket: exactly one succeeds."""
        import threading

        wav = _wav_bytes(200)
        ticket_r = client.post(
            f"/v1/projects/{project_id}/upload-tickets",
            json={"role": "mix", "original_filename": "dup.wav"},
        )
        ticket = ticket_r.json()["ticket"]

        results = []
        barrier = threading.Barrier(2)

        def upload():
            barrier.wait()
            r = client.put(
                f"/v1/uploads/{ticket}",
                content=wav,
                headers={"Content-Type": "application/octet-stream"},
            )
            results.append(r.status_code)

        threads = [threading.Thread(target=upload) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results) in ([201, 409], [201, 410]), f"Expected one 201 and one 409/410, got {results}"

    def test_two_uploads_same_bytes_dedup(self, client, project_id):
        """Two uploads with identical content produce one physical blob, two project_assets links (via different roles)."""
        wav = _wav_bytes(150)
        roles = ["mix", "stem.drums"]
        links = []
        for role in roles:
            ticket_r = client.post(
                f"/v1/projects/{project_id}/upload-tickets",
                json={"role": role, "original_filename": "dup.wav"},
            )
            ticket = ticket_r.json()["ticket"]
            r = client.put(
                f"/v1/uploads/{ticket}",
                content=wav,
                headers={"Content-Type": "application/octet-stream"},
            )
            assert r.status_code == 201
            links.append(r.json())

        assert links[0]["sha256"] == links[1]["sha256"]
        assert links[0]["link_id"] != links[1]["link_id"]
        assert links[1]["deduplicated"] is True


class TestRateLimiter:
    """Verify rate and concurrency limiting."""

    def test_ticket_rate_limit(self, client, project_id):
        """Exceed ticket rate limit and get 429."""
        from asset_api.main import ticket_rate_limiter
        ticket_rate_limiter.reset()
        ticket_rate_limiter.max_per_minute = 2

        for i in range(2):
            r = client.post(
                f"/v1/projects/{project_id}/upload-tickets",
                json={"role": "mix", "original_filename": f"f{i}.wav"},
            )
            assert r.status_code == 201

        r = client.post(
            f"/v1/projects/{project_id}/upload-tickets",
            json={"role": "mix", "original_filename": "blocked.wav"},
        )
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        body = r.json()
        assert body["detail"]["code"] == "rate_limit_exceeded"

        ticket_rate_limiter.max_per_minute = 30
        ticket_rate_limiter.reset()

    def test_concurrent_upload_limit(self, client, project_id):
        """A request overlapping a held upload receives 429 deterministically."""
        import asyncio
        import threading

        import httpx

        from asset_api.main import app, upload_concurrency

        wav = _wav_bytes(200)
        tickets = []
        for filename in ("occupy.wav", "blocked.wav"):
            response = client.post(
                f"/v1/projects/{project_id}/upload-tickets",
                json={"role": "mix", "original_filename": filename},
            )
            tickets.append(response.json()["ticket"])

        async def exercise_limit():
            upload_concurrency.reset()
            old_max = upload_concurrency.max_concurrent
            upload_concurrency._semaphore = threading.Semaphore(1)
            upload_concurrency.max_concurrent = 1
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_body():
                yield wav[:64]
                started.set()
                await release.wait()
                yield wav[64:]

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as async_client:
                first = asyncio.create_task(async_client.put(
                    f"/v1/uploads/{tickets[0]}", content=slow_body()
                ))
                await asyncio.wait_for(started.wait(), timeout=2)
                blocked = await async_client.put(
                    f"/v1/uploads/{tickets[1]}", content=wav
                )
                release.set()
                completed = await first

            upload_concurrency.reset()
            upload_concurrency._semaphore = threading.Semaphore(old_max)
            upload_concurrency.max_concurrent = old_max
            return completed, blocked

        completed, blocked = asyncio.run(exercise_limit())
        assert completed.status_code == 201
        assert blocked.status_code == 429
        assert blocked.json()["detail"]["code"] == "too_many_uploads"
        assert "Retry-After" in blocked.headers


class TestDeleteCascadeTickets:
    """Verify that project deletion invalidates pending tickets."""

    def test_ticket_invalid_after_project_delete(self, client, project_id):
        """Issue ticket, delete project, PUT should fail."""
        ticket_r = client.post(
            f"/v1/projects/{project_id}/upload-tickets",
            json={"role": "mix", "original_filename": "orphan.wav"},
        )
        ticket = ticket_r.json()["ticket"]

        client.delete(f"/v1/projects/{project_id}")

        wav = _wav_bytes(100)
        r = client.put(
            f"/v1/uploads/{ticket}",
            content=wav,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert r.status_code in (404, 410), f"Expected 404 or 410, got {r.status_code}"


class TestBlankNamePatch:
    """Verify that PATCH with blank name is rejected."""

    def test_patch_blank_name_rejected(self, client, project_id):
        r = client.patch(
            f"/v1/projects/{project_id}",
            json={"name": "   ", "revision": 1},
        )
        assert r.status_code == 422

    def test_patch_valid_name(self, client, project_id):
        r = client.patch(
            f"/v1/projects/{project_id}",
            json={"name": "New Name", "revision": 1},
        )
        assert r.status_code == 200
        assert r.json()["project"]["name"] == "New Name"


class TestRequestId:
    """Verify all error responses contain request_id."""

    def test_404_has_request_id(self, client):
        r = client.get("/v1/projects/nonexistent-id")
        assert r.status_code == 404
        body = r.json()
        assert "request_id" in body["detail"]

    def test_422_has_request_id(self, client):
        r = client.post("/v1/projects", json={})
        assert r.status_code == 422
        body = r.json()
        assert "request_id" in body["detail"]
        assert r.headers["X-Request-Id"] == body["detail"]["request_id"]

    def test_unexpected_error_is_sanitized_and_has_request_id(self, isolated_env):
        from fastapi.testclient import TestClient

        from asset_api.main import app

        path = f"/__test_unexpected_error_{id(self)}"

        def explode():
            raise RuntimeError("/private/path/must-not-leak")

        app.add_api_route(path, explode, methods=["GET"])
        with TestClient(app, raise_server_exceptions=False) as test_client:
            response = test_client.get(path)

        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail["code"] == "internal_error"
        assert detail["message"] == "Internal server error"
        assert "/private/path" not in response.text
        assert response.headers["X-Request-Id"] == detail["request_id"]


class TestInterruptedUploadRecovery:
    def test_uploading_ticket_is_failed_on_recovery(self, isolated_env):
        from datetime import datetime, timezone

        from asset_api.config import DB_PATH
        from asset_api.database import get_db, recover_interrupted_uploads

        with get_db() as conn:
            conn.execute(
                "INSERT INTO projects "
                "(id, name, description, created_at, updated_at, revision) "
                "VALUES ('p1', 'P', NULL, ?, ?, 1)",
                (datetime.now(timezone.utc).isoformat(),) * 2,
            )
            conn.execute(
                "INSERT INTO upload_tickets "
                "(ticket_hash, project_id, role, original_filename, max_bytes, "
                "expires_at, status) VALUES ('t1', 'p1', 'mix', 'x.wav', 100, ?, 'uploading')",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.commit()

        assert recover_interrupted_uploads(DB_PATH) == 1
        with get_db() as conn:
            status = conn.execute(
                "SELECT status FROM upload_tickets WHERE ticket_hash='t1'"
            ).fetchone()["status"]
        assert status == "failed"
