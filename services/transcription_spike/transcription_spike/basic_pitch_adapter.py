from __future__ import annotations

import os
import re

from .adapters import AdapterSpec

BASIC_PITCH_VERSION = "0.4.0"
BASIC_PITCH_MODEL_SHA256 = "3db297d54af8e01c6e5618245c956b1d71b6a2b978cb2dedb527173186552676"
_IMMUTABLE_IMAGE = re.compile(r"^(?:sha256:|.+@sha256:)[0-9a-f]{64}$")


def basic_pitch_docker_spec(
    image_reference: str,
    *,
    docker_binary: str = "docker",
    user_id: int | None = None,
    group_id: int | None = None,
) -> AdapterSpec:
    """Build a bounded local Docker adapter spec for the pinned Basic Pitch runtime."""
    if not _IMMUTABLE_IMAGE.fullmatch(image_reference):
        raise ValueError("Basic Pitch image reference must be immutable")
    if not docker_binary:
        raise ValueError("docker_binary must not be empty")
    user_id = os.getuid() if user_id is None else user_id
    group_id = os.getgid() if group_id is None else group_id
    if user_id < 1 or group_id < 1:
        raise ValueError("container user and group must be non-root")

    return AdapterSpec(
        name="basic-pitch",
        version=BASIC_PITCH_VERSION,
        runtime=f"docker:{image_reference}",
        model_sha256=BASIC_PITCH_MODEL_SHA256,
        command=(
            docker_binary,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "--user",
            f"{user_id}:{group_id}",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "-v",
            "{workspace}:/work",
            image_reference,
            "--input",
            "/work/input.wav",
            "--output",
            "/work/output.mid",
        ),
        output_suffix=".mid",
        timeout_seconds=900.0,
        max_output_bytes=10 * 1024 * 1024,
    )
