"""Asset API — FastAPI application for project and asset management."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import secrets
import sqlite3
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .config import (
    CORS_ORIGINS,
    PROJECT_DESC_MAX_LEN,
    PROJECT_NAME_MAX_LEN,
)
from .database import get_db, init_db, recover_interrupted_uploads
from .limiter import ConcurrencyLimiter, RateLimiter
from .roles import (
    AssetRole,
    allowed_extensions_for_role,
    max_bytes_for_role,
    sanitize_filename,
    validate_extension,
    validate_signature,
)
from .storage import (
    StreamingHashWriter,
    blob_relpath,
    cleanup_stale_temps,
    ensure_dirs,
)

logger = logging.getLogger("asset_api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="gpmidi Asset API", version="1.0.0", root_path="/asset-api")

def _rate_limit_per_min() -> int:
    from .config import UPLOAD_RATE_LIMIT_PER_MIN
    return UPLOAD_RATE_LIMIT_PER_MIN

def _max_concurrent_uploads() -> int:
    return int(os.environ.get("MAX_CONCURRENT_UPLOADS", "5"))

ticket_rate_limiter = RateLimiter(max_per_minute=_rate_limit_per_min())
upload_concurrency = ConcurrencyLimiter(max_concurrent=_max_concurrent_uploads())


if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        max_age=3600,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    rid = _request_id()
    return JSONResponse(
        status_code=422,
        content={"detail": {"code": "validation_error", "message": str(exc.errors()), "request_id": rid}},
        headers={"X-Request-Id": rid},
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
    if "request_id" not in detail:
        detail["request_id"] = _request_id()
    headers = dict(exc.headers) if exc.headers else {}
    headers["X-Request-Id"] = detail["request_id"]
    return JSONResponse(status_code=exc.status_code, content={"detail": detail}, headers=headers)


@app.exception_handler(Exception)
async def unexpected_exception_handler(request: Request, exc: Exception):
    rid = _request_id()
    logger.exception("Unhandled request error request_id=%s", rid, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": {
            "code": "internal_error",
            "message": "Internal server error",
            "request_id": rid,
        }},
        headers={"X-Request-Id": rid},
    )


@app.on_event("startup")
def startup() -> None:
    ensure_dirs()
    init_db()
    recovered = recover_interrupted_uploads()
    if recovered:
        logger.warning("Marked %d interrupted upload tickets as failed", recovered)
    removed = cleanup_stale_temps()
    if removed:
        logger.info("Cleaned up %d stale temp uploads", removed)


# --- Pydantic models ---

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=PROJECT_NAME_MAX_LEN)
    description: str | None = Field(default=None, max_length=PROJECT_DESC_MAX_LEN)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank after trim")
        return v


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=PROJECT_NAME_MAX_LEN)
    description: str | None = Field(default=None, max_length=PROJECT_DESC_MAX_LEN)
    revision: int = Field(description="Optimistic lock: must match current revision")

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank after trim")
        return v


class TicketRequest(BaseModel):
    role: AssetRole
    original_filename: str = Field(min_length=1, max_length=255)

    @field_validator("original_filename")
    @classmethod
    def check_filename(cls, v: str) -> str:
        sanitize_filename(v)
        return v


class GPRevisionCreate(BaseModel):
    original_filename: str = Field(min_length=1, max_length=255)
    note: str | None = None
    force_new: bool = False

    @field_validator("original_filename")
    @classmethod
    def check_filename(cls, v: str) -> str:
        sanitize_filename(v)
        return v


# --- Error helper ---

def _error(status_code: int, code: str, message: str, request_id: str | None = None,
           headers: dict | None = None) -> HTTPException:
    detail = {"code": code, "message": message, "request_id": request_id or _request_id()}
    exc = HTTPException(status_code=status_code, detail=detail)
    if headers:
        exc.headers = headers
    return exc


def _request_id() -> str:
    return uuid.uuid4().hex[:12]


def _ticket_ttl() -> int:
    from .config import TICKET_TTL_SECONDS
    return TICKET_TTL_SECONDS


# --- Health ---

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    from .config import BLOBS_DIR, DB_PATH
    errors = []
    if not DB_PATH.parent.exists():
        errors.append("db directory missing")
    if not BLOBS_DIR.exists():
        errors.append("blobs directory missing")
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
    except Exception:
        logger.exception("Readiness SQLite check failed")
        errors.append("sqlite unavailable")
    if errors:
        return JSONResponse({"status": "not_ready", "errors": errors}, status_code=503)
    return {"status": "ready"}


# --- Projects CRUD ---

@app.post("/v1/projects", status_code=201)
def create_project(body: ProjectCreate):
    rid = _request_id()
    project_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, description, created_at, updated_at, revision) VALUES (?,?,?,?,?,1)",
            (project_id, body.name, body.description, now, now),
        )
        conn.commit()
    return {"id": project_id, "name": body.name, "description": body.description,
            "created_at": now, "updated_at": now, "revision": 1, "request_id": rid}


@app.get("/v1/projects")
def list_projects():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT p.id, p.name, p.description, p.created_at, p.updated_at, p.revision,
                      (SELECT COUNT(*) FROM project_assets WHERE project_id=p.id) as asset_count,
                      (SELECT MAX(revision) FROM gp_revisions WHERE project_id=p.id) as latest_gp_rev
               FROM projects p ORDER BY p.updated_at DESC, p.id"""
        ).fetchall()
    return {"projects": [dict(r) for r in rows]}


