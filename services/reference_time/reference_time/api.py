"""FastAPI application for reference-time analysis.

Versioned API under /reference-time/v1.

Orchestration contract
----------------------
* Nothing the caller sends is trusted. Inputs are resolved against Asset API
  metadata (`validation.resolve_analysis_inputs`) *before* a job or cache entry
  can exist, so a validation failure creates no state at all.
* Assets are streamed to bounded per-analysis temporary storage
  (`assets.AssetClient` + `assets.download_scope`) and the whole scope is removed
  on success, failure, timeout and cancellation.
* Source-side evidence is persisted separately from full analyses, so a GP-only
  revision change reuses it and recomputes only GP extraction and mapping. The
  reuse is visible in `provenance`.
* A worker checks that its job is still active before every phase and publishes
  its result through an atomic guarded insert, so a timed-out job can never
  publish late.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import __version__
from .assets import (
    AssetClient,
    AssetFetchError,
    DownloadLimits,
    download_scope,
    purge_stale_scopes,
)
from .audio_evidence import apply_audio_evidence_to_measures, extract_audio_evidence
from .cache import canonical_json, compute_cache_key, compute_source_evidence_key
from .config import (
    ASSET_API_URL,
    AUDIO_MAX_DOWNBEATS_PERSISTED,
    AUDIO_MAX_ONSETS_PERSISTED,
    AUDIO_MAX_READ_SECONDS,
    CONNECT_TIMEOUT_SECONDS,
    CORS_ORIGINS,
    DATA_ROOT,
    DB_PATH,
    JOB_TIMEOUT_SECONDS,
    MAX_AUDIO_BYTES,
    MAX_AUDIO_DOWNLOAD_SECONDS,
    MAX_CONCURRENT_JOBS,
    MAX_GP_BYTES,
    MAX_GP_DOWNLOAD_SECONDS,
    MAX_MIDI_BYTES,
    MAX_MIDI_DOWNLOAD_SECONDS,
    MAX_QUEUE_LENGTH,
    MAX_STRUCTURE_BYTES,
    MAX_STRUCTURE_DOWNLOAD_SECONDS,
    METADATA_TIMEOUT_SECONDS,
    POOL_TIMEOUT_SECONDS,
    READ_TIMEOUT_SECONDS,
    TIMEOUT_WATCHDOG_INTERVAL_SECONDS,
    TMP_ROOT,
    WORKER_SHUTDOWN_GRACE_SECONDS,
    WRITE_TIMEOUT_SECONDS,
)
from .consensus import build_consensus, conflict_time_regions
from .database import AnalysisDB
from .gp_grid import extract_gp_grid
from .mapping import DEFAULT_PARAMS, align_measures
from .midi_tempo import build_source_measures, extract_tempo_evidence
from .models import (
    AnalysisProvenance,
    AudioEvidence,
    ConsensusDecision,
    MidiConsensus,
    ReferenceTimeAnalysis,
    SourceMeasure,
    SourceTempoEvidence,
    StructureSectionModel,
    Warning,
    WarningCode,
)
from .report import generate_html_report, generate_json_report
from .structure import StructureError, parse_structure_json
from .validation import AssetRef, AssetValidationError, resolve_analysis_inputs

logger = logging.getLogger(__name__)

SOURCE_EVIDENCE_BUNDLE_VERSION = "2"

db: AnalysisDB | None = None
executor: ThreadPoolExecutor | None = None
asset_client: AssetClient | None = None
_watchdog_stop = threading.Event()
_watchdog: threading.Thread | None = None

GP_LIMITS = DownloadLimits(max_bytes=MAX_GP_BYTES, max_seconds=MAX_GP_DOWNLOAD_SECONDS)
MIDI_LIMITS = DownloadLimits(
    max_bytes=MAX_MIDI_BYTES, max_seconds=MAX_MIDI_DOWNLOAD_SECONDS
)
AUDIO_LIMITS = DownloadLimits(
    max_bytes=MAX_AUDIO_BYTES, max_seconds=MAX_AUDIO_DOWNLOAD_SECONDS
)
STRUCTURE_LIMITS = DownloadLimits(
    max_bytes=MAX_STRUCTURE_BYTES, max_seconds=MAX_STRUCTURE_DOWNLOAD_SECONDS
)


def build_asset_client() -> AssetClient:
    return AssetClient(
        base_url=ASSET_API_URL,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        write_timeout=WRITE_TIMEOUT_SECONDS,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        metadata_timeout=METADATA_TIMEOUT_SECONDS,
    )


def processor_versions() -> dict[str, str]:
    """Exact code/algorithm/dependency identity that takes part in cache keys."""
    import mido
    import pydantic

    versions = {
        "reference_time": __version__,
        "mido": getattr(mido, "__version__", "unknown"),
        "pydantic": pydantic.VERSION,
        "alignment_params": str(DEFAULT_PARAMS["version"]),
    }
    try:
        import guitarpro

        versions["pyguitarpro"] = getattr(guitarpro, "__version__", "unknown")
    except ImportError:  # pragma: no cover - pinned in the image
        versions["pyguitarpro"] = "missing"
    try:
        import soundfile

        versions["soundfile"] = getattr(soundfile, "__version__", "unknown")
    except (ImportError, OSError):  # pragma: no cover
        versions["soundfile"] = "missing"
    return versions


def _watchdog_loop(interval: float) -> None:
    """Continuously enforce job timeouts while the service is alive."""
    while not _watchdog_stop.wait(interval):
        try:
            timed_out = db.enforce_timeouts() if db else []
            for job_id in timed_out:
                logger.warning("Job %s timed out and was failed by the watchdog", job_id)
        except Exception:  # noqa: BLE001 - the watchdog must never die
            logger.exception("Timeout watchdog iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, executor, asset_client, _watchdog
    os.makedirs(os.path.join(DATA_ROOT, "db"), exist_ok=True)
    os.makedirs(TMP_ROOT, exist_ok=True)
    purge_stale_scopes(TMP_ROOT)

    db = AnalysisDB(DB_PATH, job_timeout_seconds=JOB_TIMEOUT_SECONDS)
    recovered = db.recover_interrupted_jobs()
    logger.info(
        "Startup recovery: %d running and %d queued job(s) marked interrupted",
        recovered["running"],
        recovered["queued"],
    )
    db.enforce_timeouts()

    if asset_client is None:
        asset_client = build_asset_client()

    executor = ThreadPoolExecutor(
        max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="rt-worker"
    )
    _watchdog_stop.clear()
    _watchdog = threading.Thread(
        target=_watchdog_loop,
        args=(TIMEOUT_WATCHDOG_INTERVAL_SECONDS,),
        name="rt-timeout-watchdog",
        daemon=True,
    )
    _watchdog.start()

    logger.info("Reference-time service started, version %s", __version__)
    try:
        yield
    finally:
        _watchdog_stop.set()
        if _watchdog is not None:
            _watchdog.join(timeout=WORKER_SHUTDOWN_GRACE_SECONDS)
        if executor is not None:
            # Bounded shutdown: give in-flight work a grace period, then stop
            # accepting anything further.
            executor.shutdown(wait=True, cancel_futures=True)
        if db is not None:
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


def _error(status: int, code: str, message: str, rid: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message, "request_id": rid},
    )


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
    return {"status": "ok", "version": __version__}


@app.get("/readyz")
async def readyz():
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    try:
        db.count_active_jobs()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=503, detail="Database check failed") from None
    return {"status": "ready"}


class AnalysisRequest(BaseModel):
    gp_asset_link_id: str
    source_midi_link_ids: list[str]
    # Optional assertion about the GP revision digest. It is *checked* against
    # Asset API metadata and never used as cache identity.
    gp_revision_sha256: str | None = None
    audio_link_ids: list[str] = []
    structure_link_id: str | None = None


class AnalysisResponse(BaseModel):
    job_id: str
    analysis_id: str
    status: str
    cache_hit: bool = False
    request_id: str


# --------------------------------------------------------------------------
# Source evidence bundle (persisted, reusable across GP revisions)
# --------------------------------------------------------------------------

def _serialize_source_bundle(
    source_evidence: list[SourceTempoEvidence],
    audio_evidence: list[AudioEvidence],
    source_measures: list[SourceMeasure],
    consensus: MidiConsensus,
) -> str:
    return canonical_json(
        {
            "bundle_version": SOURCE_EVIDENCE_BUNDLE_VERSION,
            "source_evidence": [e.model_dump(mode="json") for e in source_evidence],
            "audio_evidence": [e.model_dump(mode="json") for e in audio_evidence],
            "source_measures": [m.model_dump(mode="json") for m in source_measures],
            "consensus": consensus.model_dump(mode="json"),
        }
    )


def _deserialize_source_bundle(payload: str) -> tuple:
    data = json.loads(payload)
    if data.get("bundle_version") != SOURCE_EVIDENCE_BUNDLE_VERSION:
        raise ValueError("Unsupported source evidence bundle version")
    return (
        [SourceTempoEvidence(**e) for e in data["source_evidence"]],
        [AudioEvidence(**e) for e in data["audio_evidence"]],
        [SourceMeasure(**m) for m in data["source_measures"]],
        MidiConsensus(**data["consensus"]),
    )


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

def _build_summary(analysis: ReferenceTimeAnalysis) -> dict:
    """Compact, listing-sized view of one analysis.

    Persisted alongside the full report so the Project page can show confidence,
    warnings, trusted input hashes and reuse provenance without any client
    parsing a full report document.
    """
    mapping_types: dict[str, int] = {}
    for m in analysis.mappings:
        key = m.mapping_type.value
        mapping_types[key] = mapping_types.get(key, 0) + 1
    return {
        "schema_version": analysis.schema_version,
        "gp_revision_sha256": analysis.gp_revision_sha256,
        "gp_revision_number": analysis.gp_revision_number,
        "global_confidence": round(analysis.global_confidence, 4),
        "source_measure_count": len(analysis.source_measures),
        "gp_measure_count": len(analysis.gp_measures),
        "mapping_count": len(analysis.mappings),
        "mapping_type_counts": mapping_types,
        "warning_codes": sorted({w.code.value for w in analysis.global_warnings}),
        "consensus_decision": (
            analysis.midi_consensus.decision.value if analysis.midi_consensus else None
        ),
        "input_identities": analysis.input_identities,
        "source_evidence_reused": analysis.provenance.source_evidence_reused,
        "source_evidence_key": analysis.provenance.source_evidence_key,
        "audio_asset_count": len(analysis.audio_evidence),
        "structure_version": analysis.structure_version,
        "anchored_source_indices": list(analysis.anchored_source_indices),
    }


def _abandon_if_terminal(job_id: str, phase: str) -> bool:
    """True when the job is no longer ours to finish."""
    if db is None or db.is_job_active(job_id):
        return False
    logger.warning(
        "Job %s is no longer active (phase %s); abandoning without publishing",
        job_id,
        phase,
    )
    return True


def _run_analysis(job_id, analysis_id, project_id, resolved, cache_key, source_key):
    """Execute one analysis in a worker thread."""
    stats = {"midi": 0, "audio": 0, "gp": 0}
    try:
        if not db.update_job_status(job_id, "running", "starting", "Starting analysis"):
            logger.warning("Job %s was terminal before it started", job_id)
            return

        with download_scope(TMP_ROOT, job_id.replace("-", "")[:8]) as scope:
            # ---- source-side evidence: reuse when possible ----------------
            persisted = db.get_source_evidence(source_key, project_id)
            reused = False
            source_created_at: str | None = None
            audio_warnings: list[Warning] = []

            if persisted is not None:
                try:
                    (
                        source_evidence,
                        audio_evidence,
                        source_measures,
                        consensus,
                    ) = _deserialize_source_bundle(persisted["evidence_json"])
                    reused = True
                    source_created_at = persisted["created_at"]
                    for ev in audio_evidence:
                        audio_warnings.extend(ev.warnings)
                    logger.info(
                        "Reusing persisted source evidence %s for job %s",
                        source_key[:12],
                        job_id,
                    )
                except (ValueError, KeyError, TypeError):
                    logger.warning(
                        "Persisted source evidence %s is unreadable; recomputing",
                        source_key[:12],
                    )
                    persisted = None

            if persisted is None:
                if not db.update_job_status(
                    job_id, "running", "midi_extract", "Extracting MIDI tempo maps"
                ):
                    return
                source_evidence = []
                for asset in resolved.source_midi:
                    dest = os.path.join(scope.path, f"midi-{asset.link_id}.mid")
                    asset_client.download(project_id, asset.link_id, dest, MIDI_LIMITS)
                    with open(dest, "rb") as fh:
                        midi_bytes = fh.read()
                    stats["midi"] += 1
                    source_evidence.append(
                        extract_tempo_evidence(midi_bytes, asset.link_id, asset.sha256)
                    )

                if not db.update_job_status(
                    job_id, "running", "consensus", "Building MIDI consensus"
                ):
                    return
                consensus = build_consensus(source_evidence)

                primary = next(
                    (e for e in source_evidence if e.sha256 == consensus.primary_sha256),
                    source_evidence[0],
                )
                primary_path = os.path.join(
                    scope.path, f"midi-{primary.asset_link_id}.mid"
                )
                primary_bytes = None
                if os.path.exists(primary_path):
                    with open(primary_path, "rb") as fh:
                        primary_bytes = fh.read()

                if not db.update_job_status(
                    job_id, "running", "measures", "Building source measures"
                ):
                    return
                source_measures = build_source_measures(primary, midi_bytes=primary_bytes)

                # ---- bounded audio evidence -------------------------------
                audio_evidence = []
                if resolved.audio:
                    if not db.update_job_status(
                        job_id, "running", "audio", "Extracting audio evidence"
                    ):
                        return
                    for asset in resolved.audio:
                        dest = os.path.join(scope.path, f"audio-{asset.link_id}")
                        try:
                            asset_client.download(
                                project_id, asset.link_id, dest, AUDIO_LIMITS
                            )
                        except AssetFetchError as e:
                            audio_warnings.append(
                                Warning(
                                    code=WarningCode.AUDIO_DECODE_FAILED,
                                    message=f"Audio {asset.link_id}: {e.code}",
                                )
                            )
                            audio_evidence.append(
                                AudioEvidence(
                                    asset_link_id=asset.link_id,
                                    sha256=asset.sha256,
                                    role=asset.role,
                                    warnings=[
                                        Warning(
                                            code=WarningCode.AUDIO_DECODE_FAILED,
                                            message=f"Download failed: {e.code}",
                                        )
                                    ],
                                )
                            )
                            continue
                        ev = extract_audio_evidence(
                            dest,
                            asset.link_id,
                            asset.sha256,
                            role=asset.role,
                            max_seconds=AUDIO_MAX_READ_SECONDS,
                            max_onsets=AUDIO_MAX_ONSETS_PERSISTED,
                            max_downbeats=AUDIO_MAX_DOWNBEATS_PERSISTED,
                            estimated_bpm=(
                                source_measures[0].tempo_bpm if source_measures else None
                            ),
                        )
                        stats["audio"] += 1
                        audio_evidence.append(ev)
                        audio_warnings.extend(ev.warnings)

                if audio_evidence:
                    source_measures = apply_audio_evidence_to_measures(
                        source_measures, audio_evidence
                    )

                db.store_source_evidence(
                    source_key,
                    project_id,
                    _serialize_source_bundle(
                        source_evidence, audio_evidence, source_measures, consensus
                    ),
                )
                row = db.get_source_evidence(source_key, project_id)
                source_created_at = row["created_at"] if row else None

            if not resolved.audio:
                audio_warnings.append(
                    Warning(
                        code=WarningCode.AUDIO_MISSING,
                        message="No audio assets provided; confidence reduced",
                    )
                )

            # ---- GP grid: always recomputed ------------------------------
            if not db.update_job_status(
                job_id, "running", "gp_parse", "Parsing Guitar Pro"
            ):
                return
            gp_dest = os.path.join(scope.path, "input.gp")
            asset_client.download(
                project_id, resolved.gp.link_id, gp_dest, GP_LIMITS
            )
            with open(gp_dest, "rb") as fh:
                gp_bytes = fh.read()
            try:
                gp_measures = extract_gp_grid(
                    gp_bytes, resolved.gp.sha256, resolved.gp.original_filename
                )
            except ValueError as e:
                db.update_job_status(
                    job_id,
                    "failed",
                    error_code="gp_parse_failed",
                    error_message=str(e)[:500],
                )
                return
            stats["gp"] += 1

            # ---- structure / anchors -------------------------------------
            anchors = []
            sections: list[StructureSectionModel] = []
            structure_version = None
            if resolved.structure is not None:
                if not db.update_job_status(
                    job_id, "running", "structure", "Parsing structure"
                ):
                    return
                struct_dest = os.path.join(scope.path, "structure.json")
                asset_client.download(
                    project_id, resolved.structure.link_id, struct_dest, STRUCTURE_LIMITS
                )
                with open(struct_dest, "rb") as fh:
                    struct_bytes = fh.read()
                try:
                    parsed = parse_structure_json(
                        struct_bytes, source_measures, len(gp_measures)
                    )
                except StructureError as e:
                    db.update_job_status(
                        job_id,
                        "failed",
                        error_code=e.code,
                        error_message=e.message[:500],
                    )
                    return
                anchors = list(parsed.anchors)
                structure_version = parsed.version
                sections = [
                    StructureSectionModel(
                        label=s.label,
                        source_measure_index=s.source_measure_index,
                        gp_measure_index=s.gp_measure_index,
                    )
                    for s in parsed.sections
                ]

            # ---- alignment ----------------------------------------------
            if not db.update_job_status(
                job_id, "running", "alignment", "Aligning source to GP"
            ):
                return

            has_conflict = consensus.decision == ConsensusDecision.CONFLICT
            timeline_end = source_measures[-1].seconds_end if source_measures else 0.0
            regions = (
                conflict_time_regions(consensus, timeline_end) if has_conflict else []
            )
            try:
                alignment = align_measures(
                    source_measures,
                    gp_measures,
                    anchors=anchors or None,
                    params=DEFAULT_PARAMS.copy(),
                    consensus_has_conflict=has_conflict,
                    conflict_regions=regions,
                )
            except StructureError as e:
                db.update_job_status(
                    job_id, "failed", error_code=e.code, error_message=e.message[:500]
                )
                return

            provenance = AnalysisProvenance(
                source_evidence_key=source_key,
                source_evidence_reused=reused,
                source_evidence_created_at=source_created_at,
                source_midi_extractions=stats["midi"],
                audio_extractions=stats["audio"],
                gp_extractions=stats["gp"],
                dependency_versions=processor_versions(),
            )

            analysis = ReferenceTimeAnalysis(
                analysis_id=analysis_id,
                project_id=project_id,
                gp_revision_sha256=resolved.gp.sha256,
                gp_revision_number=resolved.gp_revision_number,
                processor_versions=processor_versions(),
                parameters=DEFAULT_PARAMS,
                input_identities=resolved.input_identities(),
                source_evidence=source_evidence,
                audio_evidence=audio_evidence,
                structure_version=structure_version,
                structure_sections=sections,
                anchored_source_indices=list(alignment.anchored_source_indices),
                midi_consensus=consensus,
                source_measures=source_measures,
                gp_measures=gp_measures,
                mappings=alignment.mappings,
                global_confidence=alignment.global_confidence,
                global_warnings=alignment.warnings + audio_warnings,
                provenance=provenance,
                cache_key=cache_key,
            )

            if _abandon_if_terminal(job_id, "publish"):
                return

            published = db.store_result(
                analysis_id,
                project_id,
                cache_key,
                generate_json_report(analysis),
                owning_job_id=job_id,
                summary_json=canonical_json(_build_summary(analysis)),
            )
            if not published:
                logger.warning(
                    "Job %s finished but is no longer active; result not published",
                    job_id,
                )
                return
            db.update_job_status(job_id, "succeeded", "done", "Analysis complete")

    except AssetFetchError as e:
        db.update_job_status(
            job_id, "failed", error_code=e.code, error_message=e.message[:500]
        )
    except AssetValidationError as e:
        db.update_job_status(
            job_id, "failed", error_code=e.code, error_message=e.message[:500]
        )
    except ValueError as e:
        logger.warning("Analysis %s failed: %s", job_id, e)
        db.update_job_status(
            job_id,
            "failed",
            error_code="analysis_input_invalid",
            error_message=str(e)[:500],
        )
    except Exception as e:  # noqa: BLE001 - a worker must always leave a terminal state
        logger.exception("Analysis failed [%s]", job_id)
        try:
            db.update_job_status(
                job_id,
                "failed",
                error_code="analysis_error",
                error_message=f"Internal analysis error: {type(e).__name__}",
            )
        except (OSError, RuntimeError):
            logger.debug("Failed to mark job %s as failed", job_id)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.post("/v1/projects/{project_id}/analyses")
async def create_analysis(project_id: str, request: AnalysisRequest):
    rid = _request_id()

    try:
        assets_payload = asset_client.list_assets(project_id)
        gp_revisions = asset_client.list_gp_revisions(project_id)
    except AssetFetchError as e:
        raise _error(502, e.code, e.message, rid) from None

    assets = {}
    for payload in assets_payload:
        ref = AssetRef.from_api(payload)
        if ref.link_id:
            assets[ref.link_id] = ref

    try:
        resolved = resolve_analysis_inputs(
            project_id=project_id,
            gp_asset_link_id=request.gp_asset_link_id,
            claimed_gp_revision_sha256=request.gp_revision_sha256,
            source_midi_link_ids=request.source_midi_link_ids,
            audio_link_ids=request.audio_link_ids,
            structure_link_id=request.structure_link_id,
            assets=assets,
            gp_revisions=gp_revisions,
        )
    except AssetValidationError as e:
        # Fail closed before any job or cache entry exists.
        raise _error(422, e.code, e.message, rid) from None

    versions = processor_versions()
    cache_key = compute_cache_key(
        project_id=project_id,
        gp_revision_sha256=resolved.gp.sha256,
        gp_revision_number=resolved.gp_revision_number,
        source_midi_sha256s=resolved.source_midi_sha256s,
        audio_sha256s=resolved.audio_sha256s,
        structure_sha256=resolved.structure_sha256,
        processor_versions=versions,
        parameters=DEFAULT_PARAMS,
    )
    source_key = compute_source_evidence_key(
        project_id=project_id,
        source_midi_sha256s=resolved.source_midi_sha256s,
        audio_sha256s=resolved.audio_sha256s,
        processor_versions=versions,
        parameters=DEFAULT_PARAMS,
    )

    job_id = str(uuid.uuid4())
    analysis_id = str(uuid.uuid4())

    try:
        result, is_new = db.find_or_create_job_for_cache(
            cache_key=cache_key,
            job_id=job_id,
            analysis_id=analysis_id,
            project_id=project_id,
            max_queue_length=MAX_QUEUE_LENGTH,
        )
    except ValueError as e:
        raise _error(429, "queue_full", str(e), rid) from None

    if not is_new:
        status = result.get("status", "queued")
        return AnalysisResponse(
            job_id=result.get("job_id", ""),
            analysis_id=result.get("analysis_id", analysis_id),
            status="succeeded" if status == "cached" else status,
            cache_hit=(status == "cached"),
            request_id=rid,
        )

    executor.submit(
        _run_analysis, job_id, analysis_id, project_id, resolved, cache_key, source_key
    )

    return AnalysisResponse(
        job_id=job_id,
        analysis_id=analysis_id,
        status="queued",
        cache_hit=False,
        request_id=rid,
    )


@app.get("/v1/projects/{project_id}/analyses")
async def list_analyses(project_id: str):
    return {
        "analyses": db.get_results_for_project(project_id),
        "jobs": db.get_jobs_for_project(project_id),
    }


def _load_result(project_id: str, analysis_id: str, rid: str) -> dict:
    result = db.get_result(analysis_id)
    if not result or result["project_id"] != project_id:
        raise _error(404, "analysis_not_found", "Analysis not found", rid)
    return result


@app.get("/v1/projects/{project_id}/analyses/{analysis_id}")
async def get_analysis(project_id: str, analysis_id: str):
    rid = _request_id()
    result = _load_result(project_id, analysis_id, rid)
    data = json.loads(result["result_json"])
    return {
        "analysis_id": analysis_id,
        "project_id": project_id,
        "cache_key": result["cache_key"],
        "created_at": result["created_at"],
        "schema_version": data.get("schema_version"),
        "gp_revision_sha256": data.get("gp_revision_sha256"),
        "gp_revision_number": data.get("gp_revision_number"),
        "global_confidence": data.get("global_confidence"),
        "source_measure_count": len(data.get("source_measures", [])),
        "gp_measure_count": len(data.get("gp_measures", [])),
        "mapping_count": len(data.get("mappings", [])),
        "warning_codes": sorted({w["code"] for w in data.get("global_warnings", [])}),
        "input_identities": data.get("input_identities", {}),
        "provenance": data.get("provenance", {}),
        "consensus_decision": (data.get("midi_consensus") or {}).get("decision"),
    }


@app.get("/v1/projects/{project_id}/analyses/{analysis_id}/report.json")
async def get_json_report(project_id: str, analysis_id: str):
    rid = _request_id()
    result = _load_result(project_id, analysis_id, rid)
    return JSONResponse(
        content=json.loads(result["result_json"]), media_type="application/json"
    )


@app.get("/v1/projects/{project_id}/analyses/{analysis_id}/report.html")
async def get_html_report(project_id: str, analysis_id: str):
    rid = _request_id()
    result = _load_result(project_id, analysis_id, rid)
    analysis = ReferenceTimeAnalysis(**json.loads(result["result_json"]))
    return HTMLResponse(content=generate_html_report(analysis))


@app.get("/v1/jobs/{job_id}")
async def get_job(job_id: str):
    rid = _request_id()
    job = db.get_job(job_id)
    if not job:
        raise _error(404, "job_not_found", "Job not found", rid)
    return {
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
