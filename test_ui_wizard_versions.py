from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

FIXTURE = Path("tests/fixtures/wizard_fixture.gp5")


def test_effective_options_remove_disabled_dependencies():
    import app as web

    options = {
        1: {"humanize": False, "ghost_notes": True},
        2: {
            "humanize": True,
            "preserve_gp_played_offsets": False,
            "lock_to_drums": True,
            "double_track": True,
            "palm_mute_motion": True,
            "fret_hand_cost": True,
            "humanize_timing_over_gp_offsets": True,
            "keep_gp_played_overlaps": True,
        },
    }

    assert web.effective_track_options(options) == {
        1: {"humanize": False, "ghost_notes": False},
        2: {
            "humanize": True,
            "preserve_gp_played_offsets": False,
            "lock_to_drums": True,
            "double_track": True,
            "palm_mute_motion": True,
            "fret_hand_cost": True,
            "humanize_timing_over_gp_offsets": False,
            "keep_gp_played_overlaps": False,
        },
    }


def test_legacy_processed_job_is_exposed_as_original_render():
    import app as web

    job = {
        "id": "legacy-job",
        "stage": "processed",
        "tracks": [{"download_name": "solo.mid"}],
        "combined_name": "song_ALL.mid",
        "seed": 7,
    }

    renders = web.ensure_job_renders(job)

    assert len(renders) == 1
    assert renders[0]["version"] == 1
    assert renders[0]["label"] == "исходная сборка"
    assert renders[0]["legacy"] is True
    assert renders[0]["tracks"][0]["download_name"] == "solo.mid"


@pytest.mark.parametrize("query", ["0", "-1", "2", "x", "1.5"])
def test_invalid_render_version_returns_404(monkeypatch, query):
    import app as web

    manifest = {
        "jobs": [{
            "id": "job",
            "stage": "processed",
            "original_name": "song.gp5",
            "song": {"tracks": 1},
            "detected_tracks": [],
            "renders": [{"version": 1, "status": "ok", "tracks": []}],
        }],
        "current_job_id": "job",
    }
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    monkeypatch.setattr(web, "save_manifest", lambda _manifest: None)
    web.app.config.update(TESTING=True)

    response = web.app.test_client().get(f"/jobs/job?step=result&v={query}")

    assert response.status_code == 404


def test_process_route_allows_another_render_and_saves_only_effective_options(monkeypatch):
    import app as web

    job = {
        "id": "job",
        "stage": "processed",
        "detected_tracks": [{
            "index": 1,
            "track_type": "GUITAR",
            "available_effects": ["humanize", "double_track", "keep_gp_played_overlaps"],
        }],
        "renders": [{"version": 1, "status": "ok", "tracks": []}],
    }
    manifest = {"jobs": [job], "current_job_id": "job"}
    captured = {}
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    monkeypatch.setattr(web, "save_manifest", lambda _manifest: None)
    monkeypatch.setattr(web, "create_job", lambda *_args, **kwargs: captured.update(kwargs) or "job")
    web.app.config.update(TESTING=True)

    response = web.app.test_client().post(
        "/jobs/job/process",
        data={
            "track_indices": "1",
            "track_1_double_track": "on",
            "track_1_keep_gp_played_overlaps": "on",
        },
    )

    assert response.status_code == 302
    assert captured["track_options"] == {
        1: {"humanize": False, "double_track": False, "keep_gp_played_overlaps": False}
    }


def test_versioned_artifact_paths_are_isolated(tmp_path: Path):
    import app as web

    job = {
        "id": "job",
        "renders": [
            {"version": 1, "artifact_names": ["same.mid"]},
            {"version": 2, "artifact_names": ["same.mid"]},
        ],
    }

    assert web.render_root(tmp_path / "job", web.render_by_version(job, 1)) == tmp_path / "job" / "renders" / "1"
    assert web.render_root(tmp_path / "job", web.render_by_version(job, 2)) == tmp_path / "job" / "renders" / "2"


