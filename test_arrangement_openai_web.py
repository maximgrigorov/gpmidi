from __future__ import annotations

import io
import json
from pathlib import Path

from werkzeug.datastructures import FileStorage


def test_upload_passes_opt_in_and_health_reports_config(monkeypatch):
    import app as web
    captured = {}
    monkeypatch.setenv("OPENAI_TOKEN", "test-only")
    monkeypatch.setattr(web, "create_job", lambda _file, **kwargs: captured.update(kwargs) or "job")
    web.app.config.update(TESTING=True)
    client = web.app.test_client()
    response = client.post("/upload", data={
        "file": (io.BytesIO(b"gp"), "song.gp5"),
        "openai_arrangement_draft": "on", "seed": "17",
    }, content_type="multipart/form-data")
    assert response.status_code == 302
    assert captured["openai_arrangement_draft"] is True
    assert captured["seed"] == 17
    assert client.get("/healthz").get_json()["direct_llm_enabled"] is True


def test_template_hides_openai_without_token_and_shows_with_token(monkeypatch):
    import app as web
    monkeypatch.setattr(web, "load_manifest", lambda: {"jobs": [], "current_job_id": None})
    web.app.config.update(TESTING=True)
    monkeypatch.delenv("OPENAI_TOKEN", raising=False)
    assert b'openai_arrangement_draft' not in web.app.test_client().get("/").data
    monkeypatch.setenv("OPENAI_TOKEN", "test-only")
    assert b'openai_arrangement_draft' in web.app.test_client().get("/").data


def test_provider_failure_keeps_baseline_and_sanitizes(monkeypatch, tmp_path):
    import app as web
    job_root = tmp_path / "sessions"
    monkeypatch.setattr(web, "uploads_root", lambda: job_root)
    monkeypatch.setattr(web, "parse_song", lambda _path: object())
    monkeypatch.setattr(web, "summarize_song", lambda _song: {"title": "x"})
    monkeypatch.setattr(web, "build_track_summary", lambda *_args, **_kwargs: ([], []))
    monkeypatch.setattr(web, "build_combined_midi", lambda _tracks: None)
    monkeypatch.setattr(web, "load_manifest", lambda: {"jobs": []})
    saved = {}
    monkeypatch.setattr(web, "save_manifest", lambda value: saved.update(value))
    monkeypatch.setattr(web, "is_openai_configured", lambda: True)

    def prepare(_song, _tracks, _midi, output, _name):
        name = "song_arrangement-context.json"
        (output / name).write_text(json.dumps({"context": {"measure_count": 1, "tracks": []}}))
        return {"status": "awaiting_hermes_draft", "context_name": name}, [name]

    monkeypatch.setattr(web, "prepare_hermes_arrangement_context", prepare)
    monkeypatch.setattr(web, "create_openai_draft", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("secret test-only")))
    upload = FileStorage(stream=io.BytesIO(b"fixture"), filename="song.gp5")
    with web.app.test_request_context("/"):
        job_id = web.create_job(upload, openai_arrangement_draft=True)
    job = saved["jobs"][0]
    assert job["id"] == job_id
    assert job["arrangement"]["status"] == "draft_error"
    assert "secret" not in job["arrangement"]["error"]
    assert (job_root / job_id / "output" / "song_arrangement-context.json").exists()


def test_explicit_apply_is_separate_and_idempotent(monkeypatch, tmp_path):
    import app as web
    job_id = "job-openai"
    root = tmp_path / job_id
    (root / "input").mkdir(parents=True)
    (root / "output").mkdir()
    (root / "input" / "song.gp5").write_bytes(b"source")
    (root / "output" / "baseline.mid").write_bytes(b"baseline")
    plan_name = "song_openai-draft-plan.json"
    (root / "output" / plan_name).write_text(json.dumps({"plan": {}}))
    job = {
        "id": job_id, "stored_name": "song.gp5", "original_name": "song.gp5", "seed": 7,
        "tracks": [], "combined_name": "baseline.mid", "refingered_name": None,
        "arrangement_artifacts": [plan_name],
        "arrangement_apply_token": "apply-test-token",
        "arrangement": {"status": "draft_ready", "plan_name": plan_name},
    }
    manifest = {"jobs": [job], "current_job_id": job_id}
    monkeypatch.setattr(web, "uploads_root", lambda: tmp_path)
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    monkeypatch.setattr(web, "save_manifest", lambda _value: None)
    monkeypatch.setattr(web, "make_zip", lambda _dir: _dir / "tracks.zip")
    calls = []

    def fake_apply(_source, _plan, output, **kwargs):
        calls.append(kwargs)
        output.mkdir(parents=True)
        (output / "song_expression_ALL.mid").write_bytes(b"enriched")
        (output / "manifest.json").write_text("{}")
        return {"mode": "approved_apply"}

    monkeypatch.setattr(web, "apply_arrangement_plan", fake_apply)
    web.app.config.update(TESTING=True)
    client = web.app.test_client()
    assert client.post(f"/jobs/{job_id}/arrangement/apply", data={"apply_token": "wrong"}).status_code == 400
    assert len(calls) == 0
    assert client.post(
        f"/jobs/{job_id}/arrangement/apply", data={"apply_token": "apply-test-token"}
    ).status_code == 302
    assert (root / "output" / "baseline.mid").read_bytes() == b"baseline"
    assert (root / "output" / "song_expression_ALL.mid").read_bytes() == b"enriched"
    assert job["arrangement"]["status"] == "applied"
    assert job["arrangement_apply_token"] is None
    assert len(calls) == 1
    assert client.post(f"/jobs/{job_id}/arrangement/apply").status_code == 302
    assert len(calls) == 1


def test_kubernetes_uses_secret_reference_and_public_https_policy():
    deployment = Path("infra/ailab/apps/gpmidi-web/deployment.yaml").read_text()
    policy = Path("infra/ailab/apps/gpmidi-web/networkpolicy.yaml").read_text()
    assert "name: gpmidi-openai-secret" in deployment
    assert "key: OPENAI_TOKEN" in deployment
    assert "optional: true" in deployment
    assert "OPENAI_TOKEN:" not in deployment
    assert "cidr: 0.0.0.0/0" in policy
    assert "port: 443" in policy
    assert "192.168.0.0/16" in policy
