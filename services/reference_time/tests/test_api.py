"""FastAPI integration tests for the reference-time service.

Hermetic by construction: the real `AssetClient` is replaced with a fake that
serves generated fixtures from memory, so no test touches the network, a live
cluster, or SSH.

Two rules this module enforces on itself:

* no test converts a failure into `pytest.skip` — a job that does not reach
  `succeeded` is a test failure;
* no assertion is guarded by `if <thing_found>:`, because that silently passes
  when the thing is missing.
"""

from __future__ import annotations

import io
import json
import os
import struct
import time
import uuid
import zipfile

import mido
import pytest
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------
# Generated fixtures
# --------------------------------------------------------------------------

GP_SHA = "aa" * 32
GP2_SHA = "ab" * 32
MIDI_SHA = "11" * 32
MIDI2_SHA = "12" * 32
AUDIO_SHA = "55" * 32
STRUCT_SHA = "99" * 32

GP_LINK = "gp-link-1"
GP2_LINK = "gp-link-2"
MIDI_LINK = "midi-link-1"
MIDI2_LINK = "midi-link-2"
AUDIO_LINK = "audio-link-1"
STRUCT_LINK = "struct-link-1"
PROJECT_ID = "test-project-1"
OTHER_PROJECT_ID = "test-project-2"


def make_midi_bytes(tempo_us: int = 500_000, n_measures: int = 8) -> bytes:
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=tempo_us, time=0))
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    for _ in range(n_measures * 4):
        track.append(mido.Message("note_on", note=60, velocity=80, time=0))
        track.append(mido.Message("note_off", note=60, velocity=0, time=480))
    track.append(mido.MetaMessage("end_of_track", time=0))
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


def make_wav_bytes(sr: int = 22050, duration: float = 4.0, period: float = 2.0) -> bytes:
    """Clicks every `period` seconds so onsets land on 4/4 bar starts at 120 BPM."""
    n_samples = int(sr * duration)
    samples = bytearray(n_samples * 2)
    t = 0.0
    while t < duration:
        idx = int(t * sr) * 2
        for k in range(0, 64, 2):
            if idx + k + 1 < len(samples):
                struct.pack_into("<h", samples, idx + k, 30000 if k % 4 == 0 else -30000)
        t += period
    data = bytes(samples)
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data), b"WAVE",
        b"fmt ", 16, 1, 1, sr,
        sr * 2, 2, 16,
        b"data", len(data),
    )
    return hdr + data


def make_structure_json(sections=None, anchors=None, version="1.0") -> bytes:
    return json.dumps(
        {"version": version, "sections": sections or [], "anchors": anchors or []}
    ).encode()


def make_gp_bytes(
    n_measures: int = 8, marker_at: int | None = 0, repeat: bool = True
) -> bytes:
    """A real GP5 file built with pyguitarpro (no copyrighted content)."""
    import guitarpro

    song = guitarpro.Song()
    song.title = "acceptance-fixture"
    track = song.tracks[0]
    track.name = "GUITAR"

    while len(song.measureHeaders) < n_measures:
        song.addMeasureHeader(guitarpro.MeasureHeader())
    del song.measureHeaders[n_measures:]

    for idx, header in enumerate(song.measureHeaders):
        header.number = idx + 1
        header.timeSignature.numerator = 4
        header.timeSignature.denominator.value = 4
        if marker_at is not None and idx == marker_at:
            header.marker = guitarpro.Marker(title="Intro")
        if repeat and idx == 0:
            header.isRepeatOpen = True
        if repeat and idx == min(1, n_measures - 1):
            header.repeatClose = 2

    track.measures = []
    for header in song.measureHeaders:
        measure = guitarpro.Measure(track, header)
        voice = measure.voices[0]
        beat = guitarpro.Beat(voice)
        note = guitarpro.Note(beat)
        note.value = 5
        note.string = 6
        beat.notes.append(note)
        voice.beats.append(beat)
        track.measures.append(measure)

    buf = io.BytesIO()
    guitarpro.write(song, buf, version=(5, 1, 0))
    return buf.getvalue()


