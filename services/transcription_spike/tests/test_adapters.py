from __future__ import annotations

import hashlib
import json
import os
import sys
import time
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


def test_host_without_nofollow_fails_with_explicit_capability_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = write_script(tmp_path / "noop.py", "pass\n")
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"audio")
    monkeypatch.delattr(os, "O_NOFOLLOW")

    with pytest.raises(AdapterError) as caught:
        run_external_adapter(spec(script), audio_path=audio, artifact_dir=tmp_path / "artifacts")

    assert caught.value.code == "host_unsupported"


def test_process_created_output_symlink_is_rejected_without_reading_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.mid"
    outside.write_bytes(b"host-only-content")
    script = write_script(
        tmp_path / "symlink.py",
        "import os,sys\nos.symlink(sys.argv[3], sys.argv[2])\n",
    )
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"audio")
    adapter = spec(
        script,
        command=(sys.executable, str(script), "{input}", "{output}", str(outside)),
    )

    with pytest.raises(AdapterError) as caught:
        run_external_adapter(adapter, audio_path=audio, artifact_dir=tmp_path / "artifacts")

    assert caught.value.code == "output_missing"
    assert not list((tmp_path / "artifacts").glob("*.mid"))
    assert outside.read_bytes() == b"host-only-content"


def test_process_cannot_redirect_manifest_write_with_predictable_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("do-not-change", encoding="utf-8")
    script = write_script(
        tmp_path / "manifest_symlink.py",
        "import hashlib,os,pathlib,sys\n"
        "src,dst=map(pathlib.Path,sys.argv[1:3])\n"
        "digest=hashlib.sha256(src.read_bytes()).hexdigest()[:16]\n"
        "name=f'.fake-model-1.2.3-{digest}.mid.manifest.json.tmp'\n"
        "os.symlink(sys.argv[3], dst.parent.parent/name)\n"
        "dst.write_bytes(b'MThd-safe')\n",
    )
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"audio")
    adapter = spec(
        script,
        command=(sys.executable, str(script), "{input}", "{output}", str(outside)),
    )

    result = run_external_adapter(adapter, audio_path=audio, artifact_dir=tmp_path / "artifacts")

    assert outside.read_text(encoding="utf-8") == "do-not-change"
    assert json.loads(result.manifest_path.read_text(encoding="utf-8"))["output_bytes"] == 9
    assert not result.manifest_path.is_symlink()


@pytest.mark.skipif(os.name != "posix", reason="process-group behavior is POSIX-specific")
def test_timeout_terminates_descendants_and_runs_runtime_cleanup(tmp_path: Path) -> None:
    marker = tmp_path / "orphan-wrote"
    cleanup_marker = tmp_path / "cleanup-ran"
    child = write_script(
        tmp_path / "child.py",
        "import pathlib,sys,time\ntime.sleep(0.5)\npathlib.Path(sys.argv[1]).write_text('orphan')\n",
    )
    parent = write_script(
        tmp_path / "parent.py",
        "import subprocess,sys,time\nsubprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\ntime.sleep(5)\n",
    )
    cleanup = write_script(
        tmp_path / "cleanup.py",
        "import pathlib,sys\nassert sys.argv[2].startswith('transcription-run-')\npathlib.Path(sys.argv[1]).write_text('clean')\n",
    )
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"audio")
    adapter = spec(
        parent,
        command=(sys.executable, str(parent), str(child), str(marker), "{workspace}"),
        cleanup_command=(sys.executable, str(cleanup), str(cleanup_marker), "{run_id}"),
        timeout_seconds=0.1,
    )

    with pytest.raises(AdapterError, match="timeout"):
        run_external_adapter(adapter, audio_path=audio, artifact_dir=tmp_path / "artifacts")

    time.sleep(0.6)
    assert not marker.exists()
    assert cleanup_marker.read_text() == "clean"


def test_spec_rejects_unknown_command_placeholder(tmp_path: Path) -> None:
    script = write_script(tmp_path / "noop.py", "pass\n")

    with pytest.raises(ValueError, match="unsupported placeholder"):
        spec(script, command=(sys.executable, str(script), "{workspace}", "{network}"))


def test_cleanup_placeholders_cannot_complete_an_incomplete_run_command(tmp_path: Path) -> None:
    script = write_script(tmp_path / "noop.py", "pass\n")

    with pytest.raises(ValueError, match="command must contain"):
        spec(
            script,
            command=(sys.executable, str(script), "{input}"),
            cleanup_command=(sys.executable, str(script), "{workspace}", "{run_id}"),
        )


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
