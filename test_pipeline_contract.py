from __future__ import annotations

from pathlib import Path

TASKS = Path("infra/ailab/tekton/tasks.yaml")


PIPELINE = Path("infra/ailab/tekton/pipeline.yaml")


def test_deploy_task_selects_the_ready_pod_for_the_exact_image():
    text = TASKS.read_text(encoding="utf-8")
    deploy_task = text.split("name: deploy-by-digest", 1)[1].split("\n---\n", 1)[0]

    assert ".items[0]." not in deploy_task
    assert "deletionTimestamp" in deploy_task
    assert "Ready" in deploy_task
    assert "BY_DIGEST" in deploy_task


def test_sheetsage2_build_has_a_larger_ephemeral_storage_budget():
    task_text = TASKS.read_text(encoding="utf-8")
    build_task = task_text.split("name: kaniko-build-push", 1)[1].split(
        "\n---\n", 1
    )[0]
    assert "name: ephemeral-storage-limit" in build_task
    assert "default: 12Gi" in build_task
    assert "ephemeral-storage: $(params.ephemeral-storage-limit)" in build_task

    pipeline_text = PIPELINE.read_text(encoding="utf-8")
    sheetsage_build = pipeline_text.split("name: build-sheetsage2-service", 1)[
        1
    ].split("\n  - name:", 1)[0]
    assert "name: ephemeral-storage-limit" in sheetsage_build
    assert "value: 24Gi" in sheetsage_build
