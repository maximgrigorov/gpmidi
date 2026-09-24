from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from werkzeug.datastructures import FileStorage


def test_processing_step_passes_openai_opt_in_and_health_reports_config(monkeypatch):
    import app as web
    captured = {}
    job = {
        "id": "job", "stage": "parsed", "original_name": "song.gp5",
        "stored_name": "song.gp5",
        "detected_tracks": [{"index": 1, "track_type": "GUITAR", "available_effects": []}],
    }
    manifest = {"jobs": [job], "current_job_id": "job"}
    monkeypatch.setenv("OPENAI_TOKEN", "test-only")
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    monkeypatch.setattr(web, "create_job", lambda _file, **kwargs: captured.update(kwargs) or "job")
    web.app.config.update(TESTING=True)
    client = web.app.test_client()
    response = client.post("/jobs/job/process", data={
        "track_indices": "1",
        "openai_arrangement_draft": "on", "seed": "17",
        "arrangement_prompt": "Custom expression prompt",
    })
    assert response.status_code == 302
    assert captured["openai_arrangement_draft"] is True
    assert captured["selected_track_indices"] == {1}
    assert captured["arrangement_prompt"] == "Custom expression prompt"
    assert captured["seed"] == 17
    assert client.get("/healthz").get_json()["direct_llm_enabled"] is True


def test_template_hides_openai_without_token_and_shows_with_token_for_parsed_job(monkeypatch):
    import app as web
    job = {
        "id": "job", "stage": "parsed", "original_name": "song.gp5",
        "created_at": "now",
        "song": {
            "title": "Song", "tempo": 120, "tracks": 1, "measures": 1,
            "time_signatures": ["4/4"], "artist": "—", "album": "—",
        },
        "detected_tracks": [{
            "index": 1, "track_name": "Solo", "track_type": "GUITAR",
            "track_type_label": "Гитара / Hydra", "instrument_preset": "solo",
            "available_effects": [],
        }],
    }
    manifest = {"jobs": [job], "current_job_id": "job"}
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    web.app.config.update(TESTING=True)
    monkeypatch.delenv("OPENAI_TOKEN", raising=False)
    assert b'openai_arrangement_draft' not in web.app.test_client().get("/").data
    monkeypatch.setenv("OPENAI_TOKEN", "test-only")
    page = web.app.test_client().get("/").data
    assert b'openai_arrangement_draft' in page
    assert b'name="arrangement_prompt"' in page
    assert "только выбранные дорожки" in page.decode()


def test_applied_job_shows_explicit_baseline_and_enriched_midi_downloads_without_zip(monkeypatch):
    import app as web

    job = {
        "id": "job", "stage": "processed", "original_name": "song.gp5",
        "created_at": "now", "combined_name": "song_ALL.mid",
        "combined_url": "/download/job/song_ALL.mid", "zip_url": "/jobs/job/download.zip",
        "song": {
            "title": "Song", "tempo": 120, "tracks": 1, "measures": 1,
            "time_signatures": ["4/4"], "artist": "—", "album": "—",
        },
        "tracks": [{
            "index": 1, "track_name": "Solo", "track_type": "GUITAR",
            "track_type_label": "Гитара / Hydra", "download_name": "Solo.mid",
            "download_url": "/download/job/Solo.mid", "preview_url": "/preview",
            "preview_instrument": "Hydra", "preview_duration_ms": 1000,
            "processing_report": [], "expression": {"changed_notes": 2,
                "mean_velocity_before": 95, "mean_velocity_after": 96,
                "timing_preserved": True},
        }],
        "warnings": [], "playable_warnings": [], "playable_tabs": False,
        "humanize": False, "ghost_notes": False, "auto_sustain_vibrato": False,
        "fret_noise_on_hand_shift": False, "expand_gp_hidden_32nds": False,
        "preserve_gp_played_offsets": False, "prepare_arrangement_context": True,
        "openai_arrangement_draft": True, "refingered_url": None,
        "arrangement_artifacts": ["Solo_expression.mid", "song_expression_ALL.mid"],
        "arrangement": {
            "status": "applied", "model": "gpt-5.6-sol", "summary": "Applied",
            "apply_manifest": {"artifacts": [
                {"name": "Solo_expression.mid", "track": "Solo", "type": "GUITAR"},
                {"name": "song_expression_ALL.mid", "type": "TYPE_1_ALL"},
            ]},
        },
    }
    manifest = {"jobs": [job], "current_job_id": "job"}
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    monkeypatch.setattr(web, "save_manifest", lambda _manifest: None)
    web.app.config.update(TESTING=True)

    for endpoint in ("/jobs/job", "/"):
        page = web.app.test_client().get(endpoint).data.decode()

        assert "Baseline · общий MIDI" in page
        assert "Enriched · общий MIDI" in page
        assert 'href="/download/job/song_ALL.mid"' in page
        assert 'href="/download/job/song_expression_ALL.mid"' in page
        assert "Baseline MIDI" in page
        assert "Enriched MIDI" in page
        assert 'href="/download/job/Solo.mid"' in page
        assert 'href="/download/job/Solo_expression.mid"' in page
        assert "ZIP по дорожкам" not in page
        assert "Context JSON" not in page
        assert "Expression plan" not in page
        assert "Usage" not in page


