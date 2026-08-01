from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from transcription_spike.adapters import AdapterError, AdapterSpec, run_external_adapter


def write_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def spec(script: Path, **overrides: object) -> AdapterSpec:
    values: dict[str, object] = {
        "name": "fake-model",
        "version": "1.2.3",
        "runtime": "python-test",
        "model_sha256": "a" * 64,
        "command": (sys.executable, str(script), "{input}", "{output}"),
        "output_suffix": ".mid",
        "timeout_seconds": 2.0,
        "max_output_bytes": 1024,
    }
    values.update(overrides)
    return AdapterSpec(**values)


def test_success_is_isolated_persisted_and_manifested(tmp_path: Path) -> None:
    script = write_script(
        tmp_path / "copy.py",
        "import pathlib,sys\n"
        "src,dst=map(pathlib.Path,sys.argv[1:3])\n"
        "assert src.name == 'input.wav'\n"
        "dst.write_bytes(b'MThd' + src.read_bytes())\n",
    )
    audio = tmp_path / "secret-original-name.wav"
    audio.write_bytes(b"audio-bytes")
    artifacts = tmp_path / "artifacts"

    result = run_external_adapter(spec(script), audio_path=audio, artifact_dir=artifacts)

    expected_input_hash = hashlib.sha256(b"audio-bytes").hexdigest()
    assert result.artifact_path.name == f"fake-model-1.2.3-{expected_input_hash[:16]}.mid"
    assert result.artifact_path.read_bytes() == b"MThdaudio-bytes"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["adapter"] == {"name": "fake-model", "version": "1.2.3"}
    assert manifest["input_sha256"] == expected_input_hash
    assert manifest["model_sha256"] == "a" * 64
    assert manifest["output_sha256"] == hashlib.sha256(b"MThdaudio-bytes").hexdigest()
    assert manifest["output_bytes"] == len(b"MThdaudio-bytes")
    assert manifest["command_template"] == list(spec(script).command)
    assert manifest["runtime"] == "python-test"
    assert manifest["duration_seconds"] >= 0
    assert "secret-original-name" not in result.manifest_path.read_text(encoding="utf-8")
    assert not any(path.name.startswith("transcription-run-") for path in artifacts.iterdir())


def test_command_is_argv_and_does_not_interpret_shell_metacharacters(tmp_path: Path) -> None:
    marker = tmp_path / "shell-was-used"
    script = write_script(
        tmp_path / "argv.py",
        "import pathlib,sys\npathlib.Path(sys.argv[2]).write_text(sys.argv[3])\n",
    )
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"x")
    malicious = f"$(touch {marker})"
    adapter = spec(
        script,
        command=(sys.executable, str(script), "{input}", "{output}", malicious),
        output_suffix=".txt",
    )

    result = run_external_adapter(adapter, audio_path=audio, artifact_dir=tmp_path / "out")

    assert result.artifact_path.read_text() == malicious
    assert not marker.exists()


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        ("import time\ntime.sleep(5)\n", "timeout"),
        ("import pathlib,sys\npathlib.Path(sys.argv[2]).write_bytes(b'x'*2048)\n", "output_too_large"),
        ("raise SystemExit(7)\n", "process_failed"),
        ("pass\n", "output_missing"),
    ],
)
def test_failures_are_bounded_and_sanitized(
    tmp_path: Path, body: str, expected_code: str
) -> None:
    script = write_script(tmp_path / f"{expected_code}.py", body)
    audio = tmp_path / "private-song-name.wav"
    audio.write_bytes(b"x")
    adapter = spec(script, timeout_seconds=0.1 if expected_code == "timeout" else 2.0)

    with pytest.raises(AdapterError) as caught:
        run_external_adapter(adapter, audio_path=audio, artifact_dir=tmp_path / "out")

    assert caught.value.code == expected_code
    assert "private-song-name" not in str(caught.value)
    assert not list((tmp_path / "out").glob("*.mid"))


def test_workspace_placeholder_supports_container_style_commands(tmp_path: Path) -> None:
    script = write_script(
        tmp_path / "workspace.py",
        "from pathlib import Path\n"
        "import sys\n"
        "root=Path(sys.argv[1])\n"
        "assert (root/'input.wav').read_bytes() == b'audio'\n"
        "(root/'output.mid').write_bytes(b'MThd-workspace')\n",
    )
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"audio")
    adapter = spec(script, command=(sys.executable, str(script), "{workspace}"))

    result = run_external_adapter(adapter, audio_path=audio, artifact_dir=tmp_path / "artifacts")

    assert result.artifact_path.read_bytes() == b"MThd-workspace"


def test_spec_rejects_unknown_command_placeholder(tmp_path: Path) -> None:
    script = write_script(tmp_path / "noop.py", "pass\n")

    with pytest.raises(ValueError, match="unsupported placeholder"):
        spec(script, command=(sys.executable, str(script), "{workspace}", "{network}"))


def test_spec_rejects_unsafe_or_incomplete_contracts(tmp_path: Path) -> None:
    script = write_script(tmp_path / "noop.py", "pass\n")
    bad_values = [
        {"name": "../escape"},
        {"output_suffix": "../x.mid"},
        {"command": (sys.executable, str(script), "{input}")},
        {"command": (sys.executable, str(script), "{output}")},
        {"model_sha256": "not-a-digest"},
        {"timeout_seconds": 0},
        {"max_output_bytes": 0},
    ]

    for values in bad_values:
        with pytest.raises(ValueError):
            spec(script, **values)
