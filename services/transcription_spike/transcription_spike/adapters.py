from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")
_SUPPORTED_PLACEHOLDERS = {"input", "output", "workspace", "run_id"}


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
    cleanup_command: tuple[str, ...] | None = None
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
        command_placeholders = set(_PLACEHOLDER.findall("\0".join(self.command)))
        cleanup_placeholders: set[str] = set()
        if self.cleanup_command is not None:
            if not self.cleanup_command or any(not token for token in self.cleanup_command):
                raise ValueError("cleanup_command must contain non-empty argv tokens")
            cleanup_placeholders = set(_PLACEHOLDER.findall("\0".join(self.cleanup_command)))
        placeholders = command_placeholders | cleanup_placeholders
        unsupported = placeholders - _SUPPORTED_PLACEHOLDERS
        if unsupported:
            raise ValueError(f"unsupported placeholder: {sorted(unsupported)[0]}")
        if "workspace" not in command_placeholders and not {"input", "output"}.issubset(
            command_placeholders
        ):
            raise ValueError("command must contain {workspace} or both {input} and {output}")
        if self.cleanup_command is not None and "run_id" not in cleanup_placeholders:
            raise ValueError("cleanup_command must contain {run_id}")
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


def _signal_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.send_signal(sig)


def _stop_process(process: subprocess.Popen[bytes], cleanup_argv: list[str] | None) -> None:
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    _signal_process_group(process, signal.SIGKILL)
    if process.poll() is None:
        process.kill()
        process.wait(timeout=1)
    if cleanup_argv is not None:
        try:
            subprocess.run(
                cleanup_argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def _open_regular_file_nofollow(path: Path) -> int | None:
    if not hasattr(os, "O_NOFOLLOW"):
        raise AdapterError("output_missing", "host cannot safely open adapter output")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AdapterError("output_missing", "adapter produced no regular output") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise AdapterError("output_missing", "adapter produced no regular output")
    return descriptor


def _regular_file_size_nofollow(path: Path) -> int | None:
    descriptor = _open_regular_file_nofollow(path)
    if descriptor is None:
        return None
    try:
        return os.fstat(descriptor).st_size
    finally:
        os.close(descriptor)


def _persist_open_output(
    descriptor: int,
    *,
    artifact_dir: Path,
    final_artifact: Path,
    max_output_bytes: int,
) -> tuple[int, str]:
    temporary_descriptor, temporary_name = tempfile.mkstemp(prefix=".adapter-output-", dir=artifact_dir)
    temporary_path = Path(temporary_name)
    digest = hashlib.sha256()
    output_bytes = 0
    try:
        with os.fdopen(descriptor, "rb") as source, os.fdopen(temporary_descriptor, "wb") as destination:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                output_bytes += len(chunk)
                if output_bytes > max_output_bytes:
                    raise AdapterError("output_too_large", "adapter failed: output_too_large")
                digest.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, final_artifact)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return output_bytes, digest.hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


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
        run_id = workspace.name

        def expand(tokens: tuple[str, ...]) -> list[str]:
            return [
                token.replace("{input}", str(staged_input))
                .replace("{output}", str(staged_output))
                .replace("{workspace}", str(workspace))
                .replace("{run_id}", run_id)
                for token in tokens
            ]

        argv = expand(spec.command)
        cleanup_argv = expand(spec.cleanup_command) if spec.cleanup_command is not None else None

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
                    start_new_session=os.name == "posix",
                )
            except OSError as error:
                raise AdapterError("process_start_failed", "adapter process could not start") from error

            failure_code: str | None = None
            while process.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed > spec.timeout_seconds:
                    failure_code = "timeout"
                    break
                try:
                    output_size = _regular_file_size_nofollow(staged_output)
                except AdapterError:
                    failure_code = "output_missing"
                    break
                if output_size is not None and output_size > spec.max_output_bytes:
                    failure_code = "output_too_large"
                    break
                if stdout_path.stat().st_size + stderr_path.stat().st_size > spec.max_output_bytes:
                    failure_code = "log_too_large"
                    break
                time.sleep(0.01)

            if failure_code is not None:
                _stop_process(process, cleanup_argv)
                raise AdapterError(failure_code, f"adapter failed: {failure_code}")
            return_code = process.wait()
            _signal_process_group(process, signal.SIGKILL)

        if return_code != 0:
            _stop_process(process, cleanup_argv)
            raise AdapterError("process_failed", "adapter process failed")
        descriptor = _open_regular_file_nofollow(staged_output)
        if descriptor is None:
            raise AdapterError("output_missing", "adapter produced no output")
        output_bytes, output_sha256 = _persist_open_output(
            descriptor,
            artifact_dir=artifact_dir,
            final_artifact=final_artifact,
            max_output_bytes=spec.max_output_bytes,
        )

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
    _atomic_write_text(
        final_manifest,
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    return AdapterRunResult(artifact_path=final_artifact, manifest_path=final_manifest)
