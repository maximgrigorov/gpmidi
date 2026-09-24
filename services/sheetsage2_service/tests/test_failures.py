from __future__ import annotations

from sheetsage2_service.failures import (
    GPU_BUSY_MESSAGE,
    classify_container_failure,
    classify_exception,
    classify_pending_reason,
)


def test_cuda_oom_has_actionable_user_message():
    failure = classify_exception(RuntimeError("CUDA out of memory. Tried to allocate 64 MiB"))
    assert failure.code == "gpu_vram_exhausted"
    assert failure.user_message == GPU_BUSY_MESSAGE
    assert "CUDA" not in failure.user_message


def test_oomkilled_container_has_same_actionable_message():
    failure = classify_container_failure(reason="OOMKilled", exit_code=137, message="")
    assert failure.code == "gpu_vram_exhausted"
    assert failure.user_message == GPU_BUSY_MESSAGE


def test_unschedulable_gpu_is_reported_as_busy_not_generic_failure():
    failure = classify_pending_reason(
        "0/1 nodes are available: 1 Insufficient nvidia.com/gpu. preemption: no victims"
    )
    assert failure.code == "gpu_busy"
    assert failure.user_message == GPU_BUSY_MESSAGE


def test_unrelated_failure_does_not_claim_vram_problem():
    failure = classify_exception(ValueError("invalid audio"))
    assert failure.code == "transcription_failed"
    assert failure.user_message != GPU_BUSY_MESSAGE
