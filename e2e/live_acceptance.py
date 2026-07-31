#!/usr/bin/env python3
"""Post-deploy live acceptance runner for the Phase 2 reference-time slice.

This is **not** a pytest suite. It is an executable module invoked by a dedicated
Tekton task after rollout, against the real deployed services. It is excluded
from root pytest collection by `pytest.ini`.

Fail-closed by construction
---------------------------
Every observation goes through `Check.require`, which records the observation and
raises `AcceptanceFailure` when it does not hold. A scenario is reported as
`pass` only when it both completed without raising *and* has no failed recorded
check. The evidence document is generated from those recorded checks, so it
cannot claim PASS while an assertion failed, and the process exits non-zero if
any scenario is not `pass`.

There is no skip. A prerequisite that cannot be met — no cluster access for the
restart scenario, no reachable Flask UI — is a failure, because acceptance
requires it.

Fixtures are generated fresh for every run and tagged with a unique run id, so
scenario 8 always starts from a genuinely cold cache identity. Nothing
copyrighted is used.

Usage:
    python e2e/live_acceptance.py \
        --base-url https://192.168.30.2 \
        --flask-url https://192.168.30.2 \
        --flask-host gpmidi.ailab.local \
        --output /workspace/evidence/e2e-evidence.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import math
import os
import struct
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import guitarpro
import mido
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RUN_ID = uuid.uuid4().hex[:10]
STARTED_AT = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fail-closed check framework
# ---------------------------------------------------------------------------

class AcceptanceFailure(AssertionError):
    """A recorded observation did not hold."""


@dataclass
class Observation:
    description: str
    ok: bool
    detail: str = ""


@dataclass
class ScenarioResult:
    id: int
    key: str
    title: str
    observations: list[Observation] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    error: str | None = None
    traceback: str | None = None
    duration_seconds: float = 0.0

    @property
    def status(self) -> str:
        # PASS requires both: no exception, and no failed observation.
        if self.error is not None:
            return "fail"
        if not self.observations:
            return "fail"
        if any(not o.ok for o in self.observations):
            return "fail"
        return "pass"


class Check:
    """Records observations for one scenario and raises on the first failure."""

    def __init__(self, result: ScenarioResult):
        self._result = result

    def require(self, condition: object, description: str, detail: str = "") -> None:
        ok = bool(condition)
        self._result.observations.append(Observation(description, ok, str(detail)))
        marker = "OK  " if ok else "FAIL"
        print(f"    [{marker}] {description}" + (f" — {detail}" if detail else ""))
        if not ok:
            raise AcceptanceFailure(f"{description} ({detail})" if detail else description)

    def equal(self, actual, expected, description: str) -> None:
        self.require(
            actual == expected, description, f"expected {expected!r}, got {actual!r}"
        )

    def fact(self, key: str, value) -> None:
        """Record a machine-readable fact without asserting anything."""
        self._result.facts[key] = value


class Runner:
    def __init__(self):
        self.results: list[ScenarioResult] = []
        self.shared: dict = {}

    def run(self, scenario_id: int, key: str, title: str, fn) -> ScenarioResult:
        result = ScenarioResult(id=scenario_id, key=key, title=title)
        print(f"\n=== E2E-{scenario_id:02d} {title} ===")
        started = time.monotonic()
        try:
            fn(Check(result), self.shared)
        except AcceptanceFailure as e:
            result.error = str(e)
        except Exception as e:  # noqa: BLE001 - record and continue, never swallow
            result.error = f"{type(e).__name__}: {e}"
            result.traceback = traceback.format_exc(limit=12)
            print(f"    [FAIL] unexpected error: {result.error}")
        result.duration_seconds = round(time.monotonic() - started, 3)
        self.results.append(result)
        print(f"--- E2E-{scenario_id:02d} {result.status.upper()} "
              f"({result.duration_seconds}s) ---")
        return result

    @property
    def failed(self) -> list[ScenarioResult]:
        return [r for r in self.results if r.status != "pass"]

    def evidence(self, config: dict) -> dict:
        return {
            "evidence_version": "2",
            "run_id": RUN_ID,
            "started_at": STARTED_AT,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "config": config,
            "overall_status": "pass" if not self.failed else "fail",
            "scenario_count": len(self.results),
            "passed": len(self.results) - len(self.failed),
            "failed": len(self.failed),
            "scenarios": [
                {
                    "id": r.id,
                    "key": r.key,
                    "title": r.title,
                    "status": r.status,
                    "duration_seconds": r.duration_seconds,
                    "error": r.error,
                    "traceback": r.traceback,
                    "observations": [
                        {"description": o.description, "ok": o.ok, "detail": o.detail}
                        for o in r.observations
                    ],
                    "facts": r.facts,
                }
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class Api:
    def __init__(self, base_url: str, flask_url: str, flask_host: str, timeout: float):
        self.base = base_url.rstrip("/")
        self.asset = f"{self.base}/asset-api"
        self.rt = f"{self.base}/reference-time"
        self.flask = flask_url.rstrip("/")
        self.flask_host = flask_host
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = False

    # -- asset api --
    def create_project(self, name: str, description: str) -> dict:
        r = self.session.post(
            f"{self.asset}/v1/projects",
            json={"name": name, "description": description},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def get_project(self, project_id: str) -> dict:
        r = self.session.get(
            f"{self.asset}/v1/projects/{project_id}", timeout=self.timeout
        )
        r.raise_for_status()
        return r.json()

    def upload(self, project_id: str, role: str, filename: str, data: bytes) -> dict:
        t = self.session.post(
            f"{self.asset}/v1/projects/{project_id}/upload-tickets",
            json={"role": role, "original_filename": filename},
            timeout=self.timeout,
        )
        t.raise_for_status()
        ticket = t.json()["ticket"]
        u = self.session.put(
            f"{self.asset}/v1/uploads/{ticket}",
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=max(self.timeout, 300),
        )
        u.raise_for_status()
        return u.json()

    def download(self, project_id: str, link_id: str) -> bytes:
        r = self.session.get(
            f"{self.asset}/v1/projects/{project_id}/assets/{link_id}/download",
            timeout=max(self.timeout, 300),
        )
        r.raise_for_status()
        return r.content

    # -- reference time --
    def create_analysis(self, project_id: str, body: dict) -> requests.Response:
        return self.session.post(
            f"{self.rt}/v1/projects/{project_id}/analyses",
            json=body,
            timeout=self.timeout,
        )

    def job(self, job_id: str) -> dict:
        r = self.session.get(f"{self.rt}/v1/jobs/{job_id}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def list_analyses(self, project_id: str) -> dict:
        r = self.session.get(
            f"{self.rt}/v1/projects/{project_id}/analyses", timeout=self.timeout
        )
        r.raise_for_status()
        return r.json()

    def report_json(self, project_id: str, analysis_id: str) -> dict:
        r = self.session.get(
            f"{self.rt}/v1/projects/{project_id}/analyses/{analysis_id}/report.json",
            timeout=max(self.timeout, 120),
        )
        r.raise_for_status()
        return r.json()

    def report_html(self, project_id: str, analysis_id: str) -> str:
        r = self.session.get(
            f"{self.rt}/v1/projects/{project_id}/analyses/{analysis_id}/report.html",
            timeout=max(self.timeout, 120),
        )
        r.raise_for_status()
        return r.text

    def await_job(self, job_id: str, timeout: float = 240.0) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            last = self.job(job_id)
            if last["status"] in ("succeeded", "failed", "interrupted", "cancelled"):
                return last
            time.sleep(1.0)
        raise AcceptanceFailure(
            f"job {job_id} did not reach a terminal state within {timeout}s: {last}"
        )

    def run_analysis(self, project_id: str, body: dict, timeout: float = 240.0) -> dict:
        """POST, wait, and return {'response', 'job', 'report'}."""
        resp = self.create_analysis(project_id, body)
        if resp.status_code != 200:
            raise AcceptanceFailure(
                f"create_analysis returned {resp.status_code}: {resp.text[:400]}"
            )
        data = resp.json()
        if data.get("cache_hit"):
            job = {"status": "succeeded", "cache_hit": True, "job_id": data["job_id"]}
        else:
            job = self.await_job(data["job_id"], timeout=timeout)
        report = None
        if job["status"] == "succeeded":
            report = self.report_json(project_id, data["analysis_id"])
        return {"response": data, "job": job, "report": report}

    # -- flask ui --
    def flask_get(self, path: str, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {})
        if self.flask_host:
            headers["Host"] = self.flask_host
        return self.session.get(
            f"{self.flask}{path}", headers=headers, timeout=self.timeout, **kwargs
        )

    def flask_post(self, path: str, data, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {})
        if self.flask_host:
            headers["Host"] = self.flask_host
        return self.session.post(
            f"{self.flask}{path}",
            data=data,
            headers=headers,
            timeout=max(self.timeout, 120),
            allow_redirects=True,
            **kwargs,
        )

    # -- plain http --
    def plain(self, url: str, timeout: float | None = None) -> requests.Response:
        return self.session.get(url, timeout=timeout or self.timeout)


# ---------------------------------------------------------------------------
# Generated fixtures — unique per run, redistributable
# ---------------------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_gp(
    n_measures: int = 8,
    bpm: int = 120,
    marker_at: int | None = 4,
    marker_text: str = "Chorus",
    repeat_open: int | None = 2,
    repeat_close: int | None = 5,
    tag: str = "",
) -> bytes:
    """Minimal GP5 file with notes, a marker and a repeat span."""
    song = guitarpro.Song()
    song.title = f"E2E-{RUN_ID}-{tag or n_measures}"
    song.tempo = bpm

    while len(song.measureHeaders) < n_measures:
        song.measureHeaders.append(guitarpro.MeasureHeader())
    del song.measureHeaders[n_measures:]

    for i, hdr in enumerate(song.measureHeaders):
        hdr.number = i + 1
        hdr.timeSignature.numerator = 4
        hdr.timeSignature.denominator.value = 4
        if marker_at is not None and i == marker_at:
            hdr.marker = guitarpro.Marker(
                title=marker_text, color=guitarpro.Color(255, 0, 0)
            )
        if repeat_open is not None and i == repeat_open:
            hdr.isRepeatOpen = True
        if repeat_close is not None and i == repeat_close:
            hdr.repeatClose = 2

    track = song.tracks[0]
    track.measures = []
    for hdr in song.measureHeaders:
        measure = guitarpro.Measure(track, hdr)
        voice = measure.voices[0]
        voice.beats = []
        for _ in range(4):
            beat = guitarpro.Beat(voice)
            beat.duration.value = 4
            note = guitarpro.Note(beat)
            note.value = 5
            note.string = 1
            note.velocity = 95
            beat.notes.append(note)
            voice.beats.append(beat)
        track.measures.append(measure)

    buf = io.BytesIO()
    guitarpro.write(song, buf, version=(5, 1, 0))
    return buf.getvalue()


def make_midi(
    n_measures: int = 8,
    bpm: int = 120,
    ppq: int = 480,
    tag: str = "",
    pitch: int = 60,
) -> bytes:
    """Type 0 MIDI with one tempo, 4/4, and a note on every beat."""
    mid = mido.MidiFile(ticks_per_beat=ppq, type=0)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    # The track name makes this run's fixture byte-unique, so scenario 8 starts
    # from a cache identity that has never been computed before.
    track.append(mido.MetaMessage("track_name", name=f"e2e-{RUN_ID}-{tag}", time=0))
    for _ in range(n_measures):
        for _ in range(4):
            track.append(mido.Message("note_on", note=pitch, velocity=80, time=0))
            track.append(mido.Message("note_off", note=pitch, velocity=0, time=ppq))
    track.append(mido.MetaMessage("end_of_track", time=0))
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


def make_conflicting_midi(
    n_measures: int = 8, bpm_start: int = 120, bpm_end: int = 80, ppq: int = 480
) -> bytes:
    """MIDI whose tempo diverges half-way, so consensus must flag a region."""
    mid = mido.MidiFile(ticks_per_beat=ppq, type=0)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm_start), time=0))
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    track.append(mido.MetaMessage("track_name", name=f"e2e-{RUN_ID}-conflict", time=0))
    half = n_measures // 2
    for _ in range(half):
        for _ in range(4):
            track.append(mido.Message("note_on", note=60, velocity=80, time=0))
            track.append(mido.Message("note_off", note=60, velocity=0, time=ppq))
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm_end), time=0))
    for _ in range(n_measures - half):
        for _ in range(4):
            track.append(mido.Message("note_on", note=60, velocity=80, time=0))
            track.append(mido.Message("note_off", note=60, velocity=0, time=ppq))
    track.append(mido.MetaMessage("end_of_track", time=0))
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


def make_redundant_tempo_midi(n_measures: int = 8, bpm: int = 120, ppq: int = 480) -> bytes:
    """Same timing as `make_midi` but with a redundant tempo event per bar.

    Consensus must call this AGREED: equal raw tempo-event counts are not a
    requirement, only equal normalized trajectories.
    """
    mid = mido.MidiFile(ticks_per_beat=ppq, type=0)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    tempo = mido.bpm2tempo(bpm)
    track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    track.append(mido.MetaMessage("track_name", name=f"e2e-{RUN_ID}-redundant", time=0))
    for _ in range(n_measures):
        track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
        for _ in range(4):
            track.append(mido.Message("note_on", note=60, velocity=80, time=0))
            track.append(mido.Message("note_off", note=60, velocity=0, time=ppq))
    track.append(mido.MetaMessage("end_of_track", time=0))
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


def make_wav_clicks(
    duration_sec: float = 16.0,
    sr: int = 22050,
    click_period: float = 2.0,
    phase_offset: float = 0.0,
) -> bytes:
    """WAV with sharp clicks every `click_period` seconds from `phase_offset`.

    At 120 BPM 4/4 a bar is 2.0s, so `click_period=2.0, phase_offset=0.0` puts a
    click exactly on every MIDI downbeat and `phase_offset=1.0` puts every click
    exactly off-phase.
    """
    n_samples = int(duration_sec * sr)
    samples = [0.0] * n_samples
    t = phase_offset
    while t < duration_sec:
        idx = int(t * sr)
        for k in range(min(256, n_samples - idx)):
            if idx + k < n_samples:
                decay = math.exp(-k / 40.0)
                samples[idx + k] += 0.9 * decay * (1.0 if k % 2 == 0 else -1.0)
        t += click_period

    peak = max((abs(s) for s in samples), default=1.0) or 1.0
    pcm = b"".join(
        struct.pack("<h", int(max(min(s / peak * 32000, 32767), -32768))) for s in samples
    )
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE",
        b"fmt ", 16, 1, 1, sr,
        sr * 2, 2, 16,
        b"data", len(pcm),
    )
    return header + pcm


def make_structure(anchors=None, sections=None, version="1.0") -> bytes:
    return json.dumps(
        {"version": version, "sections": sections or [], "anchors": anchors or []},
        sort_keys=True,
    ).encode()


# ---------------------------------------------------------------------------
# Cluster access (scenario 10 only)
# ---------------------------------------------------------------------------

class Cluster:
    """Least-privilege cluster operations for the restart scenario.

    Runs inside the dedicated Tekton E2E task, whose ServiceAccount may only
    read/delete pods and read deployments in `gpmidi-ml`. If no kubectl is
    available this is a failure, not a skip: acceptance requires the restart
    proof.
    """

    def __init__(self, kubectl: str, namespace: str):
        self.kubectl = kubectl
        self.namespace = namespace

    def available(self) -> tuple[bool, str]:
        try:
            out = subprocess.run(
                [self.kubectl, "-n", self.namespace, "get", "pods", "-o", "name"],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"{type(e).__name__}: {e}"
        if out.returncode != 0:
            return False, out.stderr.strip()[:300]
        return True, out.stdout.strip()[:300]

    def _run(self, *args: str, timeout: float = 120) -> str:
        proc = subprocess.run(
            [self.kubectl, "-n", self.namespace, *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if proc.returncode != 0:
            raise AcceptanceFailure(
                f"kubectl {' '.join(args)} failed: {proc.stderr.strip()[:400]}"
            )
        return proc.stdout.strip()

    def pod_names(self, app: str) -> list[str]:
        out = self._run(
            "get", "pods", "-l", f"app={app}",
            "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
        )
        return [p for p in out.splitlines() if p]

    def pod_field(self, app: str, jsonpath: str) -> str:
        return self._run("get", "pods", "-l", f"app={app}", "-o", f"jsonpath={jsonpath}")

    def delete_pods(self, app: str) -> list[str]:
        names = self.pod_names(app)
        for name in names:
            self._run("delete", "pod", name, "--wait=false")
        return names

    def wait_ready(self, app: str, timeout: float = 240) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready = self.pod_field(app, "{.items[*].status.containerStatuses[*].ready}")
            names = self.pod_names(app)
            if names and ready and "false" not in ready.split():
                return names[0]
            time.sleep(3)
        raise AcceptanceFailure(f"pods for app={app} did not become ready in {timeout}s")

    def exec_in(self, app: str, command: list[str], timeout: float = 60) -> str:
        names = self.pod_names(app)
        if not names:
            raise AcceptanceFailure(f"no pod found for app={app}")
        return self._run("exec", names[0], "--", *command, timeout=timeout)


# ---------------------------------------------------------------------------
# Shared project setup
# ---------------------------------------------------------------------------

def setup(api: Api) -> dict:
    """Create a fresh project and upload every fixture this run needs."""
    project = api.create_project(
        f"E2E-Phase2-{RUN_ID}",
        f"Generated live acceptance run {RUN_ID} at {STARTED_AT}",
    )
    pid = project["id"]

    fixtures: dict[str, bytes] = {
        "gp_v1": make_gp(n_measures=8, tag="v1"),
        "gp_v2": make_gp(n_measures=6, marker_at=1, repeat_open=None,
                         repeat_close=None, tag="v2"),
        "gp_plain": make_gp(n_measures=8, marker_at=None, repeat_open=None,
                            repeat_close=None, tag="plain"),
        "gp_short": make_gp(n_measures=3, marker_at=None, repeat_open=None,
                            repeat_close=None, tag="short"),
        "midi_a": make_midi(n_measures=8, tag="a"),
        "midi_b": make_redundant_tempo_midi(n_measures=8),
        "midi_conflict": make_conflicting_midi(n_measures=8),
        "midi_short": make_midi(n_measures=2, tag="short"),
        "wav_in_phase": make_wav_clicks(duration_sec=16.0, click_period=2.0,
                                        phase_offset=0.0),
        "wav_off_phase": make_wav_clicks(duration_sec=16.0, click_period=2.0,
                                         phase_offset=1.0),
        "structure_ok": make_structure(
            anchors=[{"source_measure": 1, "gp_measure": 5, "label": "Chorus"}],
            sections=[{"label": "Intro", "source_measure": 0, "gp_measure": 0}],
        ),
        "structure_conflict": make_structure(
            anchors=[
                {"source_measure": 1, "gp_measure": 4},
                {"source_measure": 3, "gp_measure": 2},
            ]
        ),
        "structure_seconds": make_structure(
            anchors=[{"source_seconds": 5.0, "gp_measure": 4}]
        ),
    }

    spec = {
        "gp_v1": ("guitar-pro", f"e2e-{RUN_ID}-v1.gp5"),
        "gp_v2": ("guitar-pro", f"e2e-{RUN_ID}-v2.gp5"),
        "gp_plain": ("guitar-pro", f"e2e-{RUN_ID}-plain.gp5"),
        "gp_short": ("guitar-pro", f"e2e-{RUN_ID}-short.gp5"),
        "midi_a": ("suno-midi.mix", f"e2e-{RUN_ID}-a.mid"),
        "midi_b": ("suno-midi.drums", f"e2e-{RUN_ID}-b.mid"),
        "midi_conflict": ("suno-midi.bass", f"e2e-{RUN_ID}-conflict.mid"),
        "midi_short": ("suno-midi.guitar", f"e2e-{RUN_ID}-short.mid"),
        "wav_in_phase": ("mix", f"e2e-{RUN_ID}-inphase.wav"),
        "wav_off_phase": ("stem.drums", f"e2e-{RUN_ID}-offphase.wav"),
        "structure_ok": ("structure", f"e2e-{RUN_ID}-ok.json"),
        "structure_conflict": ("structure", f"e2e-{RUN_ID}-conflict.json"),
        "structure_seconds": ("structure", f"e2e-{RUN_ID}-seconds.json"),
    }

    links: dict[str, str] = {}
    shas: dict[str, str] = {}
    for key, (role, filename) in spec.items():
        data = fixtures[key]
        result = api.upload(pid, role, filename, data)
        links[key] = result["link_id"]
        shas[key] = result["sha256"]
        if result["sha256"] != sha256_hex(data):
            raise AcceptanceFailure(
                f"Asset API reported a different digest for {key}"
            )

    return {
        "project_id": pid,
        "links": links,
        "shas": shas,
        "fixtures": fixtures,
        "filenames": {k: v[1] for k, v in spec.items()},
    }


def body(state: dict, gp="gp_v1", midis=("midi_a",), audio=(), structure=None,
         claim_sha=True) -> dict:
    out = {
        "gp_asset_link_id": state["links"][gp],
        "source_midi_link_ids": [state["links"][m] for m in midis],
        "audio_link_ids": [state["links"][a] for a in audio],
    }
    if claim_sha:
        out["gp_revision_sha256"] = state["shas"][gp]
    if structure:
        out["structure_link_id"] = state["links"][structure]
    return out


def mappings_by_source(report: dict) -> dict[int, dict]:
    return {
        m["source_measure_index"]: m
        for m in report["mappings"]
        if m["source_measure_index"] >= 0
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_01_gp_grid(check: Check, shared: dict) -> None:
    """GP parses to the intended multi-measure grid with notes, marker, repeats."""
    api, state = shared["api"], shared["state"]
    out = api.run_analysis(state["project_id"], body(state))
    check.equal(out["job"]["status"], "succeeded", "baseline analysis succeeds")
    report = out["report"]
    shared["baseline"] = out

    gp = report["gp_measures"]
    check.equal(len(gp), 8, "GP grid has the 8 measures the fixture declares")
    check.require(
        all(m["numerator"] == 4 and m["denominator"] == 4 for m in gp),
        "every GP measure is 4/4",
    )
    check.require(
        all(m["tick_end"] > m["tick_start"] for m in gp),
        "every GP measure has a positive tick span",
    )
    check.require(
        [m["measure_number"] for m in gp] == list(range(1, 9)),
        "GP measure numbers are contiguous and 1-based",
    )
    non_empty = [m for m in gp if not m["is_empty"]]
    check.equal(len(non_empty), 8, "every GP measure carries notes")

    markers = [(m["measure_index"], m["marker_text"]) for m in gp if m["marker_text"]]
    check.equal(markers, [(4, "Chorus")], "the marker is parsed at the declared measure")

    opens = [m["measure_index"] for m in gp if m["has_repeat_open"]]
    closes = [m["measure_index"] for m in gp if m["has_repeat_close"]]
    check.equal(opens, [2], "repeat-open is parsed at the declared measure")
    check.equal(closes, [5], "repeat-close is parsed at the declared measure")
    check.equal(
        [m["repeat_close_count"] for m in gp if m["has_repeat_close"]], [2],
        "repeat count is preserved",
    )
    check.equal(
        report["gp_revision_sha256"], state["shas"]["gp_v1"],
        "the analysis records the trusted GP digest",
    )
    check.require(
        report["gp_revision_number"] is not None,
        "the resolved GP revision number is recorded",
        f"revision {report['gp_revision_number']}",
    )
    check.fact("gp_measure_count", len(gp))
    check.fact("marker", markers)
    check.fact("analysis_id", out["response"]["analysis_id"])
    check.fact("global_confidence", report["global_confidence"])


def scenario_02_consensus(check: Check, shared: dict) -> None:
    """Two agreeing MIDIs produce a deterministic consensus."""
    api, state = shared["api"], shared["state"]
    out = api.run_analysis(state["project_id"], body(state, midis=("midi_a", "midi_b")))
    check.equal(out["job"]["status"], "succeeded", "two-source analysis succeeds")
    consensus = out["report"]["midi_consensus"]
    shared["agreed"] = out

    check.equal(consensus["decision"], "agreed", "the two sources agree")
    check.equal(consensus["source_count"], 2, "both sources took part")
    check.require(
        not consensus["conflict_regions"], "no conflict region is reported",
        json.dumps(consensus["conflict_regions"])[:200],
    )
    metrics = list(consensus["agreement_metrics"].values())
    check.equal(len(metrics), 1, "one pairwise comparison is recorded")
    pair = metrics[0]
    counts = (
        pair["tempo_comparison"]["event_count_a"],
        pair["tempo_comparison"]["event_count_b"],
    )
    check.require(
        counts[0] != counts[1],
        "the fixtures deliberately carry different raw tempo-event counts",
        f"{counts}",
    )
    check.require(
        pair["tempo_comparison"]["max_bpm_diff"] <= 1.0,
        "normalized tempo trajectories match despite that",
        f"max_bpm_diff={pair['tempo_comparison']['max_bpm_diff']}",
    )
    check.require(
        pair["measure_boundary_comparison"]["aligned"],
        "measure/downbeat boundaries agree",
        json.dumps(pair["measure_boundary_comparison"]),
    )
    check.require(
        pair["preroll_comparison"]["aligned"], "pre-roll/downbeat offsets agree"
    )

    # Determinism: the same two sources in the opposite request order must reuse
    # the identical analysis rather than select a different primary.
    reverse = api.create_analysis(
        state["project_id"], body(state, midis=("midi_b", "midi_a"))
    )
    check.equal(reverse.status_code, 200, "reversed-order request is accepted")
    rev = reverse.json()
    check.require(rev["cache_hit"], "reversed order is an idempotent cache hit")
    check.equal(
        rev["analysis_id"], out["response"]["analysis_id"],
        "selection does not depend on request order",
    )
    check.fact("primary_sha256", consensus["primary_sha256"])
    check.fact("selection_reason", consensus["selection_reason"])


def scenario_03_conflict(check: Check, shared: dict) -> None:
    """A conflicting MIDI produces explicit regional conflict and lower confidence."""
    api, state = shared["api"], shared["state"]
    agreed = shared.get("agreed") or api.run_analysis(
        state["project_id"], body(state, midis=("midi_a", "midi_b"))
    )
    out = api.run_analysis(
        state["project_id"], body(state, midis=("midi_a", "midi_conflict"))
    )
    check.equal(out["job"]["status"], "succeeded", "conflicting analysis still completes")
    report = out["report"]
    consensus = report["midi_consensus"]

    check.equal(consensus["decision"], "conflict", "the conflict is explicit")
    check.require(consensus["conflict_regions"], "conflict regions are reported")
    localized = [
        r for r in consensus["conflict_regions"]
        if isinstance(r.get("region"), dict) and "start_seconds" in r["region"]
    ]
    check.require(
        localized, "at least one conflict is localized to a time region",
        json.dumps(consensus["conflict_regions"])[:300],
    )
    check.require(
        report["global_confidence"] < agreed["report"]["global_confidence"],
        "global confidence is lower than the agreeing case",
        f"{report['global_confidence']} < {agreed['report']['global_confidence']}",
    )
    codes = {w["code"] for w in report["global_warnings"]}
    check.require("tempo_map_conflict" in codes, "tempo_map_conflict is warned")

    flagged = [
        m for m in report["mappings"]
        if "consensus_conflict_region" in m["reason_codes"]
    ]
    check.require(
        flagged, "mappings inside a conflict region carry the reason code"
    )
    agreed_by_src = mappings_by_source(agreed["report"])
    conflict_by_src = mappings_by_source(report)
    lowered = [
        idx for idx, m in conflict_by_src.items()
        if idx in agreed_by_src and m["confidence"] < agreed_by_src[idx]["confidence"]
    ]
    check.require(
        lowered, "per-mapping confidence is reduced inside the conflict region",
        f"lowered source measures: {sorted(lowered)[:8]}",
    )
    check.fact("conflict_region_count", len(consensus["conflict_regions"]))
    check.fact("global_confidence_conflict", report["global_confidence"])
    check.fact("global_confidence_agreed", agreed["report"]["global_confidence"])


def scenario_04_audio(check: Check, shared: dict) -> None:
    """Audio clicks measurably corroborate or contradict the MIDI phase."""
    api, state = shared["api"], shared["state"]
    no_audio = shared.get("baseline") or api.run_analysis(
        state["project_id"], body(state)
    )
    in_phase = api.run_analysis(
        state["project_id"], body(state, audio=("wav_in_phase",))
    )
    off_phase = api.run_analysis(
        state["project_id"], body(state, audio=("wav_off_phase",))
    )
    for label, out in (("in-phase", in_phase), ("off-phase", off_phase)):
        check.equal(out["job"]["status"], "succeeded", f"{label} audio analysis succeeds")

    base_codes = {w["code"] for w in no_audio["report"]["global_warnings"]}
    check.require(
        "audio_missing" in base_codes,
        "an analysis without audio warns audio_missing",
    )

    ev = in_phase["report"]["audio_evidence"]
    check.equal(len(ev), 1, "the audio asset is serialized into the report")
    check.equal(
        ev[0]["sha256"], state["shas"]["wav_in_phase"],
        "the exact audio digest is recorded",
    )
    check.equal(
        ev[0]["asset_link_id"], state["links"]["wav_in_phase"],
        "the exact audio link id is recorded",
    )
    check.require(ev[0]["onset_count"] > 0, "onsets were detected",
                  f"{ev[0]['onset_count']} onsets")
    check.require(
        ev[0]["downbeat_candidates"], "downbeat candidates were derived"
    )
    codes = {w["code"] for w in in_phase["report"]["global_warnings"]}
    check.require("audio_missing" not in codes, "audio_missing is not warned when audio is present")

    def corroborated(report):
        return [
            m for m in report["source_measures"]
            if m["audio_downbeat_evidence"] is not None
            and m["audio_downbeat_evidence"] > 0.5
        ]

    in_ok = corroborated(in_phase["report"])
    off_ok = corroborated(off_phase["report"])
    check.require(
        len(in_ok) >= 4,
        "in-phase clicks corroborate the MIDI downbeats",
        f"{len(in_ok)} of {len(in_phase['report']['source_measures'])} measures",
    )
    check.require(
        len(off_ok) < len(in_ok),
        "clicks shifted half a bar corroborate measurably fewer downbeats",
        f"off-phase {len(off_ok)} vs in-phase {len(in_ok)}",
    )
    check.require(
        any(
            any(e.startswith("audio_corroboration") for e in m["evidence"])
            for m in in_phase["report"]["mappings"]
        ),
        "audio corroboration reaches the mapping evidence",
    )
    in_conf = in_phase["report"]["global_confidence"]
    off_conf = off_phase["report"]["global_confidence"]
    check.require(
        in_conf > off_conf,
        "in-phase audio yields higher confidence than off-phase audio",
        f"{in_conf} > {off_conf}",
    )
    check.fact("in_phase_corroborated_measures", len(in_ok))
    check.fact("off_phase_corroborated_measures", len(off_ok))
    check.fact("in_phase_confidence", in_conf)
    check.fact("off_phase_confidence", off_conf)


def scenario_05_anchor(check: Check, shared: dict) -> None:
    """A valid structure anchor changes or locks the mapping, not just the key."""
    api, state = shared["api"], shared["state"]
    free = shared.get("baseline") or api.run_analysis(state["project_id"], body(state))
    anchored = api.run_analysis(
        state["project_id"], body(state, structure="structure_ok")
    )
    check.equal(anchored["job"]["status"], "succeeded", "anchored analysis succeeds")
    report = anchored["report"]

    free_map = mappings_by_source(free["report"])
    anchored_map = mappings_by_source(report)

    check.require(1 in anchored_map, "the anchored source measure is mapped")
    check.equal(
        anchored_map[1]["gp_measure_index"], 5,
        "source measure 1 is locked to the anchored GP measure 5",
    )
    check.require(
        "anchored" in anchored_map[1]["reason_codes"],
        "the mapping records that it was anchored",
    )
    check.equal(report["anchored_source_indices"], [1], "the anchor is reported")
    check.require(
        free_map[1]["gp_measure_index"] != 5,
        "without the anchor that source measure mapped elsewhere",
        f"unanchored gp index {free_map[1]['gp_measure_index']}",
    )
    # The requirement is that the MAPPING changes, not merely the cache identity.
    free_pairs = [(i, m["gp_measure_index"]) for i, m in sorted(free_map.items())]
    anchored_pairs = [(i, m["gp_measure_index"]) for i, m in sorted(anchored_map.items())]
    check.require(
        free_pairs != anchored_pairs,
        "the selected source-to-GP mapping itself differs",
        f"{free_pairs} -> {anchored_pairs}",
    )
    check.equal(report["structure_version"], "1.0", "the structure version is recorded")
    labels = [s["label"] for s in report["structure_sections"]]
    check.require("Intro" in labels, "section evidence appears in canonical JSON",
                  json.dumps(labels))

    html = api.report_html(state["project_id"], anchored["response"]["analysis_id"])
    check.require("Structure and Markers" in html, "section evidence appears in the HTML report")
    check.require("Chorus" in html, "GP marker evidence appears in the HTML report")

    # A timestamp anchor must be converted to the measure containing that instant.
    seconds = api.run_analysis(
        state["project_id"], body(state, structure="structure_seconds")
    )
    check.equal(seconds["job"]["status"], "succeeded", "timestamp-anchored analysis succeeds")
    check.equal(
        seconds["report"]["anchored_source_indices"], [2],
        "source_seconds=5.0 resolves to the measure spanning [4.0, 6.0)",
    )
    check.fact("anchored_mapping", anchored_pairs)
    check.fact("unanchored_mapping", free_pairs)


def scenario_06_conflicting_anchor(check: Check, shared: dict) -> None:
    """Conflicting anchors fail with a stable error code."""
    api, state = shared["api"], shared["state"]
    resp = api.create_analysis(
        state["project_id"], body(state, structure="structure_conflict")
    )
    check.equal(resp.status_code, 200, "the request is admitted for processing")
    data = resp.json()
    job = api.await_job(data["job_id"])
    check.equal(job["status"], "failed", "the analysis fails closed")
    check.equal(
        job["error_code"], "structure_non_monotonic_anchors",
        "the failure carries the stable structure error code",
    )
    check.require(
        api.session.get(
            f"{api.rt}/v1/projects/{state['project_id']}/analyses/"
            f"{data['analysis_id']}/report.json",
            timeout=api.timeout,
        ).status_code == 404,
        "no report is published for the failed analysis",
    )
    check.fact("error_code", job["error_code"])

    # A wrong claimed GP digest must be rejected before any job exists.
    bad = body(state)
    bad["gp_revision_sha256"] = "f" * 64
    rejected = api.create_analysis(state["project_id"], bad)
    check.equal(rejected.status_code, 422, "a mismatching claimed GP digest is rejected")
    detail = rejected.json()["detail"]
    check.equal(detail["code"], "gp_revision_mismatch", "with a stable error code")
    check.require(detail.get("request_id"), "and a request id")

    # A wrong-role asset must be rejected too.
    wrong_role = {
        "gp_asset_link_id": state["links"]["midi_a"],
        "source_midi_link_ids": [state["links"]["midi_a"]],
        "audio_link_ids": [],
    }
    r2 = api.create_analysis(state["project_id"], wrong_role)
    check.equal(r2.status_code, 422, "a wrong-role GP input is rejected")
    check.equal(
        r2.json()["detail"]["code"], "asset_role_invalid", "with a stable error code"
    )


def scenario_07_gaps(check: Check, shared: dict) -> None:
    """Both source-gap and GP-gap cases produce explicit records."""
    api, state = shared["api"], shared["state"]

    # 8 source measures against a 3-measure GP: 5 source gaps.
    src_gap = api.run_analysis(state["project_id"], body(state, gp="gp_short"))
    check.equal(src_gap["job"]["status"], "succeeded", "source-gap analysis succeeds")
    rows = src_gap["report"]["mappings"]
    gaps = [m for m in rows if m["mapping_type"] == "source_gap"]
    check.equal(len(gaps), 5, "every unmatched source measure has a source_gap record")
    check.require(
        all(m["gp_measure_index"] is None for m in gaps),
        "a source_gap record has no GP measure",
    )
    check.require(
        all(m["source_measure_index"] >= 0 for m in gaps),
        "a source_gap record names its source measure",
    )
    check.require(
        not any(m["mapping_type"] == "repeat" for m in rows),
        "a source gap is never reported as a repeat",
        f"types={sorted({m['mapping_type'] for m in rows})}",
    )

    # 2 source measures against an 8-measure GP with no repeat evidence: 6 GP gaps.
    gp_gap = api.run_analysis(
        state["project_id"], body(state, gp="gp_plain", midis=("midi_short",))
    )
    check.equal(gp_gap["job"]["status"], "succeeded", "GP-gap analysis succeeds")
    rows2 = gp_gap["report"]["mappings"]
    gp_gaps = [m for m in rows2 if m["mapping_type"] == "gp_gap"]
    check.equal(len(gp_gaps), 6, "every unmatched GP measure has a gp_gap record")
    check.require(
        all(m["gp_measure_index"] is not None for m in gp_gaps),
        "a gp_gap record names its GP measure",
    )
    check.require(
        all(m["source_measure_index"] == -1 for m in gp_gaps),
        "a gp_gap record has no source measure",
    )
    check.require(
        all(m["gp_tick_start"] is not None for m in gp_gaps),
        "a gp_gap record carries the destination tick span",
    )
    check.fact("source_gap_count", len(gaps))
    check.fact("gp_gap_count", len(gp_gaps))


def scenario_08_cold_cache_concurrency(check: Check, shared: dict) -> None:
    """Concurrent requests on a COLD unique identity admit one computation."""
    api, state = shared["api"], shared["state"]
    pid = state["project_id"]

    # A fixture minted now, so this cache identity has never been computed.
    unique_midi = make_midi(n_measures=8, tag=f"cold-{uuid.uuid4().hex[:8]}", pitch=62)
    uploaded = api.upload(
        pid, "suno-midi.other", f"e2e-{RUN_ID}-cold.mid", unique_midi
    )
    cold_link = uploaded["link_id"]
    check.equal(
        uploaded["sha256"], sha256_hex(unique_midi), "the cold fixture uploaded intact"
    )

    request = {
        "gp_asset_link_id": state["links"]["gp_v1"],
        "gp_revision_sha256": state["shas"]["gp_v1"],
        "source_midi_link_ids": [cold_link],
        "audio_link_ids": [],
    }

    before = api.list_analyses(pid)
    before_ids = {a["analysis_id"] for a in before["analyses"]}

    # Prove the identity really is cold: no result and no job for it yet.
    probe_jobs = {j["job_id"] for j in before["jobs"]}

    concurrency = 8
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(api.create_analysis, pid, request) for _ in range(concurrency)
        ]
        responses = [f.result() for f in futures]

    codes = [r.status_code for r in responses]
    check.require(
        all(c == 200 for c in codes), "every concurrent request is accepted", f"{codes}"
    )
    payloads = [r.json() for r in responses]
    cache_hits = [p for p in payloads if p["cache_hit"]]
    check.equal(
        len(cache_hits), 0,
        "no request reported a cache hit, so the cache really was cold",
    )
    job_ids = {p["job_id"] for p in payloads}
    analysis_ids = {p["analysis_id"] for p in payloads}
    check.equal(
        len(job_ids), 1,
        "exactly one computation was admitted for the cold identity",
    )
    check.equal(
        len(analysis_ids), 1, "every follower was given the admitted analysis id"
    )
    admitted_job = next(iter(job_ids))
    check.require(
        admitted_job not in probe_jobs, "the admitted job is new to this project"
    )

    job = api.await_job(admitted_job)
    check.equal(job["status"], "succeeded", "the single admitted computation succeeds")

    after = api.list_analyses(pid)
    new_ids = {a["analysis_id"] for a in after["analyses"]} - before_ids
    check.equal(
        len(new_ids), 1, "exactly one new analysis result exists for that identity"
    )
    active_for_key = [
        j for j in after["jobs"]
        if j["analysis_id"] == next(iter(analysis_ids))
    ]
    check.equal(len(active_for_key), 1, "no duplicate job row was created")

    # A follow-up request on the now-warm identity must be an idempotent hit.
    warm = api.create_analysis(pid, request)
    check.equal(warm.status_code, 200, "the warm request is accepted")
    check.require(warm.json()["cache_hit"], "the warm request is an idempotent cache hit")
    check.equal(
        warm.json()["analysis_id"], next(iter(analysis_ids)),
        "the warm hit returns the same analysis",
    )
    check.fact("concurrency", concurrency)
    check.fact("admitted_job_id", admitted_job)
    check.fact("admitted_analysis_id", next(iter(analysis_ids)))


def scenario_09_gp_only_revision(check: Check, shared: dict) -> None:
    """A GP-only revision change reuses source evidence and recomputes mapping."""
    api, state = shared["api"], shared["state"]
    pid = state["project_id"]

    first = api.run_analysis(pid, body(state, gp="gp_v1", audio=("wav_in_phase",)))
    check.equal(first["job"]["status"], "succeeded", "revision-1 analysis succeeds")
    p1 = first["report"]["provenance"]
    check.require(
        not p1["source_evidence_reused"],
        "the first run computes source evidence rather than reusing it",
    )
    check.require(
        p1["source_midi_extractions"] >= 1, "it parsed the source MIDI",
        f"{p1['source_midi_extractions']} extraction(s)",
    )
    check.require(
        p1["audio_extractions"] >= 1, "it extracted audio evidence",
        f"{p1['audio_extractions']} extraction(s)",
    )
    check.equal(p1["gp_extractions"], 1, "it parsed the GP file once")

    second = api.run_analysis(pid, body(state, gp="gp_v2", audio=("wav_in_phase",)))
    check.equal(second["job"]["status"], "succeeded", "revision-2 analysis succeeds")
    p2 = second["report"]["provenance"]

    check.require(
        p2["source_evidence_reused"],
        "the GP-only change reuses the persisted source evidence",
    )
    check.equal(
        p2["source_evidence_key"], p1["source_evidence_key"],
        "the source-evidence identity is unchanged",
    )
    check.equal(
        p2["source_midi_extractions"], 0,
        "no source MIDI was parsed again",
    )
    check.equal(p2["audio_extractions"], 0, "no audio was extracted again")
    check.equal(p2["gp_extractions"], 1, "but the GP side was parsed again")
    check.require(
        p2["source_evidence_created_at"] == p1["source_evidence_created_at"],
        "the reused evidence is the row written by the first run",
        f"{p2['source_evidence_created_at']}",
    )
    check.require(
        second["report"]["cache_key"] != first["report"]["cache_key"],
        "the analysis identity changed with the GP revision",
    )
    check.equal(
        len(second["report"]["gp_measures"]), 6,
        "the new GP revision's own grid was extracted",
    )
    check.require(
        second["report"]["gp_revision_sha256"] == state["shas"]["gp_v2"],
        "the second analysis records the second GP digest",
    )
    check.require(
        second["report"]["source_measures"] == first["report"]["source_measures"],
        "the reused source measures are byte-identical",
    )
    check.require(
        [m["gp_measure_index"] for m in mappings_by_source(second["report"]).values()]
        != [m["gp_measure_index"] for m in mappings_by_source(first["report"]).values()],
        "the source-to-GP mapping was genuinely recomputed",
    )
    check.fact("source_evidence_key", p1["source_evidence_key"])
    check.fact("revision_1_cache_key", first["report"]["cache_key"])
    check.fact("revision_2_cache_key", second["report"]["cache_key"])
    shared["determinism_target"] = second


def scenario_10_restart(check: Check, shared: dict) -> None:
    """Pod restart preserves results and terminally recovers queued and running jobs."""
    api, state = shared["api"], shared["state"]
    cluster: Cluster = shared["cluster"]
    pid = state["project_id"]

    available, detail = cluster.available()
    check.require(
        available,
        "the runner has least-privilege cluster access for the restart scenario",
        detail,
    )

    baseline = shared.get("baseline") or api.run_analysis(pid, body(state))
    preserved_analysis_id = baseline["response"]["analysis_id"]
    preserved_before = api.report_json(pid, preserved_analysis_id)

    # Saturate the worker pool with slow work so that, at the moment the pod is
    # deleted, at least one job is running and at least one is still queued.
    slow_audio = make_wav_clicks(duration_sec=240.0, click_period=1.0)
    slow_links = []
    for i in range(3):
        up = api.upload(
            pid, "stem.other", f"e2e-{RUN_ID}-slow-{i}.wav", slow_audio
        )
        slow_links.append(up["link_id"])
    check.equal(len(slow_links), 3, "slow audio fixtures uploaded")

    slow_midis = []
    for i in range(4):
        up = api.upload(
            pid,
            "suno-midi.other",
            f"e2e-{RUN_ID}-slow-{i}.mid",
            make_midi(n_measures=8, tag=f"slow-{i}-{uuid.uuid4().hex[:6]}", pitch=64 + i),
        )
        slow_midis.append(up["link_id"])

    submitted = []
    for link in slow_midis:
        resp = api.create_analysis(
            pid,
            {
                "gp_asset_link_id": state["links"]["gp_v1"],
                "gp_revision_sha256": state["shas"]["gp_v1"],
                "source_midi_link_ids": [link],
                "audio_link_ids": slow_links,
            },
        )
        if resp.status_code == 200 and not resp.json()["cache_hit"]:
            submitted.append(resp.json()["job_id"])
    check.require(
        len(submitted) >= 3, "several slow analyses were admitted", f"{len(submitted)}"
    )

    # Observe both states before disrupting anything.
    observed = {"queued": set(), "running": set()}
    deadline = time.time() + 60
    while time.time() < deadline:
        states = {j: api.job(j)["status"] for j in submitted}
        for job_id, status in states.items():
            if status in observed:
                observed[status].add(job_id)
        if observed["queued"] and observed["running"]:
            break
        if all(s in ("succeeded", "failed", "interrupted") for s in states.values()):
            break
        time.sleep(0.5)

    check.require(
        observed["running"],
        "at least one job was observed running before the restart",
        f"{sorted(observed['running'])}",
    )
    check.require(
        observed["queued"],
        "at least one job was observed queued before the restart",
        f"{sorted(observed['queued'])}",
    )

    old_pods = cluster.pod_names("reference-time")
    check.require(old_pods, "the reference-time pod was found", f"{old_pods}")
    deleted = cluster.delete_pods("reference-time")
    check.require(deleted, "the pod was deleted", f"{deleted}")
    new_pod = cluster.wait_ready("reference-time")
    check.require(
        new_pod not in old_pods, "a new pod replaced it", f"{old_pods} -> {new_pod}"
    )

    # Readiness alone is not enough; wait for the API to answer.
    deadline = time.time() + 120
    ready = False
    while time.time() < deadline:
        try:
            if api.plain(f"{api.rt}/readyz", timeout=5).status_code == 200:
                ready = True
                break
        except requests.RequestException:
            pass
        time.sleep(2)
    check.require(ready, "the restarted service reports ready")

    recovered = {j: api.job(j) for j in submitted}
    for job_id in sorted(observed["running"] | observed["queued"]):
        job = recovered[job_id]
        check.require(
            job["status"] in ("interrupted", "succeeded", "failed"),
            f"job {job_id[:8]} reached a terminal state after restart",
            job["status"],
        )
    interrupted = [
        j for j in recovered.values()
        if j["status"] == "interrupted" and j["error_code"] == "process_restart"
    ]
    check.require(
        interrupted,
        "orphaned jobs were terminally recovered as interrupted/process_restart",
        f"{len(interrupted)} of {len(recovered)}",
    )
    recovered_queued = [
        j for j in recovered.values()
        if j["job_id"] in observed["queued"] and j["status"] == "interrupted"
    ]
    recovered_running = [
        j for j in recovered.values()
        if j["job_id"] in observed["running"] and j["status"] == "interrupted"
    ]
    check.require(
        recovered_queued, "a job that was queued was recovered",
        f"{[j['job_id'][:8] for j in recovered_queued]}",
    )
    check.require(
        recovered_running, "a job that was running was recovered",
        f"{[j['job_id'][:8] for j in recovered_running]}",
    )
    check.require(
        not any(j["status"] in ("queued", "running") for j in recovered.values()),
        "no job was left orphaned in an active state",
    )

    preserved_after = api.report_json(pid, preserved_analysis_id)
    check.equal(
        preserved_after, preserved_before,
        "a previously published result survived the restart byte-identically",
    )
    # An interrupted identity must not be served as a cache hit.
    for job in interrupted[:1]:
        listing = api.list_analyses(pid)
        published = {a["analysis_id"] for a in listing["analyses"]}
        check.require(
            job["analysis_id"] not in published,
            "an interrupted analysis was not published as a result",
        )
    check.fact("old_pods", old_pods)
    check.fact("new_pod", new_pod)
    check.fact("interrupted_jobs", [j["job_id"] for j in interrupted])
    check.fact("observed_queued", sorted(observed["queued"]))
    check.fact("observed_running", sorted(observed["running"]))


def scenario_11_determinism(check: Check, shared: dict) -> None:
    """Canonical JSON/HTML determinism per the documented identity rules."""
    api, state = shared["api"], shared["state"]
    pid = state["project_id"]
    target = shared.get("determinism_target") or shared.get("baseline")
    check.require(target is not None, "an earlier analysis is available to re-read")
    analysis_id = target["response"]["analysis_id"]

    a = api.report_json(pid, analysis_id)
    b = api.report_json(pid, analysis_id)
    check.equal(
        json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True),
        "the JSON report is byte-stable across reads",
    )

    # Documented identity rule: `created_at`/`completed_at`/`analysis_id` are
    # display metadata and excluded from canonical equality; everything else,
    # including the cache key, must match for identical inputs.
    volatile = {"analysis_id", "created_at", "completed_at", "provenance"}
    canonical = {k: v for k, v in a.items() if k not in volatile}
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    # Re-request the identical inputs: it must be a cache hit on the same result.
    repeat = api.create_analysis(pid, target["response"].get("_request") or body(
        state,
        gp="gp_v2" if shared.get("determinism_target") else "gp_v1",
        audio=("wav_in_phase",) if shared.get("determinism_target") else (),
    ))
    check.equal(repeat.status_code, 200, "the repeat request is accepted")
    check.require(repeat.json()["cache_hit"], "identical inputs are a cache hit")
    check.equal(
        repeat.json()["analysis_id"], analysis_id,
        "the cache hit resolves to the same analysis",
    )

    again = api.report_json(pid, analysis_id)
    canonical_again = {k: v for k, v in again.items() if k not in volatile}
    check.equal(
        hashlib.sha256(
            json.dumps(canonical_again, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        digest,
        "canonical content is unchanged by re-requesting the same inputs",
    )

    check.require(
        a["mappings"] == again["mappings"], "mapping order and content are stable"
    )
    check.require(
        [m["index"] for m in a["source_measures"]]
        == sorted(m["index"] for m in a["source_measures"]),
        "source measures are ordered deterministically",
    )
    check.require(
        [m["measure_index"] for m in a["gp_measures"]]
        == sorted(m["measure_index"] for m in a["gp_measures"]),
        "GP measures are ordered deterministically",
    )
    raw = json.dumps(a)
    for forbidden in ("NaN", "Infinity", "-Infinity"):
        check.require(forbidden not in raw, f"the report contains no {forbidden}")

    html_a = api.report_html(pid, analysis_id)
    html_b = api.report_html(pid, analysis_id)
    # The HTML carries a render timestamp by design; strip the meta block before
    # comparing, then require the rest to be identical.
    def strip_generated(text: str) -> str:
        return "\n".join(
            line for line in text.splitlines() if "Generated:" not in line
        )

    check.equal(
        hashlib.sha256(strip_generated(html_a).encode()).hexdigest(),
        hashlib.sha256(strip_generated(html_b).encode()).hexdigest(),
        "the HTML report is stable apart from its render timestamp",
    )
    for leak in ("/var/lib/", "Traceback", "blob_relpath", "SECRET"):
        check.require(leak not in html_a, f"the HTML report leaks no {leak!r}")
    check.fact("canonical_content_sha256", digest)
    check.fact(
        "html_sha256_without_timestamp",
        hashlib.sha256(strip_generated(html_a).encode()).hexdigest(),
    )
    check.fact("analysis_id", analysis_id)


def scenario_12_flask_ui(check: Check, shared: dict) -> None:
    """The deployed Flask Project page completes the real HTTP workflow."""
    api, state = shared["api"], shared["state"]
    pid = state["project_id"]

    health = api.flask_get("/healthz")
    check.equal(health.status_code, 200, "the deployed Flask UI answers /healthz")
    check.equal(health.json()["status"], "ok", "and reports itself healthy")

    home = api.flask_get("/")
    check.equal(home.status_code, 200, "the converter home page still renders")

    listing = api.flask_get("/projects")
    check.equal(listing.status_code, 200, "the Projects list renders over HTTP")

    page = api.flask_get(f"/projects/{pid}")
    check.equal(page.status_code, 200, "the Project page renders over HTTP")
    html = page.text
    check.require("Reference-Time" in html, "the Project page shows the analysis section")
    check.require(
        'name="gp_link_id"' in html, "the deployed page offers a GP revision selector"
    )
    check.require(
        'name="midi_link_ids"' in html, "the deployed page offers source MIDI selection"
    )
    check.require(
        'name="audio_link_ids"' in html, "the deployed page offers audio selection"
    )
    check.require(
        'name="structure_link_id"' in html,
        "the deployed page offers structure selection",
    )
    check.require(
        state["shas"]["gp_v1"][:12] in html,
        "the deployed page shows the trusted GP digest",
    )

    # Drive the real form over HTTP with a fresh identity, so this is a genuine
    # end-to-end submit rather than a cache hit.
    ui_midi = make_midi(n_measures=8, tag=f"ui-{uuid.uuid4().hex[:8]}", pitch=67)
    ui_link = api.upload(
        pid, "suno-midi.other", f"e2e-{RUN_ID}-ui.mid", ui_midi
    )["link_id"]

    submit = api.flask_post(
        f"/projects/{pid}/analyze",
        data=[
            ("gp_link_id", state["links"]["gp_v1"]),
            ("gp_sha", state["shas"]["gp_v1"]),
            ("midi_link_ids", ui_link),
            ("audio_link_ids", state["links"]["wav_in_phase"]),
            ("structure_link_id", ""),
        ],
    )
    check.equal(submit.status_code, 200, "the form submit is accepted and redirects")
    check.require(
        "Анализ запущен" in submit.text or "cache hit" in submit.text,
        "the page confirms the analysis was started",
        submit.text[:200] if submit.status_code != 200 else "",
    )

    # Find the job the UI created and poll it through the UI's own endpoint.
    jobs = api.list_analyses(pid)["jobs"]
    ui_jobs = [j for j in jobs if j["status"] in ("queued", "running", "succeeded")]
    check.require(ui_jobs, "the UI submit created a job")
    newest = ui_jobs[0]

    status = api.flask_get(f"/projects/{pid}/analyses/{newest['job_id']}/status")
    check.equal(status.status_code, 200, "the UI status endpoint answers")
    check.require(
        status.json().get("status"), "the UI status endpoint returns a job status",
        json.dumps(status.json())[:200],
    )

    terminal = api.await_job(newest["job_id"])
    check.equal(terminal["status"], "succeeded", "the UI-started analysis succeeds")

    report_html = api.flask_get(
        f"/projects/{pid}/analyses/{terminal['analysis_id']}/report"
    )
    check.equal(report_html.status_code, 200, "the UI proxies the HTML report")
    check.require(
        "Reference-Time Analysis Report" in report_html.text,
        "the proxied HTML report is the real report",
    )
    report_json = api.flask_get(
        f"/projects/{pid}/analyses/{terminal['analysis_id']}/report.json"
    )
    check.equal(report_json.status_code, 200, "the UI proxies the JSON report")
    check.equal(
        report_json.json()["project_id"], pid, "the proxied JSON report is for this project"
    )

    refreshed = api.flask_get(f"/projects/{pid}")
    check.equal(refreshed.status_code, 200, "the Project page renders after completion")
    check.require(
        "Результаты" in refreshed.text, "prior analyses are listed"
    )
    check.require(
        "/report.json" in refreshed.text, "report links are offered"
    )
    check.fact("ui_job_id", newest["job_id"])
    check.fact("ui_analysis_id", terminal["analysis_id"])
    check.fact("flask_url", f"{api.flask} (Host: {api.flask_host})")


def scenario_13_no_mutation(check: Check, shared: dict) -> None:
    """Source assets are unchanged and no permanent copy remains on Flask storage."""
    api, state = shared["api"], shared["state"]
    cluster: Cluster = shared["cluster"]
    pid = state["project_id"]

    for key in ("gp_v1", "gp_v2", "midi_a", "midi_b", "wav_in_phase", "structure_ok"):
        data = api.download(pid, state["links"][key])
        check.equal(
            sha256_hex(data), state["shas"][key],
            f"{key} still hashes to the digest recorded at upload",
        )
        check.equal(
            sha256_hex(data), sha256_hex(state["fixtures"][key]),
            f"{key} is byte-identical to the fixture that was generated",
        )

    project = api.get_project(pid)
    listed = {a["id"]: a["asset_sha256"] for a in project["assets"]}
    for key in ("gp_v1", "midi_a", "wav_in_phase"):
        check.equal(
            listed[state["links"][key]], state["shas"][key],
            f"{key} metadata digest is unchanged",
        )

    available, detail = cluster.available()
    check.require(available, "cluster access is available to inspect Flask storage", detail)

    # The UI streams uploads straight through to the Asset API; nothing from the
    # project may be left under its converter session root.
    listing = cluster.exec_in(
        "gpmidi-web",
        ["sh", "-c", "find /var/lib/gpmidi -type f | head -200 || true"],
    )
    files = [f for f in listing.splitlines() if f.strip()]
    check.fact("gpmidi_web_files", files[:50])
    offenders = [
        f for f in files
        if f.lower().endswith((".wav", ".flac", ".mid", ".midi", ".gp", ".gp5", ".gpx"))
    ]
    check.require(
        not offenders,
        "no audio/MIDI/GP file is persisted on Flask storage",
        f"{offenders[:10]}",
    )
    for key in ("gp_v1", "midi_a", "wav_in_phase"):
        name = state["filenames"][key]
        check.require(
            not any(name in f for f in files),
            f"no copy of {name} remains on Flask storage",
        )

    # The analyzer's own temporary download scopes must be cleaned up too.
    rt_tmp = cluster.exec_in(
        "reference-time",
        ["sh", "-c", "ls -1 /var/lib/reference-time/tmp 2>/dev/null | head -50 || true"],
    )
    leftovers = [line for line in rt_tmp.splitlines() if line.strip()]
    check.require(
        not leftovers,
        "the analyzer left no temporary download scope behind",
        f"{leftovers[:10]}",
    )
    check.fact("reference_time_tmp_entries", leftovers)


def scenario_14_neighbours_healthy(check: Check, shared: dict) -> None:
    """Existing services, Project data, GPU profile and active LLM are unharmed."""
    api = shared["api"]
    baseline = shared["environment_before"]

    for name, url, expect in [
        ("asset-api health", f"{api.asset}/healthz", 200),
        ("asset-api readiness", f"{api.asset}/readyz", 200),
        ("asset-api projects", f"{api.asset}/v1/projects", 200),
        ("reference-time health", f"{api.rt}/healthz", 200),
        ("reference-time readiness", f"{api.rt}/readyz", 200),
    ]:
        resp = api.plain(url)
        check.equal(resp.status_code, expect, f"{name} responds {expect}")

    # Phase 1 project data must still be readable and unchanged in count.
    projects = api.plain(f"{api.asset}/v1/projects").json()["projects"]
    check.require(
        len(projects) >= baseline["project_count"],
        "no pre-existing Project disappeared",
        f"before {baseline['project_count']}, now {len(projects)}",
    )
    for pre in baseline["preexisting_projects"]:
        still = api.plain(f"{api.asset}/v1/projects/{pre['id']}")
        check.equal(
            still.status_code, 200, f"pre-existing project {pre['id'][:8]} still readable"
        )
        check.equal(
            len(still.json()["assets"]), pre["asset_count"],
            f"project {pre['id'][:8]} kept all its assets",
        )

    for name, url in [
        ("homepage", baseline["homepage_url"]),
        ("Gitea", baseline["gitea_url"]),
    ]:
        resp = api.plain(url, timeout=20)
        check.require(
            resp.status_code < 400, f"{name} is healthy", f"HTTP {resp.status_code}"
        )

    stats = api.plain(baseline["stats_url"], timeout=20)
    check.equal(stats.status_code, 200, "the AILab stats API responds")
    now = stats.json()
    active_now = sorted(
        k for k, v in (now.get("profiles") or {}).items() if _profile_active(v)
    )
    check.equal(
        active_now, baseline["active_profiles"],
        "the active GPU profile set is unchanged",
    )
    check.require(
        now.get("gpu") is not None, "the GPU is still reported by the stats API"
    )
    if baseline["llm_url"]:
        llm = api.plain(baseline["llm_url"], timeout=20)
        check.require(
            llm.status_code < 500,
            "the active LLM endpoint still answers",
            f"HTTP {llm.status_code}",
        )
        check.fact("llm_status_code", llm.status_code)

    converter = api.flask_get("/")
    check.equal(converter.status_code, 200, "the existing converter UI still renders")
    check.fact("active_profiles_before", baseline["active_profiles"])
    check.fact("active_profiles_after", active_now)
    check.fact("gpu_after", now.get("gpu"))


def _profile_active(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("active", "running", "on", "up")
    if isinstance(value, dict):
        for key in ("active", "running", "status", "state"):
            if key in value:
                return _profile_active(value[key])
    return False


def capture_environment(api: Api, args) -> dict:
    """Record the state that must be unchanged afterwards."""
    projects = api.plain(f"{api.asset}/v1/projects").json()["projects"]
    detailed = []
    for p in projects[: args.preexisting_sample]:
        full = api.plain(f"{api.asset}/v1/projects/{p['id']}")
        if full.status_code == 200:
            detailed.append({"id": p["id"], "asset_count": len(full.json()["assets"])})

    stats_url = f"{args.homepage_url.rstrip('/')}/api/stats"
    active: list[str] = []
    gpu = None
    try:
        stats = api.plain(stats_url, timeout=20).json()
        active = sorted(
            k for k, v in (stats.get("profiles") or {}).items() if _profile_active(v)
        )
        gpu = stats.get("gpu")
    except (requests.RequestException, ValueError) as e:
        print(f"  ! could not read {stats_url}: {e}")

    return {
        "project_count": len(projects),
        "preexisting_projects": detailed,
        "homepage_url": args.homepage_url,
        "gitea_url": args.gitea_url,
        "stats_url": stats_url,
        "llm_url": args.llm_url,
        "active_profiles": active,
        "gpu_before": gpu,
    }


SCENARIOS = [
    (1, "gp_grid", "GP parses to the intended grid with notes, marker and repeats",
     scenario_01_gp_grid),
    (2, "consensus", "Two agreeing MIDIs produce deterministic consensus",
     scenario_02_consensus),
    (3, "conflict", "Conflicting MIDI yields regional conflict and lower confidence",
     scenario_03_conflict),
    (4, "audio", "Audio clicks measurably corroborate or contradict MIDI phase",
     scenario_04_audio),
    (5, "anchor", "A valid structure anchor changes or locks the mapping",
     scenario_05_anchor),
    (6, "anchor_conflict", "Conflicting anchors fail with a stable error code",
     scenario_06_conflicting_anchor),
    (7, "gaps", "Source-gap and GP-gap cases produce explicit records",
     scenario_07_gaps),
    (8, "cold_cache", "Concurrent cold-cache requests admit one computation",
     scenario_08_cold_cache_concurrency),
    (9, "gp_only_revision", "GP-only revision change reuses persisted source evidence",
     scenario_09_gp_only_revision),
    (10, "restart", "Pod restart preserves results and recovers queued and running jobs",
     scenario_10_restart),
    (11, "determinism", "Canonical JSON/HTML determinism per the identity rules",
     scenario_11_determinism),
    (12, "flask_ui", "The deployed Flask Project page completes the HTTP workflow",
     scenario_12_flask_ui),
    (13, "no_mutation", "Sources unchanged and no permanent copy on Flask storage",
     scenario_13_no_mutation),
    (14, "neighbours", "Existing services, Project data, GPU and LLM unharmed",
     scenario_14_neighbours_healthy),
]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=os.environ.get("E2E_BASE_URL", "https://192.168.30.2"))
    p.add_argument("--flask-url", default=os.environ.get("E2E_FLASK_URL", "https://192.168.30.2"))
    p.add_argument("--flask-host", default=os.environ.get("E2E_FLASK_HOST", "gpmidi.ailab.local"))
    p.add_argument("--homepage-url", default=os.environ.get("E2E_HOMEPAGE_URL", "http://192.168.30.2"))
    p.add_argument("--gitea-url", default=os.environ.get("E2E_GITEA_URL", "http://192.168.30.2:3300"))
    p.add_argument("--llm-url", default=os.environ.get("E2E_LLM_URL", "http://192.168.30.2:8080/health"))
    p.add_argument("--namespace", default=os.environ.get("E2E_NAMESPACE", "gpmidi-ml"))
    p.add_argument("--kubectl", default=os.environ.get("E2E_KUBECTL", "kubectl"))
    p.add_argument("--timeout", type=float, default=float(os.environ.get("E2E_TIMEOUT", "60")))
    p.add_argument("--preexisting-sample", type=int, default=3)
    p.add_argument("--output", default=os.environ.get("E2E_OUTPUT", ""))
    p.add_argument("--only", default="", help="comma-separated scenario ids to run")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    api = Api(args.base_url, args.flask_url, args.flask_host, args.timeout)
    cluster = Cluster(args.kubectl, args.namespace)
    runner = Runner()

    print(f"Live acceptance run {RUN_ID}")
    print(f"  reference-time : {api.rt}")
    print(f"  asset-api      : {api.asset}")
    print(f"  flask UI       : {api.flask} (Host: {api.flask_host})")
    print(f"  namespace      : {args.namespace}")

    config = {
        "base_url": args.base_url,
        "flask_url": args.flask_url,
        "flask_host": args.flask_host,
        "namespace": args.namespace,
        "homepage_url": args.homepage_url,
        "gitea_url": args.gitea_url,
        "llm_url": args.llm_url,
    }

    try:
        environment_before = capture_environment(api, args)
        state = setup(api)
    except Exception as e:  # noqa: BLE001 - setup failure must still be reported
        print(f"FATAL: setup failed: {type(e).__name__}: {e}")
        traceback.print_exc(limit=10)
        evidence = {
            "evidence_version": "2",
            "run_id": RUN_ID,
            "started_at": STARTED_AT,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "config": config,
            "overall_status": "fail",
            "setup_error": f"{type(e).__name__}: {e}",
            "scenarios": [],
        }
        _write(args.output, evidence)
        return 1

    print(f"\nProject: {state['project_id']}")
    print(f"Uploaded {len(state['links'])} generated fixtures")

    runner.shared.update(
        api=api, cluster=cluster, state=state, environment_before=environment_before
    )

    wanted = (
        {int(x) for x in args.only.split(",") if x.strip()} if args.only else None
    )
    for scenario_id, key, title, fn in SCENARIOS:
        if wanted is not None and scenario_id not in wanted:
            continue
        runner.run(scenario_id, key, title, fn)

    evidence = runner.evidence(config)
    evidence["project_id"] = state["project_id"]
    evidence["asset_link_ids"] = state["links"]
    evidence["asset_sha256s"] = state["shas"]
    evidence["environment_before"] = environment_before
    _write(args.output, evidence)

    print("\n================ SUMMARY ================")
    for r in runner.results:
        failed_obs = [o for o in r.observations if not o.ok]
        print(f"  E2E-{r.id:02d} {r.status.upper():4}  {r.title}")
        if r.error:
            print(f"           error: {r.error}")
        for o in failed_obs:
            print(f"           failed check: {o.description} — {o.detail}")
    print(
        f"\n{evidence['passed']}/{evidence['scenario_count']} scenarios passed; "
        f"overall {evidence['overall_status'].upper()}"
    )

    return 0 if evidence["overall_status"] == "pass" else 1


def _write(path: str, evidence: dict) -> None:
    payload = json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False)
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        print(f"\nEvidence written to {path}")
    else:
        print("\n--- evidence ---")
        print(payload)


if __name__ == "__main__":
    sys.exit(main())
