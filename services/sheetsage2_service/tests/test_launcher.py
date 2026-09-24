from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sheetsage2_service.launcher import KubernetesLauncher


class FakeCore:
    def __init__(self, pod):
        self.pod = pod

    def list_namespaced_pod(self, **_kwargs):
        return SimpleNamespace(items=[self.pod])


def test_observe_flattens_unschedulable_condition_into_a_message():
    condition = SimpleNamespace(
        status="False",
        reason="Unschedulable",
        message="0/1 nodes are available: 1 Insufficient nvidia.com/gpu",
    )
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            creation_timestamp=datetime.now(timezone.utc) - timedelta(seconds=130)
        ),
        status=SimpleNamespace(
            conditions=[condition], container_statuses=[], phase="Pending"
        ),
    )
    launcher = KubernetesLauncher.__new__(KubernetesLauncher)
    launcher.namespace = "gpmidi-ml"
    launcher.core = FakeCore(pod)

    observation = launcher.observe("abc123")

    assert observation.phase == "pending"
    assert observation.pending_seconds >= 120
    assert observation.pending_message == (
        "Unschedulable 0/1 nodes are available: 1 Insufficient nvidia.com/gpu"
    )
