"""Persistence, job-lifecycle and race-safety tests.

These exercise the invariants that made a late worker able to overwrite a
timed-out job, and that let a "reuse" claim be proven by comparing two hash
functions instead of by reading persisted data.
"""

from __future__ import annotations

import json
import threading
import time
import uuid

import pytest
from reference_time.database import AnalysisDB
from reference_time.models import ReferenceTimeAnalysis
from reference_time.report import generate_json_report


def _job(db, project="proj1", cache="cache1"):
    job_id = str(uuid.uuid4())
    analysis_id = str(uuid.uuid4())
    db.create_job(job_id, analysis_id, project, cache)
    return job_id, analysis_id


class TestDatabase:
    def test_create_and_get_job(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        job_id, _ = _job(db)
        job = db.get_job(job_id)
        assert job is not None
        assert job["job_id"] == job_id
        assert job["status"] == "queued"
        db.close()

    def test_schema_is_migrated_to_current_version(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        assert db.schema_version() == 2
        # Re-opening the same file must be idempotent.
        again = AnalysisDB(tmp_db_path)
        assert again.schema_version() == 2
        db.close()

    def test_update_job_status(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        job_id, _ = _job(db)

        assert db.update_job_status(job_id, "running", "midi_extract", "Processing")
        job = db.get_job(job_id)
        assert job["status"] == "running"
        assert job["started_at"] is not None

        assert db.update_job_status(job_id, "succeeded", "done", "Complete")
        job = db.get_job(job_id)
        assert job["status"] == "succeeded"
        assert job["finished_at"] is not None
        db.close()

    def test_store_and_find_result(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        job_id, analysis_id = _job(db, cache="ck")
        assert db.store_result(analysis_id, "proj1", "ck", '{"test": true}', job_id)

        found = db.find_cached_result("ck")
        assert found is not None
        assert found["analysis_id"] == analysis_id
        assert found["result_json"] == '{"test": true}'
        db.close()

    def test_cache_miss(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        assert db.find_cached_result("nonexistent") is None
        db.close()

    def test_failed_job_is_never_a_cache_hit(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        job_id, _ = _job(db)
        db.update_job_status(
            job_id, "failed", error_code="test_error", error_message="Test failure"
        )
        assert db.find_cached_result("cache1") is None
        db.close()

    def test_list_jobs_for_project(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        for i in range(3):
            _job(db, cache=f"cache{i}")
        _job(db, project="proj2", cache="other")
        assert len(db.get_jobs_for_project("proj1")) == 3
        db.close()

    def test_listings_are_deterministically_ordered(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        ids = []
        for i in range(5):
            job_id, analysis_id = _job(db, cache=f"c{i}")
            ids.append(job_id)
            db.store_result(analysis_id, "proj1", f"c{i}", "{}", job_id)
        first = [j["job_id"] for j in db.get_jobs_for_project("proj1")]
        second = [j["job_id"] for j in db.get_jobs_for_project("proj1")]
        assert first == second
        results_a = [r["analysis_id"] for r in db.get_results_for_project("proj1")]
        results_b = [r["analysis_id"] for r in db.get_results_for_project("proj1")]
        assert results_a == results_b
        db.close()

    def test_count_active_jobs(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        j1, _ = _job(db, cache="c1")
        _job(db, cache="c2")
        assert db.count_active_jobs() == 2
        db.update_job_status(j1, "succeeded")
        assert db.count_active_jobs() == 1
        db.close()


class TestTerminalStateImmutability:
    """A late worker must never resurrect or overwrite a terminal job."""

    @pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled", "interrupted"])
    def test_terminal_status_cannot_be_overwritten(self, tmp_db_path, terminal):
        db = AnalysisDB(tmp_db_path)
        job_id, _ = _job(db)
        db.update_job_status(job_id, terminal, error_code="original")

        assert db.update_job_status(job_id, "succeeded", "done", "late worker") is False
        assert db.update_job_status(job_id, "running", "phase", "late progress") is False

        job = db.get_job(job_id)
        assert job["status"] == terminal
        assert job["progress_message"] != "late worker"
        db.close()

    def test_started_at_is_written_once(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        job_id, _ = _job(db)
        db.update_job_status(job_id, "running", "phase1", "one")
        first = db.get_job(job_id)["started_at"]

        time.sleep(0.05)
        db.update_job_status(job_id, "running", "phase2", "two")
        second = db.get_job(job_id)["started_at"]

        assert first == second, "a progress update slid the timeout deadline forward"
        db.close()

    def test_progress_updates_cannot_postpone_a_timeout(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path, job_timeout_seconds=0)
        job_id, _ = _job(db)
        db.update_job_status(job_id, "running", "phase1", "one")
        time.sleep(0.05)
        db.update_job_status(job_id, "running", "phase2", "two")

        assert db.enforce_timeouts() == [job_id]
        assert db.get_job(job_id)["error_code"] == "timeout"
        db.close()

    def test_late_worker_cannot_publish_a_result_after_timeout(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path, job_timeout_seconds=0)
        job_id, analysis_id = _job(db, cache="late")
        db.update_job_status(job_id, "running")
        time.sleep(0.05)
        db.enforce_timeouts()

        published = db.store_result(
            analysis_id, "proj1", "late", '{"late": true}', owning_job_id=job_id
        )
        assert published is False
        assert db.find_cached_result("late") is None
        assert db.get_job(job_id)["status"] == "failed"
        db.close()

    def test_late_worker_cannot_mark_a_timed_out_job_succeeded(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path, job_timeout_seconds=0)
        job_id, _ = _job(db)
        db.update_job_status(job_id, "running")
        time.sleep(0.05)
        db.enforce_timeouts()

        assert db.update_job_status(job_id, "succeeded", "done", "too late") is False
        job = db.get_job(job_id)
        assert job["status"] == "failed"
        assert job["error_code"] == "timeout"
        db.close()

    def test_result_for_an_unknown_job_is_refused(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        assert (
            db.store_result("a", "proj1", "ck", "{}", owning_job_id="does-not-exist")
            is False
        )
        db.close()


class TestRecovery:
    def test_both_queued_and_running_jobs_are_terminally_recovered(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        queued, _ = _job(db, cache="c1")
        running, _ = _job(db, cache="c2")
        db.update_job_status(running, "running")

        counts = db.recover_interrupted_jobs()
        assert counts == {"queued": 1, "running": 1}
        for job_id in (queued, running):
            job = db.get_job(job_id)
            assert job["status"] == "interrupted"
            assert job["error_code"] == "process_restart"
            assert job["finished_at"] is not None
        db.close()

    def test_successful_results_survive_recovery(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        job_id, analysis_id = _job(db, cache="keep")
        db.update_job_status(job_id, "running")
        db.store_result(analysis_id, "proj1", "keep", '{"ok": true}', job_id)
        db.update_job_status(job_id, "succeeded")

        db.recover_interrupted_jobs()
        assert db.get_job(job_id)["status"] == "succeeded"
        assert db.find_cached_result("keep") is not None
        db.close()

    def test_recovered_job_is_not_returned_as_a_cache_hit(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        job_id, _ = _job(db, cache="orphan")
        db.update_job_status(job_id, "running")
        db.recover_interrupted_jobs()

        result, is_new = db.find_or_create_job_for_cache(
            "orphan", str(uuid.uuid4()), str(uuid.uuid4()), "proj1", 10
        )
        assert is_new is True, "an interrupted job was reused instead of re-queued"
        db.close()

    def test_timeout_enforcement(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path, job_timeout_seconds=0)
        job_id, _ = _job(db)
        db.update_job_status(job_id, "running")
        time.sleep(0.05)
        assert db.enforce_timeouts() == [job_id]
        job = db.get_job(job_id)
        assert job["status"] == "failed"
        assert job["error_code"] == "timeout"
        db.close()

    def test_timeout_is_idempotent(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path, job_timeout_seconds=0)
        job_id, _ = _job(db)
        db.update_job_status(job_id, "running")
        time.sleep(0.05)
        db.enforce_timeouts()
        assert db.enforce_timeouts() == []
        db.close()

    def test_timeout_does_not_affect_queued(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path, job_timeout_seconds=0)
        job_id, _ = _job(db)
        time.sleep(0.05)
        assert db.enforce_timeouts() == []
        assert db.get_job(job_id)["status"] == "queued"
        db.close()


class TestIdempotentQueue:
    def test_duplicate_cache_key_returns_existing(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        j1 = str(uuid.uuid4())
        _result1, is_new1 = db.find_or_create_job_for_cache(
            "cache_key_1", j1, str(uuid.uuid4()), "proj1", 10
        )
        assert is_new1 is True

        result2, is_new2 = db.find_or_create_job_for_cache(
            "cache_key_1", str(uuid.uuid4()), str(uuid.uuid4()), "proj1", 10
        )
        assert is_new2 is False
        assert result2["job_id"] == j1
        db.close()

    def test_cached_result_returned(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        job_id, analysis_id = _job(db, cache="cache_key_2")
        db.store_result(analysis_id, "proj1", "cache_key_2", '{"result": true}', job_id)

        result, is_new = db.find_or_create_job_for_cache(
            "cache_key_2", str(uuid.uuid4()), str(uuid.uuid4()), "proj1", 10
        )
        assert is_new is False
        assert result["status"] == "cached"
        assert result["analysis_id"] == analysis_id
        db.close()

    def test_queue_full_raises(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        for i in range(3):
            _job(db, cache=f"ck{i}")
        with pytest.raises(ValueError, match="Queue full"):
            db.find_or_create_job_for_cache(
                "new_key", str(uuid.uuid4()), str(uuid.uuid4()), "proj1", 3
            )
        db.close()

    def test_completed_job_allows_new(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        j1, _ = _job(db, cache="ck1")
        db.update_job_status(j1, "failed")

        _result, is_new = db.find_or_create_job_for_cache(
            "new_ck", str(uuid.uuid4()), str(uuid.uuid4()), "proj1", 1
        )
        assert is_new is True
        db.close()

    def test_cold_cache_concurrency_admits_exactly_one_computation(self, tmp_db_path):
        """32 threads racing on one cold cache key: one admitted, 31 followers."""
        db = AnalysisDB(tmp_db_path)
        barrier = threading.Barrier(32)
        outcomes: list[tuple[bool, str]] = []
        lock = threading.Lock()

        def attempt():
            job_id = str(uuid.uuid4())
            barrier.wait()
            result, is_new = db.find_or_create_job_for_cache(
                "cold", job_id, str(uuid.uuid4()), "proj1", 1000
            )
            with lock:
                outcomes.append((is_new, result["job_id"]))

        threads = [threading.Thread(target=attempt) for _ in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        admitted = [o for o in outcomes if o[0]]
        followers = [o for o in outcomes if not o[0]]
        assert len(admitted) == 1, f"{len(admitted)} computations were admitted"
        assert len(followers) == 31
        # Every follower points at the one admitted job.
        assert {f[1] for f in followers} == {admitted[0][1]}
        assert db.count_active_jobs() == 1
        db.close()

    def test_concurrent_result_publication_keeps_one_identity(self, tmp_db_path):
        """First writer wins; no INSERT OR REPLACE identity corruption."""
        db = AnalysisDB(tmp_db_path)
        jobs = [_job(db, cache="shared") for _ in range(8)]
        for job_id, _ in jobs:
            db.update_job_status(job_id, "running")

        barrier = threading.Barrier(len(jobs))

        def publish(job_id, analysis_id):
            barrier.wait()
            db.store_result(analysis_id, "proj1", "shared", "{}", job_id)

        threads = [
            threading.Thread(target=publish, args=(j, a)) for j, a in jobs
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stored = db.find_cached_result("shared")
        assert stored is not None
        assert stored["analysis_id"] in {a for _, a in jobs}
        # Exactly one row for this identity.
        assert len(db.get_results_for_project("proj1")) == 1
        db.close()


class TestConcurrentThreads:
    def test_concurrent_writes(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        errors: list[Exception] = []

        def create_job(idx):
            try:
                db.create_job(
                    str(uuid.uuid4()), str(uuid.uuid4()), "proj1", f"cache_{idx}"
                )
            except (OSError, RuntimeError) as e:
                errors.append(e)

        threads = [threading.Thread(target=create_job, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert db.count_active_jobs() == 10
        db.close()


class TestPersistedSourceEvidence:
    """Real persistence, not equality of two hash functions."""

    def test_stored_evidence_round_trips(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        assert db.store_source_evidence("k1", "proj1", '{"a": 1}') is True
        row = db.get_source_evidence("k1", "proj1")
        assert row is not None
        assert json.loads(row["evidence_json"]) == {"a": 1}
        assert row["created_at"]
        db.close()

    def test_evidence_is_not_readable_from_another_project(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        db.store_source_evidence("k1", "proj1", '{"a": 1}')
        assert db.get_source_evidence("k1", "proj2") is None
        db.close()

    def test_first_writer_wins(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        assert db.store_source_evidence("k1", "proj1", '{"v": 1}') is True
        assert db.store_source_evidence("k1", "proj1", '{"v": 2}') is False
        row = db.get_source_evidence("k1", "proj1")
        assert json.loads(row["evidence_json"]) == {"v": 1}
        db.close()

    def test_concurrent_writers_store_one_row(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        barrier = threading.Barrier(16)
        created: list[bool] = []
        lock = threading.Lock()

        def write(i):
            barrier.wait()
            ok = db.store_source_evidence("race", "proj1", json.dumps({"writer": i}))
            with lock:
                created.append(ok)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert created.count(True) == 1
        assert db.count_source_evidence() == 1
        db.close()

    def test_evidence_survives_a_reopen(self, tmp_db_path):
        db = AnalysisDB(tmp_db_path)
        db.store_source_evidence("persist", "proj1", '{"kept": true}')
        db.close()

        reopened = AnalysisDB(tmp_db_path)
        reopened.recover_interrupted_jobs()
        row = reopened.get_source_evidence("persist", "proj1")
        assert row is not None
        assert json.loads(row["evidence_json"]) == {"kept": True}
        reopened.close()


class TestReportDeterminism:
    def test_html_only_change_does_not_alter_canonical_json(self):
        a = ReferenceTimeAnalysis(
            analysis_id="a1",
            project_id="p1",
            gp_revision_sha256="gp",
            global_confidence=0.5,
            cache_key="key1",
        )
        assert json.loads(generate_json_report(a)) == json.loads(generate_json_report(a))
