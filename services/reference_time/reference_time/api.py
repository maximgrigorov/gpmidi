"""FastAPI application for reference-time analysis.

Versioned API under /reference-time/v1.
"""

from __future__ import annotations

import io
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import __version__
from .cache import compute_cache_key, compute_source_evidence_cache_key
from .config import (
    ASSET_API_URL,
    CORS_ORIGINS,
    DATA_ROOT,
    DB_PATH,
    JOB_TIMEOUT_SECONDS,
    MAX_CONCURRENT_JOBS,
    MAX_QUEUE_LENGTH,
)
from .consensus import build_consensus
from .database import AnalysisDB
from .gp_grid import extract_gp_grid
from .mapping import DEFAULT_PARAMS, align_measures
from .midi_tempo import build_source_measures, extract_tempo_evidence
from .models import JobStatus, ReferenceTimeAnalysis
from .report import generate_html_report, generate_json_report

logger = logging.getLogger(__name__)

db: Optional[AnalysisDB] = None
executor: Optional[ThreadPoolExecutor] = None
_running_jobs: set[str] = set()
_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, executor
    os.makedirs(os.path.join(DATA_ROOT, "db"), exist_ok=True)
    db = AnalysisDB(DB_PATH)
    db.recover_interrupted_jobs()
    executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS)
    logger.info("Reference-time service started, version %s", __version__)
    yield
    executor.shutdown(wait=False)
    db.close()


app = FastAPI(
    title="Reference-Time Analysis",
    version=__version__,
    root_path="/reference-time",
    lifespan=lifespan,
)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


def _request_id() -> str:
    return uuid.uuid4().hex[:12]


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    rid = _request_id()
    logger.exception("Unhandled error [%s]", rid)
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": "internal_error",
                "message": "An unexpected error occurred",
                "request_id": rid,
            }
        },
        headers={"X-Request-ID": rid},
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    try:
        db.count_active_jobs()
    except Exception:
        raise HTTPException(status_code=503, detail="Database check failed")
    return {"status": "ready"}


class AnalysisRequest(BaseModel):
    gp_revision_sha256: str
    gp_asset_link_id: str
    source_midi_link_ids: list[str]
    audio_link_ids: list[str] = []
    structure_link_id: Optional[str] = None


class AnalysisResponse(BaseModel):
    job_id: str
    analysis_id: str
    status: str
    cache_hit: bool = False
    request_id: str


def _fetch_asset_bytes(project_id: str, link_id: str) -> bytes:
    """Fetch asset bytes from asset-api."""
    url = f"{ASSET_API_URL}/asset-api/v1/projects/{project_id}/assets/{link_id}/download"
    with httpx.Client(timeout=60.0, verify=False) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def _fetch_asset_info(project_id: str, link_id: str) -> dict:
    """Fetch asset link metadata from asset-api."""
    url = f"{ASSET_API_URL}/asset-api/v1/projects/{project_id}/assets"
    with httpx.Client(timeout=30.0, verify=False) as client:
        resp = client.get(url)
        resp.raise_for_status()
        assets = resp.json().get("assets", [])
        for a in assets:
            if a.get("link_id") == link_id:
                return a
    raise ValueError(f"Asset link {link_id} not found in project {project_id}")


def _validate_project_assets(
    project_id: str,
    link_ids: list[str],
) -> dict[str, dict]:
    """Validate that all requested asset links belong to the project."""
    url = f"{ASSET_API_URL}/asset-api/v1/projects/{project_id}/assets"
    with httpx.Client(timeout=30.0, verify=False) as client:
        resp = client.get(url)
        resp.raise_for_status()
        assets = resp.json().get("assets", [])

    asset_map = {a["link_id"]: a for a in assets}
    for lid in link_ids:
        if lid not in asset_map:
            raise ValueError(
                f"Asset link {lid} does not belong to project {project_id}"
            )
    return asset_map


