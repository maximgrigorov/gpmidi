from __future__ import annotations

from pathlib import Path

TASKS = Path("infra/ailab/tekton/tasks.yaml")


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
    assert "ephemeral-storage: 24Gi" in build_task
    assert "ephemeral-storage-limit" not in build_task
