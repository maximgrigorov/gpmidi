from __future__ import annotations

import re
from typing import Any

_JOB_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")


def build_worker_job(
    *, job_id: str, image: str, pvc_name: str,
    model_pvc_name: str = "sheetsage2-models",
) -> dict[str, Any]:
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError("invalid job_id")
    if "@sha256:" not in image:
        raise ValueError("worker image must be pinned by digest")
    name = f"sheetsage2-{job_id}"
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": "gpmidi-ml",
            "labels": {"app": "sheetsage2-worker", "sheetsage2-job-id": job_id},
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 300,
            "activeDeadlineSeconds": 1800,
            "template": {
                "metadata": {
                    "labels": {"app": "sheetsage2-worker", "sheetsage2-job-id": job_id}
                },
                "spec": {
                    "runtimeClassName": "nvidia",
                    "serviceAccountName": "sheetsage2-worker",
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "worker",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "-m", "sheetsage2_service.worker"],
                            # The root filesystem is read-only; trust_remote_code
                            # copies model code into the HF modules cache.
                            "env": [
                                {"name": "SHEETSAGE2_JOB_ID", "value": job_id},
                                {"name": "HOME", "value": "/tmp/home"},
                                {"name": "HF_HOME", "value": "/tmp/huggingface"},
                                {"name": "XDG_CACHE_HOME", "value": "/tmp/.cache"},
                            ],
                            "resources": {
                                "requests": {
                                    "cpu": "2",
                                    "memory": "6Gi",
                                    "nvidia.com/gpu": 1,
                                },
                                "limits": {
                                    "cpu": "6",
                                    "memory": "12Gi",
                                    "ephemeral-storage": "4Gi",
                                    "nvidia.com/gpu": 1,
                                },
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/data"},
                                {"name": "models", "mountPath": "/models", "readOnly": True},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}},
                        {"name": "models", "persistentVolumeClaim": {"claimName": model_pvc_name}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "4Gi"}},
                    ],
                },
            },
        },
    }