def test_second_render_preserves_first_render_bytes_and_downloads(monkeypatch, tmp_path: Path):
    import app as web

    monkeypatch.setattr(web, "SESSIONS_ROOT", tmp_path)
    web.app.config.update(TESTING=True, SECRET_KEY="wizard-version-test")
    client = web.app.test_client()
    upload = client.post(
        "/upload",
        data={"file": (io.BytesIO(FIXTURE.read_bytes()), FIXTURE.name)},
        content_type="multipart/form-data",
    )
    job_path = upload.headers["Location"]
    defaults = {
        "track_indices": ["1", "2", "3", "4", "5"],
        "track_2_expand_gp_hidden_32nds": "on",
        "track_3_expand_gp_hidden_32nds": "on",
        "track_3_preserve_gp_played_offsets": "on",
        "track_4_expand_gp_hidden_32nds": "on",
        "track_5_expand_gp_hidden_32nds": "on",
        "seed": "7",
    }
    assert client.post(f"{job_path}/process", data=defaults).status_code == 302
    session_dir = next(tmp_path.iterdir())
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    first = manifest["jobs"][0]["renders"][0]
    first_name = first["combined_name"]
    first_path = session_dir / "uploads" / first["job_id"] / "renders" / "1" / "output" / first_name
    first_hash = hashlib.sha256(first_path.read_bytes()).hexdigest()

    changed = dict(defaults)
    changed["track_3_humanize"] = "on"
    assert client.post(f"{job_path}/process", data=changed).status_code == 302
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    job = manifest["jobs"][0]

    assert [render["version"] for render in job["renders"]] == [1, 2]
    assert hashlib.sha256(first_path.read_bytes()).hexdigest() == first_hash
    assert client.get(f"/download/{job['id']}/{first_name}?v=1").status_code == 200
    assert client.get(f"/download/{job['id']}/{job['renders'][1]['combined_name']}?v=2").status_code == 200


def test_legacy_download_url_still_reads_unmoved_output(monkeypatch, tmp_path: Path):
    import app as web

    job_id = "legacy"
    output = tmp_path / job_id / "output"
    output.mkdir(parents=True)
    output.joinpath("solo.mid").write_bytes(b"legacy-midi")
    manifest = {
        "jobs": [{
            "id": job_id, "stage": "processed", "tracks": [{"download_name": "solo.mid"}],
            "original_name": "old.gp5", "song": {"tracks": 1}, "detected_tracks": [],
        }],
        "current_job_id": job_id,
    }
    monkeypatch.setattr(web, "uploads_root", lambda: tmp_path)
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    web.app.config.update(TESTING=True)

    response = web.app.test_client().get(f"/download/{job_id}/solo.mid")

    assert response.status_code == 200
    assert response.data == b"legacy-midi"


def test_from_version_prefills_selected_tracks_and_effects(monkeypatch):
    import app as web

    job = {
        "id": "job", "stage": "processed", "original_name": "song.gp5",
        "song": {"tracks": 1, "measures": 1, "tempo": 120},
        "detected_tracks": [{
            "index": 1, "track_name": "Solo", "track_type": "GUITAR",
            "track_type_label": "Гитара", "available_effects": ["humanize", "double_track"],
            "default_effects": {}, "ui_role": "solo",
        }],
        "renders": [
            {"version": 1, "status": "ok", "tracks": [], "selected_track_indices": [1],
             "track_options": {"1": {"humanize": True, "double_track": True}}, "seed": 41},
            {"version": 2, "status": "ok", "tracks": [], "selected_track_indices": [1],
             "track_options": {"1": {"humanize": False, "double_track": False}}, "seed": 7},
        ],
    }
    manifest = {"jobs": [job], "current_job_id": "job"}
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    monkeypatch.setattr(web, "save_manifest", lambda _manifest: None)
    web.app.config.update(TESTING=True)

    response = web.app.test_client().get("/jobs/job?step=sound&from=1")
    page = response.data.decode()

    assert response.status_code == 200
    assert 'name="seed" value="41"' in page
    humanize = page.split('name="track_1_humanize"', 1)[1].split(">", 1)[0]
    double = page.split('name="track_1_double_track"', 1)[1].split(">", 1)[0]
    assert "checked" in humanize
    assert "checked" in double
    assert "disabled" not in double


