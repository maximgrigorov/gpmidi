from __future__ import annotations

import json
from collections import Counter

import pytest
from mido import Message, MidiTrack

import arrangement_workflow as workflow
from arrangement_workflow import (
    assert_preservation,
    is_solo_track,
    musical_identity,
    require_apply_approval,
)


def _track(*messages):
    track = MidiTrack()
    track.extend(messages)
    return track


def test_apply_requires_explicit_approval_flag():
    with pytest.raises(ValueError, match="--approved"):
        require_apply_approval(False)
    require_apply_approval(True)


def test_solo_detection_is_narrow():
    assert is_solo_track("Guitar Solo", "GUITAR")
    assert is_solo_track("Lead Guitar", "GUITAR")
    assert not is_solo_track("Rhythm Guitar", "GUITAR")
    assert not is_solo_track("Lead Synth", "OTHER")


def test_identity_ignores_velocity_and_timing_but_not_added_hits():
    base = _track(
        Message("note_on", note=36, velocity=95, time=0),
        Message("note_off", note=36, velocity=0, time=60),
    )
    moved = _track(
        Message("note_on", note=36, velocity=70, time=12),
        Message("note_off", note=36, velocity=0, time=60),
    )
    added = _track(*moved, Message("note_on", note=49, velocity=80, time=100))

    assert musical_identity(base) == musical_identity(moved) == Counter({("on", 36): 1, ("off", 36): 1})
    assert musical_identity(added) != musical_identity(base)


def test_preservation_requires_exact_drum_rhythm_and_allows_solo_microtiming():
    base = _track(
        Message("note_on", note=36, velocity=95, time=0),
        Message("note_off", note=36, velocity=0, time=60),
    )
    velocity_only = _track(
        Message("note_on", note=36, velocity=80, time=0),
        Message("note_off", note=36, velocity=0, time=60),
    )
    moved = _track(
        Message("note_on", note=36, velocity=80, time=12),
        Message("note_off", note=36, velocity=0, time=60),
    )

    assert_preservation(base, velocity_only, track_type="DRUMS", allow_timing=False)
    with pytest.raises(RuntimeError, match="timing"):
        assert_preservation(base, moved, track_type="DRUMS", allow_timing=False)
    assert_preservation(base, moved, track_type="GUITAR", allow_timing=True)


def test_apply_layer_locks_missing_or_unsafe_solo_velocity_profile():
    baseline = [{"track_name": "Guitar Solo", "track_type": "GUITAR"}]
    plan = {
        "global_velocity_shift": 12,
        "measure_energy": [{"measure": 1, "velocity_shift": 12, "accents": [1]}],
        "tracks": [],
        "solo_humanization": {"enabled": False},
    }

    workflow._lock_solo_velocity_profiles(plan, baseline)

    assert plan["tracks"] == [{
        "track": "Guitar Solo",
        "role": "lead",
        "velocity_shift": 0,
        "variance": 0,
        "velocity_processing": False,
    }]


def test_render_track_propagates_job_options_to_target_renderers(monkeypatch):
    calls = []
    monkeypatch.setattr(
        workflow, "build_instrument_midi",
        lambda *_a, **kw: calls.append(("instrument", kw)) or (MidiTrack(), {}),
    )
    monkeypatch.setattr(
        workflow, "build_drum_midi",
        lambda *_a, **kw: calls.append(("drums", kw)) or (MidiTrack(), {}),
    )
    monkeypatch.setattr(
        workflow, "build_other_midi",
        lambda *_a, **kw: calls.append(("other", kw)) or (MidiTrack(), {}),
    )
    options = {
        "humanize": True,
        "ghost_notes": True,
        "auto_sustain_vibrato": True,
        "fret_noise_on_hand_shift": True,
        "expand_gp_hidden_32nds": True,
        "preserve_gp_played_offsets": True,
    }
    source = type("Track", (), {"name": "Guitar Solo"})()

    workflow._render_track(
        object(), source, "GUITAR", solo_humanize=False, seed=19,
        render_options=options,
    )
    workflow._render_track(
        object(), source, "DRUMS", solo_humanize=False, seed=19,
        render_options=options,
    )
    workflow._render_track(
        object(), source, "OTHER", solo_humanize=False, seed=19,
        render_options=options,
    )

    assert calls == [
        ("instrument", {
            "humanize": True, "humanize_seed": 19,
            "auto_sustain_vibrato": True,
            "fret_noise_on_hand_shift": True,
            "performance_seed": 19,
            "expand_gp_hidden_32nds": True,
            "preserve_gp_played_offsets": True,
            "fret_hand_cost": False,
            "keep_gp_played_overlaps": False,
            "humanize_timing_over_gp_offsets": False,
            "lock_to_drums": False,
            "drum_timeline": None,
        }),
        ("drums", {"humanize": True, "humanize_seed": 19, "ghost_notes": True}),
        ("other", {"expand_gp_hidden_32nds": True}),
    ]


def test_apply_writes_factual_json_and_html_report_from_actual_midi(monkeypatch, tmp_path):
    source = tmp_path / "song.gp5"
    source.write_bytes(b"real-source-fixture")
    plan_path = tmp_path / "plan.json"
    plan = {
        "summary": "Поднять барабанный пульс без новых ударов.",
        "global_velocity_shift": 4,
        "measure_energy": [{"measure": 1, "velocity_shift": 0, "accents": []}],
        "tracks": [{
            "track": "Drums", "role": "rhythm", "velocity_shift": 0,
            "variance": 0, "velocity_processing": True, "minimum_velocity": 1,
        }],
        "solo_humanization": {"enabled": False},
    }
    plan_path.write_text(json.dumps({"plan": plan}), encoding="utf-8")
    baseline_track = _track(
        Message("note_on", note=36, velocity=95, time=0),
        Message("note_off", note=36, velocity=0, time=120),
    )
    song = type("Song", (), {"measureHeaders": [object()], "tempo": 120})()
    rendered = [{
        "index": 1,
        "track_name": "Drums",
        "track_type": "DRUMS",
        "basename": "Drums",
        "source_track": object(),
        "render_options": {"humanize": True, "ghost_notes": True},
        "midi_track": baseline_track,
        "stats": {"notes": 1},
    }]
    monkeypatch.setattr(workflow, "parse_song", lambda _path: song)
    monkeypatch.setattr(workflow, "_render_baseline", lambda *_a, **_kw: rendered)
    monkeypatch.setattr(workflow, "validate_plan", lambda *_a, **_kw: plan)
    monkeypatch.setattr(workflow, "measure_spans", lambda _song: {1: (0, 3840)})
    monkeypatch.setattr(workflow, "_smoke_errors", lambda *_a, **_kw: [])

    output = tmp_path / "output"
    manifest = workflow.apply(source, plan_path, output, approved=True, seed=7)

    report_meta = manifest["enrichment_report"]
    report_json = json.loads((output / report_meta["json_name"]).read_text(encoding="utf-8"))
    report_html = (output / report_meta["html_name"]).read_text(encoding="utf-8")
    assert report_json["tracks"][0]["new_drum_hits"] == 0
    assert report_json["tracks"][0]["totals"]["velocity_changed"] == 1
    assert report_json["tracks"][0]["baseline_processing"] == ["Humanize", "Ghost notes"]
    assert "Новых ударов в Enriched" in report_html
    assert {item["type"] for item in manifest["artifacts"]} >= {
        "ENRICHMENT_REPORT_JSON", "ENRICHMENT_REPORT_HTML",
    }
