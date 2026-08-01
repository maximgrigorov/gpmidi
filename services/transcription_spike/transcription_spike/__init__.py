"""Phase 3 transcription spike contracts and evaluation."""

from .evaluation import evaluate_events
from .models import EvaluationReport, EventKind, TranscriptionEvent

__all__ = ["EvaluationReport", "EventKind", "TranscriptionEvent", "evaluate_events"]
