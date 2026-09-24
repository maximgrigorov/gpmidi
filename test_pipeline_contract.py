from __future__ import annotations

from pathlib import Path

TASKS = Path("infra/ailab/tekton/tasks.yaml")
SHEETSAGE2_NETWORK_POLICY = Path(
    "infra/ailab/apps/sheetsage2-service/networkpolicy.yaml"
)


def test_deploy_task_selects_the_ready_pod_for_the_exact_image():
    text = TASKS.read_text(encoding="utf-8")
    deploy_task = text.split("name: deploy-by-digest", 1)[1].split("\n---\n", 1)[0]

    assert ".items[0]." not in deploy_task
    assert "deletionTimestamp" in deploy_task
    assert "Ready" in deploy_task
    assert "BY_DIGEST" in deploy_task


def test_image_builds_have_enough_ephemeral_storage_for_large_ml_wheels():
    task_text = TASKS.read_text(encoding="utf-8")
    build_task = task_text.split("name: kaniko-build-push", 1)[1].split(
        "\n---\n", 1
    )[0]
    assert "memory: 12Gi" in build_task
    assert "ephemeral-storage: 24Gi" in build_task
    assert "ephemeral-storage-limit" not in build_task


def test_model_sync_uses_a_writable_huggingface_cache():
    task_text = TASKS.read_text(encoding="utf-8")
    sync_task = task_text.split("name: sheetsage2-model-sync", 1)[1].split(
        "\n---\n", 1
    )[0]

    assert "name: HOME\n                value: /tmp/home" in sync_task
    assert "name: HF_HOME\n                value: /tmp/huggingface" in sync_task
    assert "name: HF_HUB_DISABLE_XET\n                value: \"1\"" in sync_task
    assert "sizeLimit: 2Gi" in sync_task


def test_sheetsage2_controller_can_reach_the_k3s_api_endpoint():
    policy = SHEETSAGE2_NETWORK_POLICY.read_text(encoding="utf-8")

    assert "cidr: 10.43.0.1/32" in policy
    assert "cidr: 192.168.30.2/32" in policy
    assert "port: 6443" in policy
