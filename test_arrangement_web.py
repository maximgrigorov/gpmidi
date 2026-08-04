from __future__ import annotations

from pathlib import Path


def test_selected_tracks_prepare_hermes_context_on_processing_step(monkeypatch):
    import app as web

    captured = {}
    job = {
        "id": "job-context",
        "stage": "parsed",
        "original_name": "song.gp5",
        "stored_name": "song.gp5",
        "detected_tracks": [{
            "index": 1, "track_type": "GUITAR",
            "available_effects": ["humanize"],
        }],
    }
    manifest = {"jobs": [job], "current_job_id": job["id"]}

    def fake_create_job(uploaded_file, **kwargs):
        captured.update(kwargs)
        return "job-context"

    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    monkeypatch.setattr(web, "create_job", fake_create_job)
    web.app.config.update(TESTING=True)
    response = web.app.test_client().post(
        "/jobs/job-context/process",
        data={
            "track_indices": "1",
            "prepare_arrangement_context": "on",
            "seed": "23",
        },
    )

    assert response.status_code == 302
    assert captured["prepare_arrangement_context"] is True
    assert captured["selected_track_indices"] == {1}
    assert captured["seed"] == 23
    assert "llm_arrangement" not in captured
    assert "solo_humanization" not in captured
    assert "rewrite_suggestions" not in captured


def test_template_exposes_draft_only_workflow_without_direct_llm_apply():
    source = Path("templates/index.html").read_text(encoding="utf-8")

    assert 'name="prepare_arrangement_context"' in source
    assert 'name="llm_arrangement"' not in source
    assert 'name="arrangement_expression"' not in source
    assert 'name="solo_humanization"' not in source
    assert 'name="rewrite_suggestions"' not in source
    assert "data-player-root" in source
    assert "preview-note-table" not in source
    assert "buildArticulationSummary" not in source
    assert "track.fix_categories" not in source
    assert "preview_articulations" not in source


def test_context_artifact_is_downloadable(monkeypatch, tmp_path):
    import app as web

    job_id = "context-job"
    output = tmp_path / job_id / "output"
    output.mkdir(parents=True)
    artifact = output / "song_arrangement-context.json"
    artifact.write_text("{}", encoding="utf-8")
    manifest = {
        "jobs": [{
            "id": job_id,
            "tracks": [],
            "arrangement_artifacts": [artifact.name],
            "combined_name": None,
            "refingered_name": None,
        }]
    }
    monkeypatch.setattr(web, "uploads_root", lambda: tmp_path)
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    web.app.config.update(TESTING=True)

    response = web.app.test_client().get(f"/download/{job_id}/{artifact.name}")

    assert response.status_code == 200
    assert response.data == b"{}"