def test_invalid_from_version_returns_404(monkeypatch):
    import app as web

    job = {
        "id": "job", "stage": "processed", "original_name": "song.gp5",
        "song": {"tracks": 0}, "detected_tracks": [],
        "renders": [{"version": 1, "status": "ok", "tracks": []}],
    }
    manifest = {"jobs": [job], "current_job_id": "job"}
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    monkeypatch.setattr(web, "save_manifest", lambda _manifest: None)
    web.app.config.update(TESTING=True)

    assert web.app.test_client().get("/jobs/job?step=sound&from=99").status_code == 404


def test_failed_build_is_recorded_as_render(monkeypatch):
    import app as web

    job = {
        "id": "job", "stage": "parsed", "original_name": "song.gp5",
        "detected_tracks": [{"index": 1, "track_type": "GUITAR", "available_effects": ["humanize"]}],
    }
    manifest = {"jobs": [job], "current_job_id": "job"}
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    monkeypatch.setattr(web, "save_manifest", lambda _manifest: None)
    monkeypatch.setattr(web, "create_job", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    web.app.config.update(TESTING=True)

    response = web.app.test_client().post(
        "/jobs/job/process", data={"track_indices": "1", "track_1_humanize": "on", "seed": "9"},
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/jobs/job?step=result&v=1")
    assert job["renders"][0]["status"] == "error"
    assert job["renders"][0]["track_options"] == {1: {"humanize": True}}


def test_legacy_links_keep_serving_legacy_files_after_a_new_version(monkeypatch, tmp_path: Path):
    """An old session that gets v2 must still serve its own files from version-less URLs.

    Old manifests store download/preview URLs without ``v``; the result page of the
    legacy version builds them the same way. Resolving them to the newest render
    handed out v2's bytes under v1's name.
    """
    import app as web

    job_id = "legacy"
    job_dir = tmp_path / job_id
    (job_dir / "output").mkdir(parents=True)
    (job_dir / "output" / "solo.mid").write_bytes(b"legacy-midi")
    (job_dir / "preview").mkdir()
    (job_dir / "preview" / "solo.json").write_text('{"v": "legacy"}', encoding="utf-8")
    (job_dir / "renders" / "2" / "output").mkdir(parents=True)
    (job_dir / "renders" / "2" / "output" / "solo.mid").write_bytes(b"v2-midi")
    (job_dir / "renders" / "2" / "preview").mkdir()
    (job_dir / "renders" / "2" / "preview" / "solo.json").write_text('{"v": "2"}', encoding="utf-8")
    manifest = {
        "jobs": [{
            "id": job_id, "stage": "processed", "tracks": [{"download_name": "solo.mid"}],
            "original_name": "old.gp5", "song": {"tracks": 1}, "detected_tracks": [],
        }],
        "current_job_id": job_id,
    }
    legacy = web.ensure_job_renders(manifest["jobs"][0])[0]
    manifest["jobs"][0]["renders"] = [legacy, {
        "version": 2, "label": "v2", "legacy": False, "relative_root": "renders/2",
        "status": "ok", "tracks": [{"download_name": "solo.mid"}],
    }]
    monkeypatch.setattr(web, "uploads_root", lambda: tmp_path)
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    web.app.config.update(TESTING=True)
    client = web.app.test_client()

    assert client.get(f"/download/{job_id}/solo.mid").data == b"legacy-midi"
    assert client.get(f"/download/{job_id}/solo.mid?v=1").data == b"legacy-midi"
    assert client.get(f"/download/{job_id}/solo.mid?v=2").data == b"v2-midi"
    assert client.get(f"/preview/{job_id}/solo.json").get_json() == {"v": "legacy"}
    assert client.get(f"/preview/{job_id}/solo.json?v=2").get_json() == {"v": "2"}


def _hrefs(page: str) -> list[str]:
    import html
    import re

    return [html.unescape(href) for href in re.findall(r'href="([^"]+)"', page)]


def test_older_version_page_links_its_own_combined_midi_and_zip(monkeypatch, tmp_path: Path):
    """Live NO-GO 2026-10-08: on v1's page after v2 the _ALL.mid and ZIP buttons
    pointed at ?v=2. They were job-level fields that only the newest build set;
    per-track links were right because they live inside each render."""
    import app as web

    monkeypatch.setattr(web, "SESSIONS_ROOT", tmp_path)
    web.app.config.update(TESTING=True, SECRET_KEY="wizard-version-links")
    client = web.app.test_client()
    job_path = client.post(
        "/upload",
        data={"file": (io.BytesIO(FIXTURE.read_bytes()), FIXTURE.name)},
        content_type="multipart/form-data",
    ).headers["Location"]
    form = {"track_indices": ["1", "2", "3", "4", "5"], "seed": "7"}
    assert client.post(f"{job_path}/process", data=form).status_code == 302
    session_dir = next(tmp_path.iterdir())
    job_id = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))["jobs"][0]["id"]
    v1_dir = session_dir / "uploads" / job_id / "renders" / "1"
    v1_all = next((v1_dir / "output").glob("*_ALL.mid")).read_bytes()
    v1_zip = (v1_dir / "tracks.zip").read_bytes()

    assert client.post(f"{job_path}/process", data={**form, "seed": "8", "track_1_humanize": "on"}).status_code == 302

    page = client.get(f"/jobs/{job_id}?step=result&v=1").get_data(as_text=True)
    hrefs = _hrefs(page)
    combined = [href for href in hrefs if href.endswith("_ALL.mid?v=1") or "_ALL.mid?v=" in href]
    zips = [href for href in hrefs if "/all.zip" in href]
    assert combined and all(href.endswith("?v=1") for href in combined), combined
    assert zips and all(href.endswith("?v=1") for href in zips), zips
    assert client.get(combined[0]).data == v1_all
    assert client.get(zips[0]).data == v1_zip

    page_v2 = client.get(f"/jobs/{job_id}?step=result&v=2").get_data(as_text=True)
    assert all(href.endswith("?v=2") for href in _hrefs(page_v2) if "_ALL.mid" in href or "/all.zip" in href)


