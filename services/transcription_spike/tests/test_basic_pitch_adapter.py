from __future__ import annotations

import pytest
from transcription_spike.basic_pitch_adapter import basic_pitch_docker_spec

DIGEST = "f" * 64


def test_basic_pitch_spec_requires_immutable_image_reference() -> None:
    with pytest.raises(ValueError, match="immutable"):
        basic_pitch_docker_spec("registry.example/basic-pitch:latest")


@pytest.mark.parametrize(
    "image",
    [f"sha256:{DIGEST}", f"registry.example/basic-pitch@sha256:{DIGEST}"],
)
def test_basic_pitch_spec_enforces_runtime_isolation(image: str) -> None:
    spec = basic_pitch_docker_spec(image, user_id=1234, group_id=5678)
    command = list(spec.command)

    assert spec.name == "basic-pitch"
    assert spec.version == "0.4.0"
    assert spec.model_sha256 == "3db297d54af8e01c6e5618245c956b1d71b6a2b978cb2dedb527173186552676"
    assert command[0] == "docker"
    assert command[1:4] == ["run", "--rm", "--network"]
    assert command[4] == "none"
    assert "--read-only" in command
    assert ["--cap-drop", "ALL"] == command[command.index("--cap-drop") : command.index("--cap-drop") + 2]
    assert ["--user", "1234:5678"] == command[command.index("--user") : command.index("--user") + 2]
    assert "{workspace}:/work" in command
    assert image in command
    assert command[-4:] == ["--input", "/work/input.wav", "--output", "/work/output.mid"]
