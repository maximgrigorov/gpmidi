from __future__ import annotations

from dataclasses import dataclass

GPU_BUSY_MESSAGE = (
    "Недостаточно свободной видеопамяти для запуска SheetSage2. "
    "Остановите нагрузку, очистите VRAM и повторите запрос."
)


@dataclass(frozen=True)
class UserFailure:
    code: str
    user_message: str


def _looks_like_gpu_oom(text: str) -> bool:
    value = text.lower()
    return any(
        marker in value
        for marker in (
            "cuda out of memory",
            "cublas_status_alloc_failed",
            "outofmemoryerror",
            "gpu memory",
        )
    )


def classify_exception(exc: Exception) -> UserFailure:
    if _looks_like_gpu_oom(str(exc)) or exc.__class__.__name__ == "OutOfMemoryError":
        return UserFailure("gpu_vram_exhausted", GPU_BUSY_MESSAGE)
    return UserFailure(
        "transcription_failed",
        "Не удалось разобрать аудио. Проверьте файл и повторите запрос.",
    )


def classify_container_failure(*, reason: str, exit_code: int | None, message: str) -> UserFailure:
    if reason == "OOMKilled" or _looks_like_gpu_oom(message):
        return UserFailure("gpu_vram_exhausted", GPU_BUSY_MESSAGE)
    return UserFailure(
        "worker_failed",
        "Контейнер SheetSage2 завершился с ошибкой. Повторите запрос.",
    )


def classify_pending_reason(message: str) -> UserFailure:
    value = message.lower()
    if "insufficient nvidia.com/gpu" in value or "nvidia.com/gpu" in value and "unschedulable" in value:
        return UserFailure("gpu_busy", GPU_BUSY_MESSAGE)
    return UserFailure(
        "worker_pending",
        "Задача ожидает запуска на AILab. Обновите страницу через несколько секунд.",
    )
