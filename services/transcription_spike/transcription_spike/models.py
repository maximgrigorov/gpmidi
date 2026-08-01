from __future__ import annotations

import math
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class EventKind(str, Enum):
    PITCHED = "pitched"
    PERCUSSIVE = "percussive"


class TranscriptionEvent(BaseModel):
    """One normalized event on the source-audio timeline."""

    model_config = ConfigDict(frozen=True)

    onset_seconds: float
    offset_seconds: float | None = None
    kind: EventKind
    instrument: str
    midi_pitch: int | None = None
    event_class: str | None = None
    velocity: int | None = None
    confidence: float | None = None

    @field_validator("onset_seconds", "offset_seconds", "confidence")
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("event floats must be finite")
        return value

    @model_validator(mode="after")
    def _valid_contract(self) -> "TranscriptionEvent":
        if self.onset_seconds < 0:
            raise ValueError("onset_seconds must be non-negative")
        if self.offset_seconds is not None and self.offset_seconds < self.onset_seconds:
            raise ValueError("offset_seconds must not precede onset_seconds")
        if not self.instrument.strip():
            raise ValueError("instrument must not be empty")
        if self.midi_pitch is not None and not 0 <= self.midi_pitch <= 127:
            raise ValueError("midi_pitch must be in [0, 127]")
        if self.velocity is not None and not 1 <= self.velocity <= 127:
            raise ValueError("velocity must be in [1, 127]")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.kind is EventKind.PITCHED:
            if self.midi_pitch is None:
                raise ValueError("pitched events require midi_pitch")
            if self.event_class is not None:
                raise ValueError("pitched events must not set event_class")
        else:
            if self.event_class is None or not self.event_class.strip():
                raise ValueError("percussive events require event_class")
        return self

    @property
    def match_key(self) -> tuple[str, str, int | str]:
        if self.kind is EventKind.PITCHED:
            assert self.midi_pitch is not None
            return self.kind.value, self.instrument, self.midi_pitch
        assert self.event_class is not None
        return self.kind.value, self.instrument, self.event_class

    @property
    def metric_label(self) -> str:
        if self.kind is EventKind.PITCHED:
            return self.instrument
        assert self.event_class is not None
        return self.event_class


class ClassMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    references: int
    predictions: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    onset_error_p50_seconds: float | None = None
    onset_error_p95_seconds: float | None = None


class EvaluationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    tolerance_seconds: float
    references: int
    predictions: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    onset_error_p50_seconds: float | None = None
    onset_error_p95_seconds: float | None = None
    classes: list[ClassMetrics]
