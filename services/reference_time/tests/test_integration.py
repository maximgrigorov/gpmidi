"""Integration tests for reference-time analysis.

Tests the database layer, job lifecycle, cache behavior, and API.
"""

from __future__ import annotations

import json
import uuid

import pytest

from reference_time.database import AnalysisDB
from reference_time.cache import compute_cache_key
from reference_time.models import ReferenceTimeAnalysis
from reference_time.report import generate_json_report


class TestDatabase:
    def test_create_and_get_job(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        job_id = str(uuid.uuid4())
        analysis_id = str(uuid.uuid4())
        db.create_job(job_id, analysis_id, "proj1", "cache1")

        job = db.get_job(job_id)
        assert job is not None
        assert job["job_id"] == job_id
        assert job["status"] == "queued"
        db.close()

    def test_update_job_status(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        job_id = str(uuid.uuid4())
        db.create_job(job_id, str(uuid.uuid4()), "proj1", "cache1")

        db.update_job_status(job_id, "running", "midi_extract", "Processing")
        job = db.get_job(job_id)
        assert job["status"] == "running"
        assert job["started_at"] is not None

        db.update_job_status(job_id, "succeeded", "done", "Complete")
        job = db.get_job(job_id)
        assert job["status"] == "succeeded"
        assert job["finished_at"] is not None
        db.close()

    def test_recover_interrupted(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        job_id = str(uuid.uuid4())
        db.create_job(job_id, str(uuid.uuid4()), "proj1", "cache1")
        db.update_job_status(job_id, "running")

        db.recover_interrupted_jobs()
        job = db.get_job(job_id)
        assert job["status"] == "interrupted"
        assert job["error_code"] == "process_restart"
        db.close()

    def test_store_and_find_result(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        analysis_id = str(uuid.uuid4())
        cache_key = "test_cache_key"
        result_json = '{"test": true}'

        db.store_result(analysis_id, "proj1", cache_key, result_json)

        found = db.find_cached_result(cache_key)
        assert found is not None
        assert found["analysis_id"] == analysis_id
        assert found["result_json"] == result_json
        db.close()

    def test_cache_miss(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        assert db.find_cached_result("nonexistent") is None
        db.close()

    def test_failed_not_cached(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        job_id = str(uuid.uuid4())
        db.create_job(job_id, str(uuid.uuid4()), "proj1", "cache1")
        db.update_job_status(
            job_id, "failed",
            error_code="test_error",
            error_message="Test failure",
        )

        assert db.find_cached_result("cache1") is None
        db.close()

    def test_list_jobs_for_project(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        for i in range(3):
            db.create_job(str(uuid.uuid4()), str(uuid.uuid4()), "proj1", f"cache{i}")
        db.create_job(str(uuid.uuid4()), str(uuid.uuid4()), "proj2", "other")

        jobs = db.get_jobs_for_project("proj1")
        assert len(jobs) == 3
        db.close()

    def test_count_active_jobs(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        j1 = str(uuid.uuid4())
        j2 = str(uuid.uuid4())
        db.create_job(j1, str(uuid.uuid4()), "proj1", "c1")
        db.create_job(j2, str(uuid.uuid4()), "proj1", "c2")
        assert db.count_active_jobs() == 2

        db.update_job_status(j1, "succeeded")
        assert db.count_active_jobs() == 1
        db.close()


class TestCacheInvalidation:
    def test_new_gp_reuses_source_evidence(self):
        from reference_time.cache import compute_source_evidence_cache_key

        src_key1 = compute_source_evidence_cache_key(
            ["midi_sha1"], {"reference_time": "0.1.0"}
        )
        src_key2 = compute_source_evidence_cache_key(
            ["midi_sha1"], {"reference_time": "0.1.0"}
        )
        assert src_key1 == src_key2

        full_key1 = compute_cache_key(
            "gp_sha_v1", ["midi_sha1"], [], None,
            {"reference_time": "0.1.0"}, {"p": 1}
        )
        full_key2 = compute_cache_key(
            "gp_sha_v2", ["midi_sha1"], [], None,
            {"reference_time": "0.1.0"}, {"p": 1}
        )
        assert full_key1 != full_key2

    def test_changed_midi_invalidates(self):
        k1 = compute_cache_key(
            "gp_sha", ["midi_sha1"], [], None,
            {"reference_time": "0.1.0"}, {"p": 1}
        )
        k2 = compute_cache_key(
            "gp_sha", ["midi_sha2"], [], None,
            {"reference_time": "0.1.0"}, {"p": 1}
        )
        assert k1 != k2

    def test_html_only_no_rerun(self):
        """Canonical JSON identity is independent of HTML template."""
        a = ReferenceTimeAnalysis(
            analysis_id="a1",
            project_id="p1",
            gp_revision_sha256="gp",
            global_confidence=0.5,
            cache_key="key1",
        )
        j1 = generate_json_report(a)
        j2 = generate_json_report(a)
        assert json.loads(j1) == json.loads(j2)