@app.get("/v1/projects/{project_id}")
def get_project(project_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise _error(404, "project_not_found", "Project not found")
        assets = conn.execute(
            "SELECT * FROM project_assets WHERE project_id=? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        revisions = conn.execute(
            "SELECT * FROM gp_revisions WHERE project_id=? ORDER BY revision",
            (project_id,),
        ).fetchall()
    return {
        "project": dict(row),
        "assets": [dict(a) for a in assets],
        "gp_revisions": [dict(r) for r in revisions],
    }


@app.patch("/v1/projects/{project_id}")
def update_project(project_id: str, body: ProjectUpdate):
    rid = _request_id()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        row = conn.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise _error(404, "project_not_found", "Project not found")
        if row["revision"] != body.revision:
            raise _error(409, "revision_conflict",
                         f"Expected revision {body.revision}, current is {row['revision']}")
        updates = []
        params: list = []
        if body.name is not None:
            updates.append("name=?")
            params.append(body.name.strip())
        if body.description is not None:
            updates.append("description=?")
            params.append(body.description)
        updates.append("updated_at=?")
        params.append(now)
        updates.append("revision=revision+1")
        params.append(project_id)
        conn.execute(f"UPDATE projects SET {','.join(updates)} WHERE id=?", params)
        conn.commit()
        updated = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return {"project": dict(updated), "request_id": rid}


@app.delete("/v1/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise _error(404, "project_not_found", "Project not found")
        conn.execute(
            "UPDATE upload_tickets SET status='expired' WHERE project_id=? AND status IN ('pending','uploading')",
            (project_id,),
        )
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        conn.commit()
    return Response(status_code=204)


# --- Upload tickets ---

@app.post("/v1/projects/{project_id}/upload-tickets", status_code=201)
def create_upload_ticket(project_id: str, body: TicketRequest):
    rid = _request_id()
    if not ticket_rate_limiter.allow():
        raise _error(429, "rate_limit_exceeded",
                     "Too many ticket requests. Try again later.",
                     headers={"Retry-After": str(ticket_rate_limiter.retry_after())})
    with get_db() as conn:
        row = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise _error(404, "project_not_found", "Project not found")

    ext = validate_extension(body.original_filename, body.role)
    if not ext:
        allowed = allowed_extensions_for_role(body.role)
        raise _error(422, "invalid_extension",
                     f"Extension not allowed for role {body.role.value}. Allowed: {sorted(allowed)}")

    max_b = max_bytes_for_role(body.role)
    ticket_raw = secrets.token_urlsafe(48)
    ticket_hash = hashlib.sha256(ticket_raw.encode()).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=_ticket_ttl())).isoformat()

    with get_db() as conn:
        conn.execute(
            """INSERT INTO upload_tickets
               (ticket_hash, project_id, role, original_filename, max_bytes, expires_at, status)
               VALUES (?,?,?,?,?,?,?)""",
            (ticket_hash, project_id, body.role.value, body.original_filename, max_b, expires, "pending"),
        )
        conn.commit()

    return {
        "ticket": ticket_raw,
        "expires_at": expires,
        "max_bytes": max_b,
        "role": body.role.value,
        "request_id": rid,
    }


# --- Streaming upload ---

