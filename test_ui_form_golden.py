from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import guitarpro

FIXTURE = Path("tests/fixtures/wizard_fixture.gp5")
GOLDEN = Path("tests/fixtures/wizard_golden.json")


def _defaults(web, song, selected: set[int]) -> dict[str, object]:
    data: dict[str, object] = {"track_indices": [str(index) for index in sorted(selected)], "seed": "7"}
    for track in web.discover_tracks(song):
        if int(track["index"]) not in selected:
            continue
        for effect, enabled in (track.get("default_effects") or {}).items():
            if enabled:
                data[f"track_{track['index']}_{effect}"] = "on"
    return data


def _cases(web) -> dict[str, dict[str, object]]:
    song = guitarpro.parse(FIXTURE)
    all_tracks = {1, 2, 3, 4, 5}
    defaults = _defaults(web, song, all_tracks)

    all_humanize = dict(defaults)
    for track in web.discover_tracks(song):
        if "humanize" in track["available_effects"]:
            all_humanize[f"track_{track['index']}_humanize"] = "on"

    full_rhythm = dict(all_humanize)
    for track in web.discover_tracks(song):
        for effect in ("lock_to_drums", "double_track", "pick_direction", "palm_mute_motion"):
            if effect in track["available_effects"]:
                full_rhythm[f"track_{track['index']}_{effect}"] = "on"

    exception = dict(defaults)
    exception["track_3_humanize"] = "on"
    exception["track_3_auto_sustain_vibrato"] = "on"

    return {
        "defaults": defaults,
        "all_roles_humanized": all_humanize,
        "rhythm_options": full_rhythm,
        "per_track_exception": exception,
        "single_track": _defaults(web, song, {3}),
    }


def collect_hashes(root: Path) -> dict[str, dict[str, str]]:
    import app as web

    web.SESSIONS_ROOT = root
    web.app.config.update(TESTING=True, SECRET_KEY="wizard-golden-test")
    results: dict[str, dict[str, str]] = {}
    for case_name, form in _cases(web).items():
        client = web.app.test_client()
        upload = client.post(
            "/upload",
            data={"file": (FIXTURE.open("rb"), FIXTURE.name)},
            content_type="multipart/form-data",
        )
        assert upload.status_code == 302
        job_path = urlparse(upload.headers["Location"]).path
        process = client.post(f"{job_path}/process", data=form)
        assert process.status_code == 302
        page = client.get(job_path)
        assert page.status_code == 200

        session_dirs = sorted(root.iterdir(), key=lambda path: path.stat().st_mtime_ns)
        manifest = json.loads((session_dirs[-1] / "manifest.json").read_text(encoding="utf-8"))
        job = manifest["jobs"][0]
        names = {track["download_name"] for track in job["tracks"]}
        names.update(track["double_download_name"] for track in job["tracks"] if track.get("double_download_name"))
        if job.get("combined_name"):
            names.add(job["combined_name"])
        hashes: dict[str, str] = {}
        for name in sorted(names):
            response = client.get(f"/download/{job['id']}/{name}")
            if response.status_code == 200:
                payload = response.data
            else:
                # Baseline main did not whitelist the declared Double artifact;
                # locate it through the manifest-owned job directory, never a
                # hard-coded output filename.
                payload = (session_dirs[-1] / "uploads" / job["id"] / "output" / name).read_bytes()
            hashes[name] = hashlib.sha256(payload).hexdigest()
        results[case_name] = hashes
    return results


def test_ui_form_preserves_midi_bytes(tmp_path: Path):
    assert collect_hashes(tmp_path) == json.loads(GOLDEN.read_text(encoding="utf-8"))


if __name__ == "__main__":
    if sys.argv[1:] != ["--record-golden"]:
        raise SystemExit("usage: python test_ui_form_golden.py --record-golden")
    with tempfile.TemporaryDirectory(prefix="gpmidi-wizard-golden-") as temp_dir:
        payload = collect_hashes(Path(temp_dir))
    GOLDEN.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(GOLDEN)
