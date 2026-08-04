"""Проводка opt-in performance-life флагов через CLI и web."""
from __future__ import annotations

from pathlib import Path

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


def test_processing_step_forwards_independent_per_track_performance_life_flags(monkeypatch):
    import app as web

    captured = {}
    job = {
        "id": "job123", "stage": "parsed", "original_name": "song.gp5",
        "stored_name": "song.gp5",
        "detected_tracks": [{
            "index": 1, "track_type": "GUITAR",
            "available_effects": [
                "auto_sustain_vibrato", "fret_noise_on_hand_shift",
            ],
        }],
    }
    monkeypatch.setattr(
        web, "load_manifest",
        lambda: {"jobs": [job], "current_job_id": "job123"},
    )

    def fake_create_job(uploaded_file, **kwargs):
        captured.update(kwargs)
        return "job123"

    monkeypatch.setattr(web, "create_job", fake_create_job)
    web.app.config.update(TESTING=True)
    client = web.app.test_client()

    response = client.post(
        "/jobs/job123/process",
        data={
            "track_indices": "1",
            "track_1_auto_sustain_vibrato": "on",
            "track_1_fret_noise_on_hand_shift": "on",
            "seed": "19",
        },
    )

    assert response.status_code == 302
    assert captured["track_options"][1]["auto_sustain_vibrato"] is True
    assert captured["track_options"][1]["fret_noise_on_hand_shift"] is True
    assert captured["seed"] == 19


def test_track_selection_template_exposes_separate_performance_life_checkboxes():
    source = Path("templates/index.html").read_text(encoding="utf-8")

    assert 'name="track_{{ track.index }}_auto_sustain_vibrato"' in source
    assert 'name="track_{{ track.index }}_fret_noise_on_hand_shift"' in source