def test_successful_job_persists_exact_prompt_artifact_and_hash(monkeypatch, tmp_path):
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

    normalized = "Keep clear solo timing.\nShape velocity only."
    prompt_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    captured = {}

    def draft(_context, _config, *, instructions=None):
        captured["instructions"] = instructions
        return {
            "status": "draft_ready", "provider": "openai", "model": "gpt-5.6-sol",
            "response_id": "resp", "plan": {"summary": "ok"},
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                      "estimated_cost_usd": 0.01},
            "instructions": instructions,
            "instructions_sha256": prompt_hash,
        }

    monkeypatch.setattr(web, "prepare_hermes_arrangement_context", prepare)
    monkeypatch.setattr(web, "create_openai_draft", draft)
    upload = FileStorage(stream=io.BytesIO(b"fixture"), filename="song.gp5")
    with web.app.test_request_context("/"):
        job_id = web.create_job(
            upload, openai_arrangement_draft=True,
            arrangement_prompt="Keep clear solo timing.\r\nShape velocity only.",
        )

    job = saved["jobs"][0]
    prompt_name = job["arrangement"]["prompt_name"]
    prompt_path = job_root / job_id / "output" / prompt_name
    assert captured["instructions"] == normalized
    assert prompt_path.read_text() == normalized
    assert hashlib.sha256(prompt_path.read_bytes()).hexdigest() == prompt_hash
    assert job["arrangement"]["prompt_sha256"] == prompt_hash
    assert prompt_name in job["arrangement_artifacts"]


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
        job_id = web.create_job(
            upload, openai_arrangement_draft=True,
            arrangement_prompt="Failure-path prompt",
        )
    job = saved["jobs"][0]
    assert job["id"] == job_id
    assert job["arrangement"]["status"] == "draft_error"
    assert "secret" not in job["arrangement"]["error"]
    assert (job_root / job_id / "output" / "song_arrangement-context.json").exists()
    prompt_path = job_root / job_id / "output" / job["arrangement"]["prompt_name"]
    assert prompt_path.read_text() == "Failure-path prompt"
    assert job["arrangement"]["prompt_sha256"] == hashlib.sha256(
        prompt_path.read_bytes()
    ).hexdigest()


def test_provider_failure_exposes_only_safe_diagnostic_metadata(monkeypatch, tmp_path):
    import app as web
    from arrangement_openai import OpenAIDraftError

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
    failure = OpenAIDraftError(
        "OpenAI rejected the request (HTTP 400)",
        code="provider_rejected",
        provider_request_id="req_safe_123",
    )
    monkeypatch.setattr(
        web, "create_openai_draft", lambda *_a, **_k: (_ for _ in ()).throw(failure)
    )
    upload = FileStorage(stream=io.BytesIO(b"fixture"), filename="song.gp5")
    with web.app.test_request_context("/"):
        web.create_job(upload, openai_arrangement_draft=True)

    arrangement = saved["jobs"][0]["arrangement"]
    assert arrangement["error"] == "OpenAI rejected the request (HTTP 400)"
    assert arrangement["diagnostic_code"] == "provider_rejected"
    assert arrangement["provider_request_id"] == "req_safe_123"
    assert "secret" not in json.dumps(arrangement)


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
        "tracks": [{"index": 2}], "combined_name": "baseline.mid", "refingered_name": None,
        "humanize": True, "ghost_notes": True,
        "auto_sustain_vibrato": True, "fret_noise_on_hand_shift": True,
        "expand_gp_hidden_32nds": True, "preserve_gp_played_offsets": True,
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
    assert calls[0]["render_options"] == {
        "humanize": True, "ghost_notes": True,
        "auto_sustain_vibrato": True, "fret_noise_on_hand_shift": True,
        "expand_gp_hidden_32nds": True, "preserve_gp_played_offsets": True,
        "fret_hand_cost": False, "keep_gp_played_overlaps": False,
        "humanize_timing_over_gp_offsets": False, "lock_to_drums": False,
    }
    assert calls[0]["included_track_indices"] == {2}
    assert client.post(f"/jobs/{job_id}/arrangement/apply").status_code == 302
    assert len(calls) == 1


def test_applied_job_exposes_session_scoped_inline_enrichment_report(monkeypatch, tmp_path):
    import app as web

    job_id = "job-report"
    report_name = "song_enrichment-report.html"
    output = tmp_path / job_id / "output"
    output.mkdir(parents=True)
    output.joinpath(report_name).write_text(
        "<!doctype html><title>Фактический отчёт</title>", encoding="utf-8"
    )
    job = {
        "id": job_id,
        "stage": "processed",
        "original_name": "song.gp5",
        "created_at": "now",
        "song": {"title": "Song", "tempo": 120, "tracks": 0, "measures": 1},
        "tracks": [],
        "warnings": [],
        "playable_warnings": [],
        "arrangement_artifacts": [report_name],
        "arrangement": {
            "status": "applied",
            "apply_manifest": {
                "artifacts": [{"name": report_name, "type": "ENRICHMENT_REPORT_HTML"}],
                "enrichment_report": {"html_name": report_name},
            },
        },
    }
    manifest = {"jobs": [job], "current_job_id": job_id}
    monkeypatch.setattr(web, "uploads_root", lambda: tmp_path)
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    monkeypatch.setattr(web, "save_manifest", lambda _value: None)
    web.app.config.update(TESTING=True)
    client = web.app.test_client()

    page = client.get(f"/jobs/{job_id}").data.decode()
    expected_url = f"/reports/{job_id}/{report_name}"
    assert expected_url in page
    assert "Открыть фактический HTML-отчёт" in page

    response = client.get(expected_url)
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert "inline" in response.headers.get("Content-Disposition", "")
    assert "Фактический отчёт" in response.data.decode()


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