@app.put("/v1/uploads/{ticket}")
async def upload_blob(ticket: str, request: Request):
    rid = _request_id()
    ticket_hash = hashlib.sha256(ticket.encode()).hexdigest()

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM upload_tickets WHERE ticket_hash=?", (ticket_hash,)
        ).fetchone()
        if not row:
            raise _error(404, "ticket_not_found", "Upload ticket not found or invalid")
        if row["status"] not in ("pending",):
            raise _error(410, "ticket_consumed", "Ticket already used")
        expires = datetime.fromisoformat(row["expires_at"])
        if datetime.now(timezone.utc) > expires:
            raise _error(410, "ticket_expired", "Ticket expired")

        # Atomic claim: CAS pending -> uploading
        cur = conn.execute(
            "UPDATE upload_tickets SET status='uploading' WHERE ticket_hash=? AND status='pending'",
            (ticket_hash,),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise _error(409, "ticket_race", "Ticket claimed by another request")

    project_id = row["project_id"]
    role = AssetRole(row["role"])
    original_filename = row["original_filename"]
    max_bytes = row["max_bytes"]

    # Verify project still exists after atomic claim
    with get_db() as conn:
        proj = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not proj:
            _mark_ticket_failed(ticket_hash)
            raise _error(404, "project_not_found", "Project was deleted")

    ext = validate_extension(original_filename, role)

    if not upload_concurrency.try_acquire():
        _mark_ticket_failed(ticket_hash)
        raise _error(429, "too_many_uploads",
                     "Too many concurrent uploads. Try again later.",
                     headers={"Retry-After": "5"})

    writer = StreamingHashWriter()
    header_checked = False
    header_buf = bytearray()

    try:
        async for chunk in request.stream():
            writer.write(chunk)
            if writer.size > max_bytes:
                writer.abort()
                raise _error(413, "file_too_large",
                             f"Upload exceeds {max_bytes} bytes limit")
            if not header_checked:
                header_buf.extend(chunk)
                if len(header_buf) >= 64:
                    if not validate_signature(bytes(header_buf[:64]), ext or ""):
                        writer.abort()
                        raise _error(415, "invalid_signature",
                                     "File signature does not match expected format")
                    header_checked = True

        if writer.size == 0:
            writer.abort()
            raise _error(422, "empty_upload", "No data received")

        if not header_checked and writer.size > 0 and not validate_signature(bytes(header_buf), ext or ""):
            writer.abort()
            raise _error(415, "invalid_signature",
                         "File signature does not match expected format")

        sha256_hex = writer.finalize()
        writer.commit(sha256_hex)
        rel = blob_relpath(sha256_hex)

        media_type = mimetypes.guess_type(original_filename)[0] or "application/octet-stream"

        now = datetime.now(timezone.utc).isoformat()
        link_id = str(uuid.uuid4())

        with get_db() as conn:
            conn.execute(
                "UPDATE upload_tickets SET status='consumed', consumed_at=? WHERE ticket_hash=?",
                (now, ticket_hash),
            )
            existing = conn.execute("SELECT sha256 FROM assets WHERE sha256=?", (sha256_hex,)).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO assets (sha256, size_bytes, media_type, original_name, blob_relpath, created_at) VALUES (?,?,?,?,?,?)",
                    (sha256_hex, writer.size, media_type, original_filename, rel, now),
                )

            provenance = json.dumps({"uploaded_at": now, "original_filename": original_filename})

            try:
                conn.execute(
                    """INSERT INTO project_assets (id, project_id, asset_sha256, role, original_filename, created_at, provenance)
                       VALUES (?,?,?,?,?,?,?)""",
                    (link_id, project_id, sha256_hex, role.value, original_filename, now, provenance),
                )
            except sqlite3.IntegrityError:
                existing_link = conn.execute(
                    "SELECT id FROM project_assets WHERE project_id=? AND asset_sha256=? AND role=?",
                    (project_id, sha256_hex, role.value),
                ).fetchone()
                if existing_link:
                    link_id = existing_link["id"]
                else:
                    raise

            if role == AssetRole.GUITAR_PRO:
                _create_gp_revision(conn, project_id, sha256_hex, original_filename, now)

            conn.commit()

        return JSONResponse(
            status_code=201,
            content={
                "link_id": link_id,
                "sha256": sha256_hex,
                "size_bytes": writer.size,
                "role": role.value,
                "deduplicated": existing is not None,
                "request_id": rid,
            },
        )
    except HTTPException:
        _mark_ticket_failed(ticket_hash)
        raise
    except Exception:
        writer.abort()
        _mark_ticket_failed(ticket_hash)
        raise
    finally:
        upload_concurrency.release()


def _mark_ticket_failed(ticket_hash: str) -> None:
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE upload_tickets SET status='failed' WHERE ticket_hash=? AND status='uploading'",
                (ticket_hash,),
            )
            conn.commit()
    except (sqlite3.Error, OSError):
        logger.debug("Failed to mark ticket %s as failed", ticket_hash)


def _create_gp_revision(
    conn: sqlite3.Connection,
    project_id: str,
    sha256_hex: str,
    original_filename: str,
    now: str,
    note: str | None = None,
    force_new: bool = False,
) -> dict:
    existing = conn.execute(
        "SELECT * FROM gp_revisions WHERE project_id=? AND asset_sha256=?",
        (project_id, sha256_hex),
    ).fetchone()
    if existing and not force_new:
        return dict(existing)

    max_rev = conn.execute(
        "SELECT MAX(revision) as m FROM gp_revisions WHERE project_id=?", (project_id,)
    ).fetchone()
    next_rev = (max_rev["m"] or 0) + 1
    rev_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO gp_revisions (id, project_id, revision, asset_sha256, original_filename, created_at, note) VALUES (?,?,?,?,?,?,?)",
        (rev_id, project_id, next_rev, sha256_hex, original_filename, now, note),
    )
    return {"id": rev_id, "revision": next_rev, "asset_sha256": sha256_hex}


