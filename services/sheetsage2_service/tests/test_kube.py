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


def test_worker_job_rejects_mutable_image():
    import pytest

    with pytest.raises(ValueError, match="digest"):
        build_worker_job(job_id="abc123", image="registry.local/sheetsage2:latest", pvc_name="data")
