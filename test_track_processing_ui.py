from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from mido import MidiTrack


def _source_track(name: str, *, percussion: bool = False):
    return SimpleNamespace(name=name, isPercussionTrack=percussion)


def test_discovery_is_parse_only_and_defaults_every_track_to_selected(monkeypatch):
    import app as web

    song = SimpleNamespace(tracks=[
        _source_track("Drums", percussion=True),
        _source_track("Synth (Staff 1)"),
        _source_track("Guitar (Solo)"),
    ])
    types = iter(["DRUMS", "OTHER", "GUITAR"])
    monkeypatch.setattr(web, "resolve_track_type", lambda _track: next(types))

    discovered = web.discover_tracks(song)

    assert [row["index"] for row in discovered] == [1, 2, 3]
    assert [row["selected"] for row in discovered] == [True, True, True]
    assert discovered[1]["track_name"] == "Synth (Staff 1)"
    assert discovered[2]["available_effects"] == [
        "humanize", "auto_sustain_vibrato", "fret_noise_on_hand_shift",
        "fret_hand_cost", "expand_gp_hidden_32nds", "preserve_gp_played_offsets",
        "keep_gp_played_overlaps", "humanize_timing_over_gp_offsets",
    ]


def test_upload_stops_after_parse_before_any_midi_processing(monkeypatch):
    import app as web

    captured = {}
    monkeypatch.setattr(
        web, "create_parse_job",
        lambda uploaded_file: captured.update(filename=uploaded_file.filename) or "parsed-job",
    )
    monkeypatch.setattr(
        web, "create_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not process on upload")),
    )
    web.app.config.update(TESTING=True)

    response = web.app.test_client().post(
        "/upload",
        data={"file": (io.BytesIO(b"fixture"), "song.gp5")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/jobs/parsed-job")
    assert captured == {"filename": "song.gp5"}


def test_track_options_are_scoped_per_selected_track():
    import app as web

    form = {
        "track_1_humanize": "on",
        "track_1_ghost_notes": "on",
        "track_3_humanize": "on",
        "track_3_auto_sustain_vibrato": "on",
        "track_3_preserve_gp_played_offsets": "on",
        # This must be ignored because track 2 is not selected.
        "track_2_humanize": "on",
    }
    discovered = [
        {"index": 1, "track_type": "DRUMS", "available_effects": ["humanize", "ghost_notes"]},
        {"index": 2, "track_type": "OTHER", "available_effects": ["expand_gp_hidden_32nds"]},
        {"index": 3, "track_type": "GUITAR", "available_effects": [
            "humanize", "auto_sustain_vibrato", "fret_noise_on_hand_shift",
            "expand_gp_hidden_32nds", "preserve_gp_played_offsets",
        ]},
    ]

    options = web.parse_track_options(form, {1, 3}, discovered)

    assert set(options) == {1, 3}
    assert options[1] == {"humanize": True, "ghost_notes": True}
    assert options[3] == {
        "humanize": True,
        "auto_sustain_vibrato": True,
        "fret_noise_on_hand_shift": False,
        "expand_gp_hidden_32nds": False,
        "preserve_gp_played_offsets": True,
    }


def test_process_route_supports_one_selected_track_and_passes_per_track_effects(monkeypatch, tmp_path):
    import app as web

    job_id = "parsed-job"
    job_dir = tmp_path / job_id
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input" / "song.gp5").write_bytes(b"source")
    job = {
        "id": job_id,
        "stage": "parsed",
        "original_name": "song.gp5",
        "stored_name": "song.gp5",
        "detected_tracks": [
            {"index": 1, "track_name": "Synth", "track_type": "OTHER",
             "available_effects": ["expand_gp_hidden_32nds"]},
            {"index": 2, "track_name": "Guitar Solo", "track_type": "GUITAR",
             "available_effects": ["humanize", "auto_sustain_vibrato",
                                   "fret_noise_on_hand_shift", "expand_gp_hidden_32nds",
                                   "preserve_gp_played_offsets"]},
        ],
    }
    manifest = {"jobs": [job], "current_job_id": job_id}
    captured = {}
    monkeypatch.setattr(web, "uploads_root", lambda: tmp_path)
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    monkeypatch.setattr(web, "save_manifest", lambda _value: None)

    def fake_create_job(uploaded_file, **kwargs):
        captured["uploaded_file"] = uploaded_file
        captured.update(kwargs)
        return job_id

    monkeypatch.setattr(web, "create_job", fake_create_job)
    web.app.config.update(TESTING=True)

    response = web.app.test_client().post(
        f"/jobs/{job_id}/process",
        data={
            "track_indices": "2",
            "track_2_humanize": "on",
            "track_2_auto_sustain_vibrato": "on",
            "openai_arrangement_draft": "on",
            "arrangement_prompt": "Solo only",
            "seed": "23",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/jobs/{job_id}")
    assert captured["existing_job"]["id"] == job_id
    assert captured["selected_track_indices"] == {2}
    assert captured["track_options"] == {2: {
        "humanize": True,
        "auto_sustain_vibrato": True,
        "fret_noise_on_hand_shift": False,
        "expand_gp_hidden_32nds": False,
        "preserve_gp_played_offsets": False,
    }}
    assert captured["openai_arrangement_draft"] is True
    assert captured["arrangement_prompt"] == "Solo only"
    assert captured["seed"] == 23


def test_process_route_rejects_empty_selection(monkeypatch):
    import app as web

    job = {
        "id": "parsed-job", "stage": "parsed",
        "detected_tracks": [{"index": 1, "track_type": "OTHER", "available_effects": []}],
    }
    manifest = {"jobs": [job], "current_job_id": "parsed-job"}
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    web.app.config.update(TESTING=True)

    response = web.app.test_client().post("/jobs/parsed-job/process", data={})

    assert response.status_code == 400


def test_track_summary_only_renders_selected_tracks_with_their_own_options(monkeypatch, tmp_path):
    import app as web

    song = SimpleNamespace(tracks=[_source_track("Guitar Solo"), _source_track("Synth")])
    calls = []
    monkeypatch.setattr(web, "resolve_track_type", lambda track: "GUITAR" if "Guitar" in track.name else "OTHER")
    monkeypatch.setattr(
        web, "build_instrument_midi",
        lambda *_args, **kwargs: calls.append(("GUITAR", kwargs)) or (
            MidiTrack(), {"notes": 1, "ks": 2, "cc1": 3, "warnings": [], "config": "hydra"},
        ),
    )
    monkeypatch.setattr(
        web, "build_other_midi",
        lambda *_args, **kwargs: calls.append(("OTHER", kwargs)) or (
            MidiTrack(), {"notes": 1, "ks": 0, "cc1": 0, "warnings": []},
        ),
    )
    monkeypatch.setattr(
        web, "analyze_midi_preview",
        lambda *_args, **_kwargs: {
            "note_count": 1, "duration_ms": 10, "articulation_counts": {},
            "instrument_preset": "solo", "notes": [], "pitch_min": 60, "pitch_max": 60,
        },
    )
    monkeypatch.setattr(web, "is_empty_export_track", lambda *_args: False)

    with web.app.test_request_context("/"):
        tracks, midi_tracks = web.build_track_summary(
            song, tmp_path / "out", tmp_path / "job", "job",
            selected_track_indices={1},
            track_options={1: {
                "humanize": True,
                "auto_sustain_vibrato": True,
                "fret_noise_on_hand_shift": False,
                "expand_gp_hidden_32nds": False,
                "preserve_gp_played_offsets": True,
            }},
            seed=23,
        )

    assert len(tracks) == len(midi_tracks) == 1
    assert tracks[0]["track_name"] == "Guitar Solo"
    assert calls == [("GUITAR", {
        "humanize": True, "humanize_seed": 23,
        "auto_sustain_vibrato": True,
        "fret_noise_on_hand_shift": False,
        "performance_seed": 23,
        "expand_gp_hidden_32nds": False,
        "preserve_gp_played_offsets": True,
        "fret_hand_cost": False,
        "keep_gp_played_overlaps": False,
        "humanize_timing_over_gp_offsets": False,
    })]
    assert tracks[0]["effects"]["humanize"] is True
    assert "Hydra" in " ".join(tracks[0]["processing_report"])


def test_template_exposes_default_all_track_selection_per_track_effects_and_report():
    source = Path("templates/index.html").read_text(encoding="utf-8")

    assert 'name="track_indices"' in source
    assert "checked" in source.split('name="track_indices"', 1)[1].split(">", 1)[0]
    assert 'name="track_{{ track.index }}_humanize"' in source
    assert 'name="track_{{ track.index }}_auto_sustain_vibrato"' in source
    assert "processing_report" in source
    assert "Обработать выбранные" in source


def test_stored_upload_name_keeps_its_extension(tmp_path: Path):
    """secure_filename() strips non-ASCII, so "Песня.gp" collapsed to "gp".

    An extensionless stored name silently disabled GP7/GP8 detection (which
    gates on `path.suffix`) and playable-tab re-fingering — in a Russian UI the
    common case, not an edge case.
    """
    import app as web

    assert Path(web.unique_name(tmp_path, "Песня.gp")).suffix == ".gp"
    assert Path(web.unique_name(tmp_path, "Пе.gp5")).suffix == ".gp5"
    assert web.unique_name(tmp_path, "Song.gp5") == "Song.gp5"

    # Hostile names still collapse to a basename inside the target directory.
    for raw in ("../../etc/passwd.gp", "a/b/c.gp5", "..gp5"):
        name = web.unique_name(tmp_path, raw)
        assert Path(name).name == name
        assert (tmp_path / name).resolve().parent == tmp_path.resolve()


def test_unique_name_does_not_collide_on_repeat_uploads(tmp_path: Path):
    import app as web

    first = web.unique_name(tmp_path, "Песня.gp")
    (tmp_path / first).write_bytes(b"x")
    second = web.unique_name(tmp_path, "Песня.gp")
    assert second != first
    assert Path(second).suffix == ".gp"


def test_discovery_prechecks_hidden_32nds_everywhere_and_played_offsets_on_solo(monkeypatch):
    """Решение пользователя 2026-09-24 — те же дефолты, что у CLI."""
    import app as web

    song = SimpleNamespace(tracks=[
        _source_track("Guitar (Solo)"), _source_track("Guitar (Rhytm)"),
        _source_track("Synth (Staff 1)"),
    ])
    types = iter(["GUITAR", "GUITAR", "OTHER"])
    monkeypatch.setattr(web, "resolve_track_type", lambda _track: next(types))

    solo, rhythm, synth = web.discover_tracks(song)

    assert solo["default_effects"] == {"expand_gp_hidden_32nds": True, "preserve_gp_played_offsets": True}
    assert rhythm["default_effects"] == {"expand_gp_hidden_32nds": True, "preserve_gp_played_offsets": False}
    assert synth["default_effects"] == {"expand_gp_hidden_32nds": True}
    source = Path("templates/index.html").read_text(encoding="utf-8")
    assert "get('expand_gp_hidden_32nds') %} checked" in source
    assert "get('preserve_gp_played_offsets') %} checked" in source
