"""Unit tests for roles, validation, storage, cache_key modules."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from app.roles import (
    AssetRole,
    allowed_extensions_for_role,
    max_bytes_for_role,
    sanitize_filename,
    validate_extension,
    validate_signature,
)
from app.storage import StreamingHashWriter, blob_relpath, cleanup_stale_temps
from app.cache_key import cache_key, canonical_json


# --- Role / extension validation ---

class TestRoleExtension:
    def test_audio_extensions(self):
        for role in [AssetRole.MIX, AssetRole.STEM_DRUMS, AssetRole.STEM_BASS]:
            exts = allowed_extensions_for_role(role)
            assert ".wav" in exts
            assert ".flac" in exts
            assert ".mp3" not in exts

    def test_midi_extensions(self):
        for role in [AssetRole.SUNO_MIDI_MIX, AssetRole.SUNO_MIDI_DRUMS]:
            exts = allowed_extensions_for_role(role)
            assert ".mid" in exts
            assert ".midi" in exts

    def test_gp_extensions(self):
        exts = allowed_extensions_for_role(AssetRole.GUITAR_PRO)
        assert ".gp5" in exts
        assert ".gpx" in exts
        assert ".gp" in exts

    def test_text_extensions(self):
        for role in [AssetRole.LYRICS, AssetRole.STRUCTURE]:
            exts = allowed_extensions_for_role(role)
            assert ".txt" in exts
            assert ".md" in exts
            assert ".json" in exts

    def test_validate_extension_valid(self):
        assert validate_extension("song.wav", AssetRole.MIX) == ".wav"
        assert validate_extension("TRACK.FLAC", AssetRole.STEM_GUITAR) == ".flac"
        assert validate_extension("tabs.gp5", AssetRole.GUITAR_PRO) == ".gp5"

    def test_validate_extension_invalid(self):
        assert validate_extension("song.mp3", AssetRole.MIX) is None
        assert validate_extension("song.wav", AssetRole.GUITAR_PRO) is None


# --- Signature validation ---

class TestSignatureValidation:
    def test_wav_signature(self):
        header = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 50
        assert validate_signature(header, ".wav") is True

    def test_wav_bad_signature(self):
        header = b"NOTW" + b"\x00" * 60
        assert validate_signature(header, ".wav") is False

    def test_flac_signature(self):
        header = b"fLaC" + b"\x00" * 60
        assert validate_signature(header, ".flac") is True

    def test_midi_signature(self):
        header = b"MThd" + b"\x00" * 60
        assert validate_signature(header, ".mid") is True

    def test_unknown_extension_passes(self):
        assert validate_signature(b"anything", ".xyz") is True

    def test_short_header_fails(self):
        assert validate_signature(b"RI", ".wav") is False


# --- Filename sanitization ---

class TestSanitizeFilename:
    def test_normal_filename(self):
        assert sanitize_filename("song.wav") == "song.wav"

    def test_traversal_rejected(self):
        with pytest.raises(ValueError, match="traversal"):
            sanitize_filename("../etc/passwd")

    def test_traversal_backslash(self):
        with pytest.raises(ValueError, match="traversal"):
            sanitize_filename("..\\windows\\system32")

    def test_slash_rejected(self):
        with pytest.raises(ValueError, match="unsafe"):
            sanitize_filename("path/file.wav")

    def test_null_rejected(self):
        with pytest.raises(ValueError, match="unsafe"):
            sanitize_filename("file\x00.wav")

    def test_hidden_file_rejected(self):
        with pytest.raises(ValueError, match="hidden"):
            sanitize_filename(".htaccess")

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="too long or empty"):
            sanitize_filename("")

    def test_too_long_rejected(self):
        with pytest.raises(ValueError, match="too long or empty"):
            sanitize_filename("a" * 256)


# --- Blob path derivation ---

class TestBlobPath:
    def test_path_structure(self):
        sha = "abcdef1234567890" * 4
        rel = blob_relpath(sha)
        assert rel == f"ab/cd/{sha}"

    def test_lowercase(self):
        sha = "ABCDEF1234567890" * 4
        rel = blob_relpath(sha)
        assert rel.startswith("ab/cd/")
        assert rel.endswith(sha.lower())


# --- Streaming hash writer ---

class TestStreamingHashWriter:
    def test_write_and_finalize(self):
        with tempfile.TemporaryDirectory() as td:
            writer = StreamingHashWriter(tmp_dir=Path(td))
            data = b"hello world"
            writer.write(data)
            sha = writer.finalize()
            assert sha == hashlib.sha256(data).hexdigest()
            assert writer.size == len(data)

    def test_abort_removes_temp(self):
        with tempfile.TemporaryDirectory() as td:
            writer = StreamingHashWriter(tmp_dir=Path(td))
            writer.write(b"data")
            tmp = writer.tmp_path
            assert tmp.exists()
            writer.abort()
            assert not tmp.exists()

    def test_commit_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            blobs_dir = Path(td) / "blobs"
            blobs_dir.mkdir()
            tmp_dir = Path(td) / "tmp"
            tmp_dir.mkdir()

            data = b"same content"
            sha = hashlib.sha256(data).hexdigest()

            w1 = StreamingHashWriter(tmp_dir=tmp_dir)
            w1.write(data)
            w1.finalize()
            p1 = w1.commit(sha, blobs_dir)
            assert p1.exists()

            w2 = StreamingHashWriter(tmp_dir=tmp_dir)
            w2.write(data)
            w2.finalize()
            p2 = w2.commit(sha, blobs_dir)
            assert p2 == p1
            assert not w2.tmp_path.exists()

    def test_size_tracking(self):
        with tempfile.TemporaryDirectory() as td:
            writer = StreamingHashWriter(tmp_dir=Path(td))
            writer.write(b"abc")
            writer.write(b"def")
            assert writer.size == 6
            writer.abort()


# --- Stale temp cleanup ---

class TestCleanupTemps:
    def test_removes_old_files(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            old = Path(td) / "upload_old"
            old.write_bytes(b"x")
            os.utime(str(old), (time.time() - 7200, time.time() - 7200))
            fresh = Path(td) / "upload_fresh"
            fresh.write_bytes(b"y")
            removed = cleanup_stale_temps(Path(td), max_age_seconds=3600)
            assert removed == 1
            assert not old.exists()
            assert fresh.exists()


# --- Cache key ---

class TestCacheKey:
    def test_deterministic(self):
        k1 = cache_key("abc123", "basic_pitch", "1.0.0")
        k2 = cache_key("abc123", "basic_pitch", "1.0.0")
        assert k1 == k2

    def test_changes_on_input(self):
        k1 = cache_key("abc123", "basic_pitch", "1.0.0")
        k2 = cache_key("def456", "basic_pitch", "1.0.0")
        assert k1 != k2

    def test_changes_on_version(self):
        k1 = cache_key("abc123", "basic_pitch", "1.0.0")
        k2 = cache_key("abc123", "basic_pitch", "2.0.0")
        assert k1 != k2

    def test_changes_on_params(self):
        k1 = cache_key("abc123", "proc", "1.0", parameters={"threshold": 0.5})
        k2 = cache_key("abc123", "proc", "1.0", parameters={"threshold": 0.6})
        assert k1 != k2

    def test_canonical_json_sorted_keys(self):
        j1 = canonical_json({"b": 1, "a": 2})
        j2 = canonical_json({"a": 2, "b": 1})
        assert j1 == j2

    def test_canonical_json_rejects_nan(self):
        with pytest.raises((ValueError, OverflowError)):
            canonical_json({"x": float("nan")})


# --- GP revision numbering (via direct DB) ---

class TestGPRevisionNumbering:
    def test_monotonic_revisions(self):
        from app.database import init_db, get_db
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            init_db(db_path)
            with get_db(db_path) as conn:
                pid = "proj-1"
                conn.execute(
                    "INSERT INTO projects (id,name,created_at,updated_at,revision) VALUES (?,?,?,?,1)",
                    (pid, "Test", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                )
                conn.execute(
                    "INSERT INTO assets (sha256,size_bytes,media_type,original_name,blob_relpath,created_at) VALUES (?,?,?,?,?,?)",
                    ("aaa", 100, "application/octet-stream", "f.gp5", "aa/aa/aaa", "2026-01-01T00:00:00Z"),
                )
                conn.execute(
                    "INSERT INTO assets (sha256,size_bytes,media_type,original_name,blob_relpath,created_at) VALUES (?,?,?,?,?,?)",
                    ("bbb", 200, "application/octet-stream", "f2.gp5", "bb/bb/bbb", "2026-01-01T00:00:00Z"),
                )
                conn.execute(
                    "INSERT INTO gp_revisions (id,project_id,revision,asset_sha256,original_filename,created_at) VALUES (?,?,?,?,?,?)",
                    ("r1", pid, 1, "aaa", "f.gp5", "2026-01-01T00:00:00Z"),
                )
                conn.execute(
                    "INSERT INTO gp_revisions (id,project_id,revision,asset_sha256,original_filename,created_at) VALUES (?,?,?,?,?,?)",
                    ("r2", pid, 2, "bbb", "f2.gp5", "2026-01-01T00:00:00Z"),
                )
                conn.commit()
                rows = conn.execute(
                    "SELECT revision FROM gp_revisions WHERE project_id=? ORDER BY revision", (pid,)
                ).fetchall()
                assert [r["revision"] for r in rows] == [1, 2]

    def test_idempotent_same_hash(self):
        """Same GP hash should not create a new revision unless force_new."""
        from app.database import init_db, get_db
        from app.main import _create_gp_revision
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            init_db(db_path)
            with get_db(db_path) as conn:
                pid = "proj-1"
                conn.execute(
                    "INSERT INTO projects (id,name,created_at,updated_at,revision) VALUES (?,?,?,?,1)",
                    (pid, "Test", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                )
                conn.execute(
                    "INSERT INTO assets (sha256,size_bytes,media_type,original_name,blob_relpath,created_at) VALUES (?,?,?,?,?,?)",
                    ("sha1", 100, "x", "f.gp5", "sh/a1/sha1", "now"),
                )
                conn.commit()
                r1 = _create_gp_revision(conn, pid, "sha1", "f.gp5", "now")
                conn.commit()
                r2 = _create_gp_revision(conn, pid, "sha1", "f.gp5", "now")
                assert r2["revision"] == r1["revision"]


import os