def _run_analysis(
    job_id: str,
    analysis_id: str,
    project_id: str,
    request: AnalysisRequest,
    cache_key: str,
):
    """Execute analysis in background thread."""
    try:
        with _lock:
            _running_jobs.add(job_id)

        db.update_job_status(job_id, "running", "validating", "Validating inputs")

        all_link_ids = [request.gp_asset_link_id] + request.source_midi_link_ids + request.audio_link_ids
        if request.structure_link_id:
            all_link_ids.append(request.structure_link_id)

        try:
            asset_map = _validate_project_assets(project_id, all_link_ids)
        except ValueError as e:
            db.update_job_status(
                job_id, "failed",
                error_code="asset_validation_failed",
                error_message=str(e),
            )
            return

        db.update_job_status(job_id, "running", "gp_parse", "Parsing Guitar Pro")

        gp_info = asset_map[request.gp_asset_link_id]
        gp_bytes = _fetch_asset_bytes(project_id, request.gp_asset_link_id)
        gp_sha = gp_info.get("sha256", request.gp_revision_sha256)
        original_name = gp_info.get("original_filename", "")

        try:
            gp_measures = extract_gp_grid(gp_bytes, gp_sha, original_name)
        except ValueError as e:
            db.update_job_status(
                job_id, "failed",
                error_code="gp_parse_failed",
                error_message=str(e),
            )
            return

        db.update_job_status(job_id, "running", "midi_extract", "Extracting MIDI tempo maps")

        source_evidence_list = []
        for mid_link_id in request.source_midi_link_ids:
            mid_info = asset_map[mid_link_id]
            mid_bytes = _fetch_asset_bytes(project_id, mid_link_id)
            try:
                evidence = extract_tempo_evidence(
                    mid_bytes,
                    mid_link_id,
                    mid_info.get("sha256", ""),
                )
                source_evidence_list.append(evidence)
            except ValueError as e:
                db.update_job_status(
                    job_id, "failed",
                    error_code="midi_parse_failed",
                    error_message=f"Failed to parse MIDI {mid_link_id}: {e}",
                )
                return

        db.update_job_status(job_id, "running", "consensus", "Building MIDI consensus")

        try:
            consensus = build_consensus(source_evidence_list)
        except ValueError as e:
            db.update_job_status(
                job_id, "failed",
                error_code="consensus_failed",
                error_message=str(e),
            )
            return

        primary_evidence = None
        for ev in source_evidence_list:
            if ev.sha256 == consensus.primary_sha256:
                primary_evidence = ev
                break
        if primary_evidence is None:
            primary_evidence = source_evidence_list[0]

        db.update_job_status(job_id, "running", "measures", "Building source measures")
        source_measures = build_source_measures(primary_evidence)

        db.update_job_status(job_id, "running", "alignment", "Aligning source to GP")

        alignment = align_measures(source_measures, gp_measures, params=DEFAULT_PARAMS.copy())

        input_identities = {
            "gp_revision": gp_sha,
        }
        for ev in source_evidence_list:
            input_identities[f"source_midi_{ev.asset_link_id}"] = ev.sha256

        processor_versions = {
            "reference_time": __version__,
            "mido": primary_evidence.parser_version,
        }

        analysis = ReferenceTimeAnalysis(
            analysis_id=analysis_id,
            project_id=project_id,
            gp_revision_sha256=gp_sha,
            processor_versions=processor_versions,
            parameters=DEFAULT_PARAMS,
            input_identities=input_identities,
            source_evidence=source_evidence_list,
            midi_consensus=consensus,
            source_measures=source_measures,
            gp_measures=gp_measures,
            mappings=alignment.mappings,
            global_confidence=alignment.global_confidence,
            global_warnings=alignment.warnings,
            cache_key=cache_key,
        )

        result_json = generate_json_report(analysis)
        db.store_result(analysis_id, project_id, cache_key, result_json)
        db.update_job_status(job_id, "succeeded", "done", "Analysis complete")

    except Exception as e:
        logger.exception("Analysis failed [%s]", job_id)
        try:
            db.update_job_status(
                job_id, "failed",
                error_code="analysis_error",
                error_message=f"Internal analysis error: {type(e).__name__}",
            )
        except Exception:
            pass
    finally:
        with _lock:
            _running_jobs.discard(job_id)


