from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from .failures import UserFailure, classify_container_failure, classify_pending_reason
from .launcher import WorkerObservation, launcher_from_env

_ALLOWED = {".wav", ".flac", ".mp3"}
_MAX_BYTES = int(os.environ.get("SHEETSAGE2_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
_JOB_ID = re.compile(r"^[a-f0-9]{12}$")
_OWNER_TOKEN = re.compile(r"^[a-f0-9]{64}$")


class Launcher(Protocol):
    def launch(self, job_id: str) -> None: ...
    def observe(self, job_id: str) -> WorkerObservation: ...
    def delete(self, job_id: str) -> None: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _load_state(job_dir: Path) -> dict:
    try:
        return json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        raise HTTPException(404, "job not found") from None


def _safe_filename(name: str) -> str:
    leaf = Path(name).name
    if leaf != name or not leaf or leaf.startswith("."):
        raise HTTPException(400, "invalid filename")
    cleaned = re.sub(r"[^A-Za-z0-9А-Яа-яЁё._ -]+", "_", leaf).strip()
    if not cleaned:
        raise HTTPException(400, "invalid filename")
    return cleaned[:200]


def _owner_sha256(request: Request) -> str:
    token = request.headers.get("X-Owner-Token", "")
    if not _OWNER_TOKEN.fullmatch(token):
        raise HTTPException(401, "owner token required")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _valid_magic(extension: str, header: bytes) -> bool:
    if extension == ".wav":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    if extension == ".flac":
        return header.startswith(b"fLaC")
    if extension == ".mp3":
        return header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0)
    return False


def _public_state(job_id: str, state: dict) -> dict:
    payload = {
        key: value for key, value in state.items()
        if key not in {"stored_filename", "owner_token_sha256"}
    }
    payload["id"] = job_id
    if state.get("status") == "succeeded":
        payload["downloads"] = {
            "archive": f"/v1/jobs/{job_id}/downloads/archive",
            "midi": f"/v1/jobs/{job_id}/downloads/midi",
            "report": f"/v1/jobs/{job_id}/downloads/report",
        }
    return payload


def create_app(*, data_root: Path | None = None, launcher: Launcher | None = None) -> FastAPI:
    root = Path(data_root or os.environ.get("SHEETSAGE2_DATA_ROOT", "/data"))
    jobs_root = root / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    worker_launcher = launcher or launcher_from_env()
    app = FastAPI(title="gpmidi SheetSage2", version="1")

    def job_dir(job_id: str, owner_sha256: str) -> Path:
        if not _JOB_ID.fullmatch(job_id):
            raise HTTPException(404, "job not found")
        path = jobs_root / job_id
        if not path.is_dir():
            raise HTTPException(404, "job not found")
        state = _load_state(path)
        if not hmac.compare_digest(str(state.get("owner_token_sha256", "")), owner_sha256):
            raise HTTPException(404, "job not found")
        return path

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "data_root_writable": os.access(root, os.W_OK)}

    async def persist_and_launch(filename: str, chunks, owner_sha256: str) -> dict:
        filename = _safe_filename(filename)
        extension = Path(filename).suffix.lower()
        if extension not in _ALLOWED:
            raise HTTPException(415, "Поддерживаются WAV, FLAC и MP3.")
        identifier = uuid.uuid4().hex[:12]
        directory = jobs_root / identifier
        input_dir = directory / "input"
        input_dir.mkdir(parents=True)
        target = input_dir / filename
        total = 0
        header = b""
        try:
            with target.open("wb") as output:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        raise HTTPException(413, "Аудиофайл слишком большой.")
                    if len(header) < 16:
                        header += chunk[: 16 - len(header)]
                    output.write(chunk)
            if total == 0 or not _valid_magic(extension, header):
                raise HTTPException(415, "Содержимое файла не соответствует аудиоформату.")
            state = {
                "status": "queued",
                "stage": "waiting_for_gpu",
                "original_filename": filename,
                "stored_filename": filename,
                "owner_token_sha256": owner_sha256,
                "created_at": _now(),
            }
            _atomic_json(directory / "state.json", state)
            worker_launcher.launch(identifier)
        except HTTPException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        # The launcher is a boundary over Kubernetes clients with multiple
        # exception families. Convert every launch failure to one safe API
        # response and remove the partially persisted upload.
        except Exception:  # noqa: BLE001
            shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(503, "Не удалось поставить задачу SheetSage2 в очередь.") from None
        return _public_state(identifier, state)

    @app.post("/v1/jobs", status_code=202)
    async def create_job(request: Request, file: UploadFile = File(...)):
        async def upload_chunks():
            while chunk := await file.read(1024 * 1024):
                yield chunk

        return await persist_and_launch(
            file.filename or "", upload_chunks(), _owner_sha256(request)
        )

    @app.put("/v1/jobs", status_code=202)
    async def create_job_stream(request: Request):
        filename = request.headers.get("X-Filename", "")
        return await persist_and_launch(filename, request.stream(), _owner_sha256(request))

    @app.get("/v1/jobs")
    def list_jobs(request: Request):
        owner_sha256 = _owner_sha256(request)
        rows = []
        for directory in sorted(jobs_root.iterdir(), reverse=True):
            if directory.is_dir() and _JOB_ID.fullmatch(directory.name):
                try:
                    state = _load_state(directory)
                    if hmac.compare_digest(
                        str(state.get("owner_token_sha256", "")), owner_sha256
                    ):
                        rows.append(_public_state(directory.name, state))
                except HTTPException:
                    continue
        return {"jobs": rows[:30]}

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        directory = job_dir(job_id, _owner_sha256(request))
        state = _load_state(directory)
        if state.get("status") in {"queued", "running"}:
            observation = worker_launcher.observe(job_id)
            failure = None
            if observation.phase == "terminated" and observation.exit_code not in (None, 0):
                failure = classify_container_failure(
                    reason=observation.terminated_reason,
                    exit_code=observation.exit_code,
                    message=observation.terminated_message,
                )
            elif observation.phase == "terminated" and observation.exit_code == 0:
                failure = UserFailure(
                    "artifacts_missing",
                    "Контейнер SheetSage2 завершился без полного набора артефактов. Повторите запрос.",
                )
            elif observation.phase == "pending" and observation.pending_seconds >= 120:
                candidate = classify_pending_reason(observation.pending_message)
                if candidate.code == "gpu_busy":
                    failure = candidate
            if failure is not None:
                state = {
                    **state,
                    "status": "failed",
                    "error_code": failure.code,
                    "message": failure.user_message,
                    "original_filename": state.get("original_filename", "audio"),
                    "created_at": state.get("created_at", _now()),
                }
                _atomic_json(directory / "state.json", state)
        return _public_state(job_id, state)

    @app.get("/v1/jobs/{job_id}/downloads/{kind}")
    def download(job_id: str, kind: str, request: Request):
        directory = job_dir(job_id, _owner_sha256(request))
        state = _load_state(directory)
        if state.get("status") != "succeeded":
            raise HTTPException(409, "result is not ready")
        fields = {
            "archive": ("archive_name", "application/zip"),
            "midi": ("midi_name", "audio/midi"),
            "report": ("report_html_name", "text/html; charset=utf-8"),
        }
        if kind not in fields:
            raise HTTPException(404, "download not found")
        field, media_type = fields[kind]
        name = state.get(field, "")
        if not name or Path(name).name != name:
            raise HTTPException(404, "download not found")
        path = directory / "result" / name
        if not path.is_file():
            raise HTTPException(404, "download not found")
        return FileResponse(path, media_type=media_type, filename=name)

    @app.delete("/v1/jobs/{job_id}", status_code=204)
    def delete_job(job_id: str, request: Request):
        directory = job_dir(job_id, _owner_sha256(request))
        worker_launcher.delete(job_id)
        shutil.rmtree(directory)

    return app
