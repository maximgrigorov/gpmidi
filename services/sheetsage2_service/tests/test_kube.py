from __future__ import annotations

from sheetsage2_service.kube import build_worker_job


def test_worker_job_is_gpu_bounded_ephemeral_and_uses_shared_storage():
    manifest = build_worker_job(
        job_id="abc123",
        image="registry.local/sheetsage2@sha256:" + "1" * 64,
        pvc_name="sheetsage2-data",
    )
    assert manifest["kind"] == "Job"
    spec = manifest["spec"]
    assert spec["backoffLimit"] == 0
    assert spec["ttlSecondsAfterFinished"] == 300
    assert spec["activeDeadlineSeconds"] == 1800
    pod = spec["template"]["spec"]
    assert pod["runtimeClassName"] == "nvidia"
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    container = pod["containers"][0]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert container["resources"]["requests"]["nvidia.com/gpu"] == 1
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["env"][0] == {"name": "SHEETSAGE2_JOB_ID", "value": "abc123"}
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "sheetsage2-data"
    assert pod["volumes"][1]["persistentVolumeClaim"]["claimName"] == "sheetsage2-models"
    assert container["volumeMounts"][1]["readOnly"] is True


def test_worker_job_points_every_cache_at_the_writable_tmp_mount():
    # The root filesystem is read-only and HOME=/home/sheetsage does not exist,
    # so trust_remote_code could not create ~/.cache/huggingface/modules.
    manifest = build_worker_job(
        job_id="abc123",
        image="registry.local/sheetsage2@sha256:" + "1" * 64,
        pvc_name="sheetsage2-data",
    )
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}
    tmp_mount = next(m["mountPath"] for m in container["volumeMounts"] if m["name"] == "tmp")
    for name in ("HOME", "HF_HOME", "XDG_CACHE_HOME"):
        assert env[name].startswith(tmp_mount + "/"), name


def test_worker_job_rejects_mutable_image():
    import pytest

    with pytest.raises(ValueError, match="digest"):
        build_worker_job(job_id="abc123", image="registry.local/sheetsage2:latest", pvc_name="data")


def test_muscriptor_worker_selects_large_model_and_its_verified_cache():
    manifest = build_worker_job(
        job_id="abc123",
        image="registry.local/audio-midi@sha256:" + "1" * 64,
        pvc_name="sheetsage2-data",
        transcriber="muscriptor-large",
        instruments=["voice", "distorted_electric_guitar", "drums"],
        muscriptor_model_pvc_name="muscriptor-models",
    )
    pod = manifest["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["AUDIO_TRANSCRIBER"] == "muscriptor-large"
    assert env["MUSCRIPTOR_MODEL_PATH"] == "/muscriptor-models/large/model.safetensors"
    assert env["MUSCRIPTOR_INSTRUMENTS"] == "voice,distorted_electric_guitar,drums"
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["muscriptor-models"]["persistentVolumeClaim"]["claimName"] == "muscriptor-models"
    mount = next(item for item in container["volumeMounts"] if item["name"] == "muscriptor-models")
    assert mount == {
        "name": "muscriptor-models", "mountPath": "/muscriptor-models", "readOnly": True
    }