# --- GP Revisions ---

@app.post("/v1/projects/{project_id}/gp-revisions", status_code=201)
def create_gp_revision_via_ticket(project_id: str, body: GPRevisionCreate):
    """Create a GP revision via the ticket flow (upload GP with role guitar-pro)."""
    with get_db() as conn:
        row = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise _error(404, "project_not_found", "Project not found")
    raise _error(501, "use_ticket_flow",
                 "Use upload-tickets with role=guitar-pro for GP uploads, "
                 "revision is created automatically on upload")


# --- Assets ---

@app.get("/v1/projects/{project_id}/assets")
def list_project_assets(project_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise _error(404, "project_not_found", "Project not found")
        assets = conn.execute(
            """SELECT pa.id, pa.role, pa.original_filename, pa.label, pa.created_at,
                      pa.provenance, a.sha256, a.size_bytes, a.media_type
               FROM project_assets pa JOIN assets a ON pa.asset_sha256=a.sha256
               WHERE pa.project_id=? ORDER BY pa.created_at, pa.id""",
            (project_id,),
        ).fetchall()
    return {"assets": [dict(a) for a in assets]}


@app.get("/v1/projects/{project_id}/assets/{link_id}/download")
def download_asset(project_id: str, link_id: str):
    with get_db() as conn:
        row = conn.execute(
            """SELECT pa.original_filename, a.sha256, a.size_bytes, a.media_type, a.blob_relpath
               FROM project_assets pa JOIN assets a ON pa.asset_sha256=a.sha256
               WHERE pa.id=? AND pa.project_id=?""",
            (link_id, project_id),
        ).fetchone()
        if not row:
            raise _error(404, "asset_not_found", "Asset link not found in this project")

    from .config import BLOBS_DIR
    filepath = BLOBS_DIR / row["blob_relpath"]
    if not filepath.exists():
        raise _error(500, "blob_missing", "Blob file not found on disk")

    safe_name = row["original_filename"].replace('"', "")
    size = row["size_bytes"]
    media = row["media_type"]

    def iterfile() -> AsyncIterator[bytes]:
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    headers = {
        "Content-Disposition": f'attachment; filename="{safe_name}"',
        "Content-Length": str(size),
    }
    return StreamingResponse(iterfile(), media_type=media, headers=headers)


@app.delete("/v1/projects/{project_id}/assets/{link_id}", status_code=204)
def delete_asset_link(project_id: str, link_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM project_assets WHERE id=? AND project_id=?",
            (link_id, project_id),
        ).fetchone()
        if not row:
            raise _error(404, "asset_not_found", "Asset link not found")
        conn.execute("DELETE FROM project_assets WHERE id=?", (link_id,))
        conn.commit()
    return Response(status_code=204)


# --- Manifest ---

@app.get("/v1/projects/{project_id}/manifest")
def get_manifest(project_id: str):
    with get_db() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise _error(404, "project_not_found", "Project not found")
        assets = conn.execute(
            """SELECT pa.id as link_id, pa.role, pa.original_filename, pa.label,
                      pa.created_at, pa.provenance, a.sha256, a.size_bytes, a.media_type
               FROM project_assets pa JOIN assets a ON pa.asset_sha256=a.sha256
               WHERE pa.project_id=? ORDER BY pa.role, pa.created_at, pa.id""",
            (project_id,),
        ).fetchall()
        revisions = conn.execute(
            "SELECT id, revision, asset_sha256, original_filename, created_at, note FROM gp_revisions WHERE project_id=? ORDER BY revision",
            (project_id,),
        ).fetchall()

    manifest = {
        "schema_version": 1,
        "project": {
            "id": project["id"],
            "name": project["name"],
            "description": project["description"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
            "revision": project["revision"],
        },
        "gp_revisions": [
            {
                "id": r["id"],
                "revision": r["revision"],
                "sha256": r["asset_sha256"],
                "original_filename": r["original_filename"],
                "created_at": r["created_at"],
                "note": r["note"],
            }
            for r in revisions
        ],
        "assets": [
            {
                "link_id": a["link_id"],
                "sha256": a["sha256"],
                "size_bytes": a["size_bytes"],
                "media_type": a["media_type"],
                "role": a["role"],
                "original_filename": a["original_filename"],
                "label": a["label"],
                "created_at": a["created_at"],
                "provenance": json.loads(a["provenance"]) if a["provenance"] else None,
            }
            for a in assets
        ],
    }
    return manifest
