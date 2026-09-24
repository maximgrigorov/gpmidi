from __future__ import annotations

from pathlib import Path

import yaml

TASKS = Path("infra/ailab/tekton/tasks.yaml")
PIPELINE = Path("infra/ailab/tekton/pipeline.yaml")
SHEETSAGE2_NETWORK_POLICY = Path(
    "infra/ailab/apps/sheetsage2-service/networkpolicy.yaml"
)
SHEETSAGE2_DEPLOYMENT = Path("infra/ailab/apps/sheetsage2-service/deployment.yaml")


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


def test_image_builds_do_not_hold_compressed_layers_in_memory():
    # Kaniko's default compressed caching keeps each compressed layer in RAM.
    # The torch cu128 layer OOM-killed the 12Gi build during its snapshot.
    task_text = TASKS.read_text(encoding="utf-8")
    build_task = task_text.split("name: kaniko-build-push", 1)[1].split(
        "\n---\n", 1
    )[0]
    assert "- --compressed-caching=false" in build_task


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


def test_sheetsage2_deploy_delivers_the_declared_readiness_probe():
    # deploy-by-digest only patched the image, so a probe change in the
    # Deployment manifest never reached the live object: AILab kept probing
    # /healthz after /readyz was declared. The pipeline must deliver it.
    deployment = yaml.safe_load(SHEETSAGE2_DEPLOYMENT.read_text(encoding="utf-8"))
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    declared = container["readinessProbe"]["httpGet"]["path"]
    assert declared == "/readyz"

    pipeline = next(
        doc for doc in yaml.safe_load_all(PIPELINE.read_text(encoding="utf-8"))
        if doc and doc["metadata"]["name"] == "gpmidi-ci"
    )
    deploy = next(
        task for task in pipeline["spec"]["tasks"]
        if task["name"] == "deploy-sheetsage2-service"
    )
    params = {param["name"]: param["value"] for param in deploy["params"]}
    assert params["readiness-path"] == declared

    task = next(
        doc for doc in yaml.safe_load_all(TASKS.read_text(encoding="utf-8"))
        if doc and doc["metadata"]["name"] == "deploy-by-digest"
    )
    task_params = {param["name"]: param for param in task["spec"]["params"]}
    assert task_params["readiness-path"]["default"] == ""
    script = task["spec"]["steps"][0]["script"]
    assert script.index("readinessProbe") < script.index("rollout status")
    assert "readiness_path:" in script