@app.post("/v1/projects/{project_id}/analyses")
async def create_analysis(project_id: str, request: AnalysisRequest):
    rid = _request_id()

    if not request.source_midi_link_ids:
        raise HTTPException(
            status_code=422,
            detail={"code": "no_source_midi", "message": "At least one source MIDI is required", "request_id": rid},
        )

    source_midi_sha256s = []
    audio_sha256s = []
    structure_sha256 = None

    try:
        all_ids = [request.gp_asset_link_id] + request.source_midi_link_ids + request.audio_link_ids
        if request.structure_link_id:
            all_ids.append(request.structure_link_id)
        asset_map = _validate_project_assets(project_id, all_ids)

        for mid_id in request.source_midi_link_ids:
            source_midi_sha256s.append(asset_map[mid_id].get("sha256", ""))
        for aud_id in request.audio_link_ids:
            audio_sha256s.append(asset_map[aud_id].get("sha256", ""))
        if request.structure_link_id:
            structure_sha256 = asset_map[request.structure_link_id].get("sha256")

    except (ValueError, httpx.HTTPError) as e:
        raise HTTPException(
            status_code=422,
            detail={"code": "asset_validation_failed", "message": str(e), "request_id": rid},
        )

    processor_versions = {
        "reference_time": __version__,
    }
    cache_key_val = compute_cache_key(
        request.gp_revision_sha256,
        source_midi_sha256s,
        audio_sha256s,
        structure_sha256,
        processor_versions,
        DEFAULT_PARAMS,
    )

    cached = db.find_cached_result(cache_key_val)
    if cached:
        return AnalysisResponse(
            job_id="",
            analysis_id=cached["analysis_id"],
            status="succeeded",
            cache_hit=True,
            request_id=rid,
        )

    with _lock:
        active = db.count_active_jobs()
        if active >= MAX_QUEUE_LENGTH:
            raise HTTPException(
                status_code=429,
                detail={"code": "queue_full", "message": "Analysis queue is full", "request_id": rid},
            )

    job_id = str(uuid.uuid4())
    analysis_id = str(uuid.uuid4())

    db.create_job(job_id, analysis_id, project_id, cache_key_val)
    executor.submit(_run_analysis, job_id, analysis_id, project_id, request, cache_key_val)

    return AnalysisResponse(
        job_id=job_id,
        analysis_id=analysis_id,
        status="queued",
        cache_hit=False,
        request_id=rid,
    )


@app.get("/v1/projects/{project_id}/analyses")
async def list_analyses(project_id: str):
    results = db.get_results_for_project(project_id)
    jobs = db.get_jobs_for_project(project_id)
    return {
        "analyses": results,
        "jobs": jobs,
    }


@app.get("/v1/projects/{project_id}/analyses/{analysis_id}")
async def get_analysis(project_id: str, analysis_id: str):
    result = db.get_result(analysis_id)
    if not result or result["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return {"analysis_id": analysis_id, "project_id": project_id, "cache_key": result["cache_key"]}


@app.get("/v1/projects/{project_id}/analyses/{analysis_id}/report.json")
async def get_json_report(project_id: str, analysis_id: str):
    result = db.get_result(analysis_id)
    if not result or result["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return JSONResponse(
        content=__import__("json").loads(result["result_json"]),
        media_type="application/json",
    )


@app.get("/v1/projects/{project_id}/analyses/{analysis_id}/report.html")
async def get_html_report(project_id: str, analysis_id: str):
    result = db.get_result(analysis_id)
    if not result or result["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Analysis not found")

    import json
    analysis_data = json.loads(result["result_json"])
    analysis = ReferenceTimeAnalysis(**analysis_data)
    html_content = generate_html_report(analysis)
    return HTMLResponse(content=html_content)


@app.get("/v1/jobs/{job_id}")
async def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    safe_job = {
        "job_id": job["job_id"],
        "analysis_id": job["analysis_id"],
        "project_id": job["project_id"],
        "status": job["status"],
        "progress_phase": job["progress_phase"],
        "progress_message": job["progress_message"],
        "error_code": job["error_code"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }
    return safe_job
