from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

from . import MODEL_REVISION, MUSCRIPTOR_LARGE_REVISION
from .artifacts import package_transcription
from .failures import classify_exception

Transcriber = Callable[[Path, Path], dict]


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _merge_state(path: Path, updates: dict) -> None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.update({key: value for key, value in updates.items() if value is not None})
    for key, value in updates.items():
        if value is None:
            state.pop(key, None)
    _atomic_json(path, state)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _default_transcriber(input_path: Path, output_path: Path) -> dict:
    from .model_runtime import transcribe

    return transcribe(input_path, output_path)


def _muscriptor_transcriber(input_path: Path, output_path: Path) -> dict:
    from .muscriptor_runtime import transcribe

    return transcribe(input_path, output_path)


def run_worker(
    *,
    job_dir: Path,
    transcriber: Transcriber = _default_transcriber,
    model_revision: str = MODEL_REVISION,
    transcribers: dict[str, Transcriber] | None = None,
    model_revisions: dict[str, str] | None = None,
) -> int:
    job_dir = Path(job_dir)
    state_path = job_dir / "state.json"
    inputs = sorted((job_dir / "input").glob("*"))
    if len(inputs) != 1 or not inputs[0].is_file():
        _atomic_json(
            state_path,
            {"status": "failed", "error_code": "invalid_input", "message": "Входной аудиофайл недоступен."},
        )
        return 2
    input_path = inputs[0]
    try:
        initial_state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        initial_state = {}
    selected = str(
        initial_state.get("transcriber")
        or os.environ.get("AUDIO_TRANSCRIBER", "sheetsage2")
    )
    available = transcribers or {
        "sheetsage2": transcriber,
        "muscriptor-large": _muscriptor_transcriber,
    }
    revisions = model_revisions or {
        "sheetsage2": model_revision,
        "muscriptor-large": MUSCRIPTOR_LARGE_REVISION,
    }
    if selected not in available or selected not in revisions:
        _merge_state(
            state_path,
            {
                "status": "failed",
                "error_code": "invalid_model",
                "message": "Неизвестная модель транскрипции.",
            },
        )
        return 2
    model_output = job_dir / "model-output"
    _merge_state(state_path, {"status": "running", "stage": "loading_model"})
    started = time.monotonic()
    try:
        details = available[selected](input_path, model_output) or {}
        elapsed = float(details.get("elapsed_seconds", time.monotonic() - started))
        packaged = package_transcription(
            model_output=model_output,
            destination=job_dir / "result",
            original_filename=input_path.name,
            model_revision=revisions[selected],
            input_sha256=_sha256(input_path),
            elapsed_seconds=elapsed,
            transcriber=selected,
            requested_instruments=list(initial_state.get("instruments") or []),
            peak_vram_mib=details.get("peak_vram_mib"),
        )
    # This is the worker's terminal boundary: every model/runtime failure must
    # produce durable, normalized state instead of leaving the job "running".
    except Exception as exc:  # noqa: BLE001
        # The user only sees the normalized message; keep the cause in the pod log.
        traceback.print_exc(file=sys.stderr)
        failure = classify_exception(exc)
        result = job_dir / "result"
        if result.exists():
            import shutil

            shutil.rmtree(result)
        _merge_state(
            state_path,
            {
                "status": "failed",
                "stage": None,
                "error_code": failure.code,
                "message": failure.user_message,
            },
        )
        return 20 if failure.code == "gpu_vram_exhausted" else 1
    _merge_state(
        state_path,
        {
            "status": "succeeded",
            "stage": None,
            "archive_name": packaged.archive_path.name,
            "midi_name": packaged.all_tracks_path.name,
            "report_html_name": packaged.report_html_path.name,
            "report_json_name": packaged.report_json_path.name,
        },
    )
    return 0


def main() -> int:
    job_id = os.environ.get("SHEETSAGE2_JOB_ID", "")
    if not job_id:
        raise SystemExit("SHEETSAGE2_JOB_ID is required")
    return run_worker(job_dir=Path("/data/jobs") / job_id)


if __name__ == "__main__":
    raise SystemExit(main())
