from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class AdapterSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    runtime: str
    model_sha256: str
    command: tuple[str, ...]
    output_suffix: str
    timeout_seconds: float
    max_output_bytes: int

    @field_validator("timeout_seconds")
    @classmethod
    def _positive_finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        return value

    @model_validator(mode="after")
    def _valid_contract(self) -> "AdapterSpec":
        if not _SAFE_NAME.fullmatch(self.name) or not _SAFE_NAME.fullmatch(self.version):
            raise ValueError("adapter name and version must be safe path components")
        if not self.runtime.strip():
            raise ValueError("runtime must not be empty")
        if not _SHA256.fullmatch(self.model_sha256):
            raise ValueError("model_sha256 must be a lowercase SHA-256 digest")
        if not self.command or any(not token for token in self.command):
            raise ValueError("command must contain non-empty argv tokens")
        joined = "\0".join(self.command)
        if "{input}" not in joined or "{output}" not in joined:
            raise ValueError("command must contain {input} and {output} placeholders")
        if not re.fullmatch(r"\.[A-Za-z0-9]+", self.output_suffix):
            raise ValueError("output_suffix must be a simple extension")
        if self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        return self


@dataclass(frozen=True)
class AdapterRunResult:
    artifact_path: Path
    manifest_path: Path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _network_quiet_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in list(environment):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            environment.pop(key)
    environment["NO_PROXY"] = "*"
    return environment


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def run_external_adapter(
    spec: AdapterSpec,
    *,
    audio_path: Path,
    artifact_dir: Path,
) -> AdapterRunResult:
    """Run one adapter in a temporary workspace and persist verified output."""
    audio_path = Path(audio_path)
    artifact_dir = Path(artifact_dir)
    if not audio_path.is_file():
        raise AdapterError("input_missing", "adapter input is unavailable")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    input_sha256 = _sha256_file(audio_path)
    artifact_name = f"{spec.name}-{spec.version}-{input_sha256[:16]}{spec.output_suffix}"
    final_artifact = artifact_dir / artifact_name
    final_manifest = artifact_dir / f"{artifact_name}.manifest.json"

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="transcription-run-", dir=artifact_dir) as temporary:
        workspace = Path(temporary)
        staged_input = workspace / "input.wav"
        staged_output = workspace / f"output{spec.output_suffix}"
        stdout_path = workspace / "stdout.log"
        stderr_path = workspace / "stderr.log"
        shutil.copyfile(audio_path, staged_input)
        argv = [
            token.replace("{input}", str(staged_input)).replace("{output}", str(staged_output))
            for token in spec.command
        ]

        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=workspace,
                    env=_network_quiet_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                )
            except OSError as error:
                raise AdapterError("process_start_failed", "adapter process could not start") from error

            failure_code: str | None = None
            while process.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed > spec.timeout_seconds:
                    failure_code = "timeout"
                    break
                if staged_output.exists() and staged_output.stat().st_size > spec.max_output_bytes:
                    failure_code = "output_too_large"
                    break
                if stdout_path.stat().st_size + stderr_path.stat().st_size > spec.max_output_bytes:
                    failure_code = "log_too_large"
                    break
                time.sleep(0.01)

            if failure_code is not None:
                _stop_process(process)
                raise AdapterError(failure_code, f"adapter failed: {failure_code}")
            return_code = process.wait()

        if return_code != 0:
            raise AdapterError("process_failed", "adapter process failed")
        if not staged_output.is_file():
            raise AdapterError("output_missing", "adapter produced no output")
        output_bytes = staged_output.stat().st_size
        if output_bytes > spec.max_output_bytes:
            raise AdapterError("output_too_large", "adapter failed: output_too_large")

        output_sha256 = _sha256_file(staged_output)
        os.replace(staged_output, final_artifact)

    duration_seconds = time.monotonic() - started
    manifest = {
        "adapter": {"name": spec.name, "version": spec.version},
        "command_template": list(spec.command),
        "duration_seconds": duration_seconds,
        "input_sha256": input_sha256,
        "model_sha256": spec.model_sha256,
        "output_bytes": output_bytes,
        "output_filename": final_artifact.name,
        "output_sha256": output_sha256,
        "runtime": spec.runtime,
        "schema_version": "1.0.0",
    }
    temporary_manifest = artifact_dir / f".{final_manifest.name}.tmp"
    temporary_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_manifest, final_manifest)
    return AdapterRunResult(artifact_path=final_artifact, manifest_path=final_manifest)