def make_gp8_bytes() -> bytes:
    """Minimal modern .gp ZIP/GPIF exercising the real service path."""
    gpif = b"""<GPIF>
<MasterTrack><Automations>
<Automation><Type>Tempo</Type><Bar>0</Bar><Position>0</Position><Value>90 2</Value></Automation>
<Automation><Type>SyncPoint</Type><Bar>0</Bar><Value><BarIndex>0</BarIndex></Value></Automation>
</Automations></MasterTrack>
<MasterBars>
<MasterBar><Time>4/4</Time><Section><Letter>A</Letter><Text>Intro</Text></Section><Bars>0</Bars></MasterBar>
<MasterBar><Time>3/4</Time><Bars>1</Bars></MasterBar>
</MasterBars>
<Bars>
<Bar id="0"><Voices>0 -1 -1 -1</Voices></Bar>
<Bar id="1"><Voices>-1 -1 -1 -1</Voices></Bar>
</Bars>
<Voices><Voice id="0"><Beats>0</Beats></Voice></Voices>
<Beats><Beat id="0"><Notes>0</Notes></Beat></Beats>
<Notes><Note id="0"/></Notes>
</GPIF>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Content/score.gpif", gpif)
    return buf.getvalue()


GP_BYTES = make_gp_bytes()
GP2_BYTES = make_gp_bytes(n_measures=6, marker_at=1)
MIDI_BYTES = make_midi_bytes()
MIDI2_BYTES = make_midi_bytes(tempo_us=600_000)
WAV_BYTES = make_wav_bytes()


# --------------------------------------------------------------------------
# Fake Asset API
# --------------------------------------------------------------------------

class FakeAssetClient:
    """In-memory stand-in for `assets.AssetClient`.

    Counts downloads per link so tests can prove that persisted source evidence
    is genuinely reused instead of silently recomputed.
    """

    def __init__(self):
        self.assets: dict[str, list[dict]] = {PROJECT_ID: [], OTHER_PROJECT_ID: []}
        self.revisions: dict[str, list[dict]] = {PROJECT_ID: [], OTHER_PROJECT_ID: []}
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.download_counts: dict[str, int] = {}
        self.fail_download_for: set[str] = set()

    # -- fixture wiring --
    def add(self, project_id, link_id, role, sha, filename, data, media_type=""):
        self.assets.setdefault(project_id, []).append(
            {
                "id": link_id,
                "role": role,
                "sha256": sha,
                "original_filename": filename,
                "media_type": media_type,
                "size_bytes": len(data),
            }
        )
        self.blobs[(project_id, link_id)] = data

    def add_gp_revision(self, project_id, revision, sha):
        self.revisions.setdefault(project_id, []).append(
            {"revision": revision, "sha256": sha}
        )

    # -- client interface --
    def list_assets(self, project_id):
        return list(self.assets.get(project_id, []))

    def list_gp_revisions(self, project_id):
        return list(self.revisions.get(project_id, []))

    def get_project(self, project_id):
        return {
            "id": project_id,
            "assets": self.list_assets(project_id),
            "gp_revisions": self.list_gp_revisions(project_id),
        }

    def download(self, project_id, link_id, dest_path, limits):
        from reference_time.assets import AssetFetchError, DownloadedAsset

        self.download_counts[link_id] = self.download_counts.get(link_id, 0) + 1
        if link_id in self.fail_download_for:
            raise AssetFetchError("asset_download_failed", f"injected failure {link_id}")
        data = self.blobs.get((project_id, link_id))
        if data is None:
            raise AssetFetchError("asset_download_status_error", "not found")
        if len(data) > limits.max_bytes:
            raise AssetFetchError("asset_too_large", "over limit")
        with open(dest_path, "wb") as fh:
            fh.write(data)
        return DownloadedAsset(path=dest_path, size_bytes=len(data))


def _populate(fake: FakeAssetClient) -> None:
    fake.add(PROJECT_ID, GP_LINK, "guitar-pro", GP_SHA, "song.gp5", GP_BYTES)
    fake.add(PROJECT_ID, GP2_LINK, "guitar-pro", GP2_SHA, "song-v2.gp5", GP2_BYTES)
    fake.add(PROJECT_ID, MIDI_LINK, "suno-midi.mix", MIDI_SHA, "a.mid", MIDI_BYTES)
    fake.add(PROJECT_ID, MIDI2_LINK, "suno-midi.drums", MIDI2_SHA, "b.mid", MIDI2_BYTES)
    fake.add(PROJECT_ID, AUDIO_LINK, "mix", AUDIO_SHA, "mix.wav", WAV_BYTES)
    fake.add(
        PROJECT_ID,
        STRUCT_LINK,
        "structure",
        STRUCT_SHA,
        "structure.json",
        make_structure_json(sections=[{"label": "Intro", "source_measure": 0, "gp_measure": 0}]),
    )
    fake.add_gp_revision(PROJECT_ID, 1, GP_SHA)
    fake.add_gp_revision(PROJECT_ID, 2, GP2_SHA)

    # A different project holding byte-identical assets.
    fake.add(OTHER_PROJECT_ID, "other-gp", "guitar-pro", GP_SHA, "song.gp5", GP_BYTES)
    fake.add(OTHER_PROJECT_ID, "other-mid", "suno-midi.mix", MIDI_SHA, "a.mid", MIDI_BYTES)
    fake.add_gp_revision(OTHER_PROJECT_ID, 1, GP_SHA)


@pytest.fixture
def fake_assets() -> FakeAssetClient:
    fake = FakeAssetClient()
    _populate(fake)
    return fake


@pytest.fixture
def client(tmp_path, fake_assets):
    os.environ["RT_DATA_ROOT"] = str(tmp_path / "data")
    os.environ["RT_TMP_ROOT"] = str(tmp_path / "tmp")
    os.environ["ASSET_API_URL"] = "http://fake-asset-api:8000"

    from importlib import reload

    import reference_time.config
    reload(reference_time.config)
    import reference_time.api as api_mod
    reload(api_mod)

    api_mod.asset_client = fake_assets
    with TestClient(api_mod.app) as c:
        c.api_mod = api_mod
        c.fake = fake_assets
        yield c


def create(client, **overrides) -> dict:
    body = {
        "gp_asset_link_id": GP_LINK,
        "source_midi_link_ids": [MIDI_LINK],
    }
    body.update(overrides)
    project = body.pop("project_id", PROJECT_ID)
    resp = client.post(f"/v1/projects/{project}/analyses", json=body)
    return {"status_code": resp.status_code, "body": resp.json()}


def run_to_success(client, **overrides) -> dict:
    """Create an analysis and assert it reaches `succeeded`. Returns the report."""
    created = create(client, **overrides)
    assert created["status_code"] == 200, created["body"]
    data = created["body"]
    project = overrides.get("project_id", PROJECT_ID)
    job = await_terminal(client, data["job_id"], data["status"])
    assert job["status"] == "succeeded", job
    report = client.get(
        f"/v1/projects/{project}/analyses/{data['analysis_id']}/report.json"
    )
    assert report.status_code == 200, report.text
    return {"created": data, "job": job, "report": report.json()}


def await_terminal(client, job_id: str, initial_status: str = "", timeout: float = 30.0) -> dict:
    if initial_status == "succeeded":
        return {"status": "succeeded", "job_id": job_id, "cache_hit": True}
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        resp = client.get(f"/v1/jobs/{job_id}")
        if resp.status_code == 200:
            last = resp.json()
            if last["status"] in ("succeeded", "failed", "interrupted", "cancelled"):
                return last
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach a terminal state: {last}")


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

class TestHealthEndpoints:
    def test_healthz(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_readyz(self, client):
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"


# --------------------------------------------------------------------------
# Validation — fail closed, no job or cache entry created
# --------------------------------------------------------------------------

class TestValidation:
    @pytest.mark.parametrize(
        ("overrides", "code"),
        [
            ({"source_midi_link_ids": []}, "no_source_midi"),
            ({"source_midi_link_ids": ["nope"]}, "asset_not_in_project"),
            ({"gp_asset_link_id": MIDI_LINK}, "asset_role_invalid"),
            ({"source_midi_link_ids": [AUDIO_LINK]}, "asset_role_invalid"),
            ({"audio_link_ids": [MIDI_LINK]}, "asset_role_invalid"),
            ({"structure_link_id": AUDIO_LINK}, "asset_role_invalid"),
            ({"gp_revision_sha256": "ff" * 32}, "gp_revision_mismatch"),
        ],
    )
    def test_invalid_request_is_rejected_with_stable_code(self, client, overrides, code):
        created = create(client, **overrides)
        assert created["status_code"] == 422, created
        detail = created["body"]["detail"]
        assert detail["code"] == code
        assert detail["request_id"]

    def test_validation_failure_creates_no_job_and_no_result(self, client):
        create(client, source_midi_link_ids=["nope"])
        listing = client.get(f"/v1/projects/{PROJECT_ID}/analyses").json()
        assert listing["jobs"] == []
        assert listing["analyses"] == []

    def test_matching_claimed_sha_is_accepted(self, client):
        created = create(client, gp_revision_sha256=GP_SHA)
        assert created["status_code"] == 200, created["body"]

    def test_asset_api_failure_is_reported_as_bad_gateway(self, client):
        from reference_time.assets import AssetFetchError

        def boom(_project_id):
            raise AssetFetchError("asset_api_unreachable", "down")

        client.fake.list_assets = boom
        created = create(client)
        assert created["status_code"] == 502
        assert created["body"]["detail"]["code"] == "asset_api_unreachable"


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

class TestAnalysisLifecycle:
    def test_full_run_produces_a_grid_and_mappings(self, client):
        out = run_to_success(client)
        report = out["report"]
        assert report["project_id"] == PROJECT_ID
        assert report["gp_revision_sha256"] == GP_SHA
        assert report["gp_revision_number"] == 1
        assert len(report["gp_measures"]) == 8
        assert len(report["source_measures"]) == 8
        assert report["mappings"]
        assert report["global_confidence"] > 0
        assert out["job"]["progress_phase"] == "done"

    def test_full_run_accepts_modern_gp8_zip_gpif_without_proxy(self, client):
        client.fake.blobs[(PROJECT_ID, GP_LINK)] = make_gp8_bytes()
        gp_asset = next(
            asset for asset in client.fake.assets[PROJECT_ID] if asset["id"] == GP_LINK
        )
        gp_asset["original_filename"] = "song.gp"
        gp_asset["size_bytes"] = len(client.fake.blobs[(PROJECT_ID, GP_LINK)])

        report = run_to_success(client)["report"]

        assert report["schema_version"] == "2.1.0"
        assert len(report["gp_measures"]) == 2
        first, second = report["gp_measures"]
        assert (first["numerator"], first["denominator"]) == (4, 4)
        assert first["marker_text"] == "A — Intro"
        assert first["tempo_bpm"] == 90.0
        assert any(w["code"] == "gp_audio_sync_points" for w in first["warnings"])
        assert (second["numerator"], second["denominator"]) == (3, 4)
        assert second["is_empty"] is True

    def test_gp_grid_carries_marker_and_repeat_metadata(self, client):
        report = run_to_success(client)["report"]
        assert any(m["marker_text"] for m in report["gp_measures"])
        assert any(m["has_repeat_open"] for m in report["gp_measures"])
        assert any(m["has_repeat_close"] for m in report["gp_measures"])

    def test_html_report_renders(self, client):
        out = run_to_success(client)
        html = client.get(
            f"/v1/projects/{PROJECT_ID}/analyses/"
            f"{out['created']['analysis_id']}/report.html"
        )
        assert html.status_code == 200
        assert "Reference-Time Analysis Report" in html.text
        assert "Provenance" in html.text

    def test_analysis_summary_endpoint(self, client):
        out = run_to_success(client)
        summary = client.get(
            f"/v1/projects/{PROJECT_ID}/analyses/{out['created']['analysis_id']}"
        ).json()
        assert summary["source_measure_count"] == 8
        assert summary["gp_measure_count"] == 8
        assert summary["provenance"]["source_evidence_key"]

    def test_job_not_found(self, client):
        resp = client.get(f"/v1/jobs/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "job_not_found"

    def test_report_not_found(self, client):
        resp = client.get(
            f"/v1/projects/{PROJECT_ID}/analyses/{uuid.uuid4()}/report.json"
        )
        assert resp.status_code == 404

    def test_analysis_of_other_project_is_not_readable(self, client):
        out = run_to_success(client)
        resp = client.get(
            f"/v1/projects/{OTHER_PROJECT_ID}/analyses/"
            f"{out['created']['analysis_id']}/report.json"
        )
        assert resp.status_code == 404


# --------------------------------------------------------------------------
# Cache identity and project isolation
# --------------------------------------------------------------------------

class TestCacheIdentity:
    def test_repeat_request_is_an_idempotent_cache_hit(self, client):
        first = run_to_success(client)
        second = create(client)
        assert second["status_code"] == 200
        assert second["body"]["cache_hit"] is True
        assert second["body"]["status"] == "succeeded"
        assert second["body"]["analysis_id"] == first["created"]["analysis_id"]

    def test_identical_assets_in_another_project_do_not_collide(self, client):
        first = run_to_success(client)
        second = create(
            client,
            project_id=OTHER_PROJECT_ID,
            gp_asset_link_id="other-gp",
            source_midi_link_ids=["other-mid"],
        )
        assert second["status_code"] == 200
        assert second["body"]["cache_hit"] is False
        assert second["body"]["analysis_id"] != first["created"]["analysis_id"]

    def test_changed_source_midi_produces_a_new_analysis(self, client):
        first = run_to_success(client)
        second = run_to_success(client, source_midi_link_ids=[MIDI2_LINK])
        assert second["created"]["analysis_id"] != first["created"]["analysis_id"]
        assert second["report"]["cache_key"] != first["report"]["cache_key"]

    def test_adding_audio_produces_a_new_analysis(self, client):
        first = run_to_success(client)
        second = run_to_success(client, audio_link_ids=[AUDIO_LINK])
        assert second["report"]["cache_key"] != first["report"]["cache_key"]

    def test_adding_structure_produces_a_new_analysis(self, client):
        first = run_to_success(client)
        second = run_to_success(client, structure_link_id=STRUCT_LINK)
        assert second["report"]["cache_key"] != first["report"]["cache_key"]


# --------------------------------------------------------------------------
# Persisted source evidence reuse (audit item H)
# --------------------------------------------------------------------------

class TestSourceEvidenceReuse:
    def test_gp_only_change_reuses_source_evidence_and_recomputes_gp(self, client):
        first = run_to_success(client)
        assert first["report"]["provenance"]["source_evidence_reused"] is False
        assert first["report"]["provenance"]["source_midi_extractions"] == 1
        assert first["report"]["provenance"]["gp_extractions"] == 1
        midi_downloads_after_first = client.fake.download_counts[MIDI_LINK]

        second = run_to_success(client, gp_asset_link_id=GP2_LINK)
        prov = second["report"]["provenance"]

        # Reused: the source MIDI was not downloaded or parsed again...
        assert prov["source_evidence_reused"] is True
        assert prov["source_midi_extractions"] == 0
        assert client.fake.download_counts[MIDI_LINK] == midi_downloads_after_first
        # ...while the GP side was genuinely recomputed.
        assert prov["gp_extractions"] == 1
        assert client.fake.download_counts[GP2_LINK] == 1
        assert len(second["report"]["gp_measures"]) == 6
        assert second["report"]["cache_key"] != first["report"]["cache_key"]
        assert (
            prov["source_evidence_key"]
            == first["report"]["provenance"]["source_evidence_key"]
        )

    def test_changed_midi_invalidates_persisted_source_evidence(self, client):
        first = run_to_success(client)
        second = run_to_success(client, source_midi_link_ids=[MIDI2_LINK])
        assert second["report"]["provenance"]["source_evidence_reused"] is False
        assert second["report"]["provenance"]["source_midi_extractions"] == 1
        assert (
            second["report"]["provenance"]["source_evidence_key"]
            != first["report"]["provenance"]["source_evidence_key"]
        )

    def test_changed_audio_invalidates_persisted_source_evidence(self, client):
        without = run_to_success(client)
        with_audio = run_to_success(client, audio_link_ids=[AUDIO_LINK])
        assert (
            with_audio["report"]["provenance"]["source_evidence_key"]
            != without["report"]["provenance"]["source_evidence_key"]
        )
        assert with_audio["report"]["provenance"]["audio_extractions"] == 1

    def test_persisted_source_evidence_is_project_scoped(self, client):
        run_to_success(client)
        other = create(
            client,
            project_id=OTHER_PROJECT_ID,
            gp_asset_link_id="other-gp",
            source_midi_link_ids=["other-mid"],
        )
        job = await_terminal(client, other["body"]["job_id"], other["body"]["status"])
        assert job["status"] == "succeeded"
        report = client.get(
            f"/v1/projects/{OTHER_PROJECT_ID}/analyses/"
            f"{other['body']['analysis_id']}/report.json"
        ).json()
        # Byte-identical MIDI in a different project must not read project A's row.
        assert report["provenance"]["source_evidence_reused"] is False
        assert report["provenance"]["source_midi_extractions"] == 1


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------

class TestAudio:
    def test_missing_audio_warns_and_still_succeeds(self, client):
        report = run_to_success(client)["report"]
        codes = [w["code"] for w in report["global_warnings"]]
        assert "audio_missing" in codes
        assert report["audio_evidence"] == []

    def test_audio_is_serialized_with_exact_identity(self, client):
        report = run_to_success(client, audio_link_ids=[AUDIO_LINK])["report"]
        codes = [w["code"] for w in report["global_warnings"]]
        assert "audio_missing" not in codes
        assert len(report["audio_evidence"]) == 1
        ev = report["audio_evidence"][0]
        assert ev["asset_link_id"] == AUDIO_LINK
        assert ev["sha256"] == AUDIO_SHA
        assert ev["role"] == "mix"
        assert ev["sample_rate"] == 22050
        assert ev["onset_count"] > 0

    def test_audio_corroboration_reaches_the_mapping_evidence(self, client):
        report = run_to_success(client, audio_link_ids=[AUDIO_LINK])["report"]
        measures_with_audio = [
            m for m in report["source_measures"] if m["audio_downbeat_evidence"]
        ]
        assert measures_with_audio, "audio produced no downbeat corroboration"
        assert any(
            any(e.startswith("audio_corroboration") for e in m["evidence"])
            for m in report["mappings"]
        )

    def test_audio_download_failure_degrades_instead_of_failing(self, client):
        client.fake.fail_download_for.add(AUDIO_LINK)
        report = run_to_success(client, audio_link_ids=[AUDIO_LINK])["report"]
        codes = [w["code"] for w in report["global_warnings"]]
        assert "audio_decode_failed" in codes
        assert report["audio_evidence"][0]["duration_seconds"] == 0.0

    def test_undecodable_audio_degrades_instead_of_failing(self, client):
        client.fake.blobs[(PROJECT_ID, AUDIO_LINK)] = b"RIFF....WAVEnot-really-audio"
        report = run_to_success(client, audio_link_ids=[AUDIO_LINK])["report"]
        codes = [w["code"] for w in report["global_warnings"]]
        assert "audio_decode_failed" in codes


# --------------------------------------------------------------------------
# Structure / anchors
# --------------------------------------------------------------------------

class TestStructure:
    def _put_structure(self, client, payload: bytes) -> None:
        client.fake.blobs[(PROJECT_ID, STRUCT_LINK)] = payload

    def test_valid_anchor_locks_the_mapping_not_just_the_cache_key(self, client):
        free = run_to_success(client)["report"]
        self._put_structure(
            client, make_structure_json(anchors=[{"source_measure": 1, "gp_measure": 5}])
        )
        anchored = run_to_success(client, structure_link_id=STRUCT_LINK)["report"]

        by_src_free = {m["source_measure_index"]: m for m in free["mappings"]}
        by_src_anchored = {m["source_measure_index"]: m for m in anchored["mappings"]}
        assert by_src_anchored[1]["gp_measure_index"] == 5
        assert "anchored" in by_src_anchored[1]["reason_codes"]
        assert by_src_free[1]["gp_measure_index"] != 5
        assert anchored["anchored_source_indices"] == [1]

    def test_sections_appear_in_json_and_html(self, client):
        self._put_structure(
            client,
            make_structure_json(
                sections=[{"label": "Intro", "source_measure": 0, "gp_measure": 0}]
            ),
        )
        out = run_to_success(client, structure_link_id=STRUCT_LINK)
        assert out["report"]["structure_version"] == "1.0"
        assert out["report"]["structure_sections"][0]["label"] == "Intro"
        html = client.get(
            f"/v1/projects/{PROJECT_ID}/analyses/"
            f"{out['created']['analysis_id']}/report.html"
        ).text
        assert "Structure and Markers" in html
        assert "Intro" in html

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            (b"{oops", "structure_malformed_json"),
            (b"[1,2,3]", "structure_root_not_object"),
            (json.dumps({"version": "7.7"}).encode(), "structure_unsupported_version"),
            (
                json.dumps(
                    {
                        "version": "1.0",
                        "anchors": [
                            {"source_measure": 1, "gp_measure": 4},
                            {"source_measure": 3, "gp_measure": 2},
                        ],
                    }
                ).encode(),
                "structure_non_monotonic_anchors",
            ),
            (
                json.dumps(
                    {
                        "version": "1.0",
                        "anchors": [
                            {"source_measure": 1, "gp_measure": 1},
                            {"source_measure": 1, "gp_measure": 2},
                        ],
                    }
                ).encode(),
                "structure_conflicting_anchor",
            ),
            (
                json.dumps(
                    {"version": "1.0", "anchors": [{"source_measure": 99, "gp_measure": 1}]}
                ).encode(),
                "structure_source_index_out_of_range",
            ),
        ],
    )
    def test_invalid_structure_fails_the_job_with_a_stable_code(
        self, client, payload, code
    ):
        self._put_structure(client, payload)
        created = create(client, structure_link_id=STRUCT_LINK)
        assert created["status_code"] == 200
        job = await_terminal(
            client, created["body"]["job_id"], created["body"]["status"]
        )
        assert job["status"] == "failed"
        assert job["error_code"] == code

    def test_timestamp_anchor_is_converted(self, client):
        self._put_structure(
            client,
            make_structure_json(anchors=[{"source_seconds": 5.0, "gp_measure": 4}]),
        )
        report = run_to_success(client, structure_link_id=STRUCT_LINK)["report"]
        # 120 BPM 4/4 → measure 2 spans [4.0, 6.0)
        assert report["anchored_source_indices"] == [2]


# --------------------------------------------------------------------------
# Consensus
# --------------------------------------------------------------------------

class TestConsensus:
    def test_two_agreeing_midis_produce_deterministic_consensus(self, client):
        client.fake.add(
            PROJECT_ID, "midi-copy", "suno-midi.bass", "13" * 32, "c.mid", MIDI_BYTES
        )
        a = run_to_success(client, source_midi_link_ids=[MIDI_LINK, "midi-copy"])
        assert a["report"]["midi_consensus"]["decision"] == "agreed"
        assert a["report"]["midi_consensus"]["source_count"] == 2

    def test_conflicting_midi_produces_regional_conflict_and_lower_confidence(
        self, client
    ):
        agreeing = run_to_success(client)["report"]
        conflicting = run_to_success(
            client, source_midi_link_ids=[MIDI_LINK, MIDI2_LINK]
        )["report"]

        consensus = conflicting["midi_consensus"]
        assert consensus["decision"] == "conflict"
        assert consensus["conflict_regions"]
        assert conflicting["global_confidence"] < agreeing["global_confidence"]
        codes = [w["code"] for w in conflicting["global_warnings"]]
        assert "tempo_map_conflict" in codes
        assert any(
            "consensus_conflict_region" in m["reason_codes"]
            for m in conflicting["mappings"]
        )

    def test_consensus_selection_is_order_independent(self, client):
        forward = run_to_success(client, source_midi_link_ids=[MIDI_LINK, MIDI2_LINK])
        reverse = create(client, source_midi_link_ids=[MIDI2_LINK, MIDI_LINK])
        assert reverse["body"]["cache_hit"] is True
        assert reverse["body"]["analysis_id"] == forward["created"]["analysis_id"]


# --------------------------------------------------------------------------
# Gaps
# --------------------------------------------------------------------------

class TestGaps:
    def test_more_source_than_gp_measures_yields_source_gaps(self, client):
        # 8 source measures, 2 GP measures
        client.fake.add(
            PROJECT_ID, "gp-tiny", "guitar-pro", "cd" * 32, "tiny.gp5",
            make_gp_bytes(n_measures=2, marker_at=None, repeat=False),
        )
        client.fake.add_gp_revision(PROJECT_ID, 3, "cd" * 32)
        report = run_to_success(client, gp_asset_link_id="gp-tiny")["report"]
        types = [m["mapping_type"] for m in report["mappings"]]
        assert types.count("source_gap") == 6
        assert "repeat" not in types

    def test_more_gp_than_source_measures_yields_gp_gaps(self, client):
        # A plain GP fixture: no repeat span and no marker, so nothing justifies
        # mapping two source measures onto one GP measure and the gap accounting
        # is unambiguous.
        client.fake.add(
            PROJECT_ID, "gp-plain", "guitar-pro", "ce" * 32, "plain.gp5",
            make_gp_bytes(n_measures=8, marker_at=None, repeat=False),
        )
        client.fake.add_gp_revision(PROJECT_ID, 4, "ce" * 32)
        client.fake.blobs[(PROJECT_ID, MIDI_LINK)] = make_midi_bytes(n_measures=2)
        report = run_to_success(client, gp_asset_link_id="gp-plain")["report"]
        gp_gaps = [m for m in report["mappings"] if m["mapping_type"] == "gp_gap"]
        assert len(gp_gaps) == 6
        assert all(m["gp_measure_index"] is not None for m in gp_gaps)
        assert all(m["source_measure_index"] == -1 for m in gp_gaps)


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------

class TestDeterminism:
    def test_canonical_analysis_content_is_stable_across_runs(self, client, tmp_path):
        first = run_to_success(client)["report"]

        # A second, independent service instance over a fresh database.
        from importlib import reload

        os.environ["RT_DATA_ROOT"] = str(tmp_path / "data2")
        os.environ["RT_TMP_ROOT"] = str(tmp_path / "tmp2")
        import reference_time.config
        reload(reference_time.config)
        import reference_time.api as api_mod
        reload(api_mod)
        api_mod.asset_client = client.fake
        with TestClient(api_mod.app) as second_client:
            second_client.fake = client.fake
            second = run_to_success(second_client)["report"]

        volatile = {"analysis_id", "created_at", "completed_at", "provenance"}
        assert {k: v for k, v in first.items() if k not in volatile} == {
            k: v for k, v in second.items() if k not in volatile
        }
        assert first["cache_key"] == second["cache_key"]
