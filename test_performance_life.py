"""Проводка opt-in performance-life флагов через CLI и web."""
from __future__ import annotations

import io

import gp_to_shreddage as g


def test_cli_parses_independent_performance_life_flags():
    options = g.parse_cli_options([
        "gp_to_shreddage.py", "song.gp5",
        "--auto-sustain-vibrato", "--fret-noise-on-hand-shift", "--seed=19",
    ])

    assert options["source"] == "song.gp5"
    assert options["auto_sustain_vibrato"] is True
    assert options["fret_noise_on_hand_shift"] is True
    assert options["seed"] == 19


def test_web_upload_forwards_independent_performance_life_flags(monkeypatch, tmp_path):
    import app as web

    captured = {}

    def fake_create_job(uploaded_file, **kwargs):
        captured.update(kwargs)
        return "job123"

    monkeypatch.setattr(web, "create_job", fake_create_job)
    web.app.config.update(TESTING=True)
    client = web.app.test_client()

    response = client.post(
        "/upload",
        data={
            "file": (io.BytesIO(b"fixture"), "song.gp5"),
            "auto_sustain_vibrato": "on",
            "fret_noise_on_hand_shift": "on",
            "seed": "19",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert captured["auto_sustain_vibrato"] is True
    assert captured["fret_noise_on_hand_shift"] is True
    assert captured["seed"] == 19


def test_index_exposes_separate_performance_life_checkboxes(monkeypatch):
    import app as web

    monkeypatch.setattr(web, "load_manifest", lambda: {"jobs": [], "current_job_id": None})
    web.app.config.update(TESTING=True)
    response = web.app.test_client().get("/")

    assert response.status_code == 200
    assert b'name="auto_sustain_vibrato"' in response.data
    assert b'name="fret_noise_on_hand_shift"' in response.data
