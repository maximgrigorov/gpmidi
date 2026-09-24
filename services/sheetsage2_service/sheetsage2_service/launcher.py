from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from .kube import build_worker_job


@dataclass(frozen=True)
class WorkerObservation:
    phase: str
    pending_message: str = ""
    pending_seconds: float = 0.0
    terminated_reason: str = ""
    terminated_message: str = ""
    exit_code: int | None = None


class KubernetesLauncher:
    def __init__(
        self, *, image: str, pvc_name: str,
        model_pvc_name: str = "sheetsage2-models",
        namespace: str = "gpmidi-ml",
    ):
        from kubernetes import client, config

        config.load_incluster_config()
        self.batch = client.BatchV1Api()
        self.core = client.CoreV1Api()
        self.image = image
        self.pvc_name = pvc_name
        self.model_pvc_name = model_pvc_name
        self.namespace = namespace

    def launch(self, job_id: str) -> None:
        body = build_worker_job(
            job_id=job_id, image=self.image, pvc_name=self.pvc_name,
            model_pvc_name=self.model_pvc_name,
        )
        self.batch.create_namespaced_job(namespace=self.namespace, body=body)

    def observe(self, job_id: str) -> WorkerObservation:
        pods = self.core.list_namespaced_pod(
            namespace=self.namespace,
            label_selector=f"app=sheetsage2-worker,sheetsage2-job-id={job_id}",
        ).items
        if not pods:
            return WorkerObservation(phase="queued")
        pod = max(pods, key=lambda value: value.metadata.creation_timestamp)
        now = datetime.now(timezone.utc)
        created = pod.metadata.creation_timestamp or now
        age = max(0.0, (now - created).total_seconds())
        conditions = pod.status.conditions or []
        pending_message = " ".join(
            part
            for condition in conditions
            if condition.status == "False"
            for part in (condition.reason, condition.message)
            if part
        )
        statuses = pod.status.container_statuses or []
        for status in statuses:
            terminated = status.state.terminated if status.state else None
            if terminated is not None:
                return WorkerObservation(
                    phase="terminated",
                    terminated_reason=terminated.reason or "",
                    terminated_message=terminated.message or "",
                    exit_code=terminated.exit_code,
                )
        phase = (pod.status.phase or "pending").lower()
        return WorkerObservation(
            phase=phase,
            pending_message=pending_message,
            pending_seconds=age,
        )

    def delete(self, job_id: str) -> None:
        from kubernetes.client.exceptions import ApiException

        try:
            self.batch.delete_namespaced_job(
                name=f"sheetsage2-{job_id}",
                namespace=self.namespace,
                propagation_policy="Background",
            )
        except ApiException as exc:
            if exc.status != 404:
                raise


def launcher_from_env() -> KubernetesLauncher:
    image = os.environ.get("SHEETSAGE2_WORKER_IMAGE", "")
    if "@sha256:" not in image:
        raise RuntimeError("SHEETSAGE2_WORKER_IMAGE must be pinned by digest")
    return KubernetesLauncher(
        image=image,
        pvc_name=os.environ.get("SHEETSAGE2_PVC", "sheetsage2-data"),
        model_pvc_name=os.environ.get("SHEETSAGE2_MODEL_PVC", "sheetsage2-models"),
        namespace=os.environ.get("KUBERNETES_NAMESPACE", "gpmidi-ml"),
    )