def test_version_page_offers_its_refingered_gp(monkeypatch, tmp_path: Path):
    """The re-fingered GP download existed on the old page and was lost in the wizard."""
    import app as web

    job_id = "job"
    for version in ("1", "2"):
        out = tmp_path / job_id / "renders" / version / "output"
        out.mkdir(parents=True)
        out.joinpath("song_refingered.gp").write_bytes(f"gp-v{version}".encode())
    renders = [{
        "version": n, "label": f"v{n}", "legacy": False, "relative_root": f"renders/{n}",
        "status": "ok", "tracks": [], "refingered_name": "song_refingered.gp",
        "artifact_names": ["song_refingered.gp"], "selected_track_indices": [], "track_options": {},
    } for n in (1, 2)]
    manifest = {"jobs": [{
        "id": job_id, "stage": "processed", "original_name": "song.gp", "song": {"tracks": 1},
        "detected_tracks": [], "renders": renders, "refingered_name": "song_refingered.gp",
        "refingered_url": f"/download/{job_id}/song_refingered.gp?v=2",
    }], "current_job_id": job_id}
    monkeypatch.setattr(web, "uploads_root", lambda: tmp_path)
    monkeypatch.setattr(web, "load_manifest", lambda: manifest)
    monkeypatch.setattr(web, "save_manifest", lambda _manifest: None)
    web.app.config.update(TESTING=True)
    client = web.app.test_client()

    page = client.get(f"/jobs/{job_id}?step=result&v=1").get_data(as_text=True)
    gp_links = [href for href in _hrefs(page) if "song_refingered.gp" in href]
    assert gp_links == [f"/download/{job_id}/song_refingered.gp?v=1"]
    assert client.get(gp_links[0]).data == b"gp-v1"
