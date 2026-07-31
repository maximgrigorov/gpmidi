"""Data contracts for reference-time analysis.

All models are Pydantic v2 with strict validation:
- No NaN/Infinity in floats (validated)
- UTC ISO 8601 timestamps
- Stable list ordering by documented keys
- Deterministic JSON serialization
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


def _check_finite(v: float | None) -> float | None:
    if v is not None and (math.isnan(v) or math.isinf(v)):
        raise ValueError("NaN and Infinity are not allowed")
    return v


class FiniteFloat(float):
    """Float that rejects NaN and Infinity."""

    @classmethod
    def __get_validators__(cls):
        yield cls._validate

    @classmethod
    def _validate(cls, v):
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            raise ValueError("NaN and Infinity are not allowed")
        return v


class WarningCode(str, Enum):
    DEFAULT_TEMPO = "default_tempo"
    DEFAULT_TIME_SIG = "default_time_sig"
    SMPTE_DIVISION = "smpte_division"
    EMPTY_TRACK = "empty_track"
    SHORT_DURATION = "short_duration"
    LONG_DURATION = "long_duration"
    PRE_ROLL_DETECTED = "pre_roll_detected"
    PICKUP_DETECTED = "pickup_detected"
    TRAILING_SILENCE = "trailing_silence"
    MID_MEASURE_TS_CHANGE = "mid_measure_ts_change"
    TEMPO_MAP_CONFLICT = "tempo_map_conflict"
    AUDIO_MISSING = "audio_missing"
    AUDIO_DECODE_FAILED = "audio_decode_failed"
    LOW_CONFIDENCE = "low_confidence"
    AMBIGUOUS_MAPPING = "ambiguous_mapping"
    ANCHOR_CONFLICT = "anchor_conflict"
    MALFORMED_MIDI = "malformed_midi"
    NON_MONOTONIC_TEMPO = "non_monotonic_tempo"
    MEASURE_DURATION_MISMATCH = "measure_duration_mismatch"
    GP_REPEAT_DETECTED = "gp_repeat_detected"
    GP_EMPTY_MEASURE = "gp_empty_measure"


class MappingType(str, Enum):
    ONE_TO_ONE = "one_to_one"
    SOURCE_GAP = "source_gap"
    GP_GAP = "gp_gap"
    REPEAT = "repeat"
    AMBIGUOUS = "ambiguous"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class TempoEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    tick: int
    seconds: float
    bpm: float

    @field_validator("bpm", "seconds")
    @classmethod
    def _finite(cls, v: float) -> float:
        return _check_finite(v)


class TimeSignatureEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    tick: int
    seconds: float
    numerator: int
    denominator: int

    @field_validator("seconds")
    @classmethod
    def _finite(cls, v: float) -> float:
        return _check_finite(v)


class Warning(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: WarningCode
    message: str
    context: Optional[dict] = None


class SourceTempoEvidence(BaseModel):
    """Tempo/timing evidence extracted from one source MIDI file."""
    model_config = ConfigDict(frozen=True)

    asset_link_id: str
    sha256: str
    parser_name: str
    parser_version: str
    midi_ppq: int
    time_signatures: list[TimeSignatureEvent]
    tempo_events: list[TempoEvent]
    first_event_tick: Optional[int] = None
    first_event_seconds: Optional[float] = None
    duration_ticks: int
    duration_seconds: float
    source_type: int  # MIDI type 0 or 1
    warnings: list[Warning] = []
    validation_errors: list[str] = []

    @field_validator("duration_seconds", "first_event_seconds")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        return _check_finite(v)


class SourceMeasure(BaseModel):
    """One measure in the source (Suno MIDI) timeline."""
    model_config = ConfigDict(frozen=True)

    index: int
    tick_start: int
    tick_end: int
    seconds_start: float
    seconds_end: float
    numerator: int
    denominator: int
    tempo_bpm: float
    audio_downbeat_evidence: Optional[float] = None
    confidence: float
    warnings: list[Warning] = []

    @field_validator(
        "seconds_start", "seconds_end", "tempo_bpm",
        "audio_downbeat_evidence", "confidence",
    )
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        return _check_finite(v)

    @model_validator(mode="after")
    def _confidence_bounds(self):
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")
        return self


class GPMeasure(BaseModel):
    """One measure in the destination Guitar Pro grid."""
    model_config = ConfigDict(frozen=True)

    gp_revision_sha256: str
    measure_index: int
    measure_number: int  # 1-based display number
    tick_start: int
    tick_end: int
    numerator: int
    denominator: int
    marker_text: Optional[str] = None
    section_text: Optional[str] = None
    has_repeat_open: bool = False
    has_repeat_close: bool = False
    repeat_close_count: int = 0
    has_alternate_ending: bool = False
    alternate_ending_numbers: list[int] = []
    is_empty: bool = False
    tempo_bpm: Optional[float] = None
    warnings: list[Warning] = []

    @field_validator("tempo_bpm")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        return _check_finite(v)


class MappingAlternative(BaseModel):
    model_config = ConfigDict(frozen=True)

    gp_measure_index: Optional[int]
    score: float
    reason: str

    @field_validator("score")
    @classmethod
    def _finite(cls, v: float) -> float:
        return _check_finite(v)


class MeasureMapping(BaseModel):
    """Mapping from one source measure to a GP measure (or gap)."""
    model_config = ConfigDict(frozen=True)

    source_measure_index: int
    gp_measure_index: Optional[int] = None
    gp_measure_number: Optional[int] = None
    mapping_type: MappingType
    source_seconds_start: float
    source_seconds_end: float
    gp_tick_start: Optional[int] = None
    gp_tick_end: Optional[int] = None
    normalized_position_start: float = 0.0
    normalized_position_end: float = 1.0
    confidence: float
    evidence: list[str] = []
    reason_codes: list[str] = []
    warnings: list[Warning] = []
    alternatives: list[MappingAlternative] = []

    @field_validator(
        "source_seconds_start", "source_seconds_end",
        "normalized_position_start", "normalized_position_end",
        "confidence",
    )
    @classmethod
    def _finite(cls, v: float) -> float:
        return _check_finite(v)

    @model_validator(mode="after")
    def _confidence_bounds(self):
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")
        return self


class ConsensusDecision(str, Enum):
    AGREED = "agreed"
    CONFLICT = "conflict"
    SINGLE_SOURCE = "single_source"


class MidiConsensus(BaseModel):
    """Result of comparing multiple source MIDI tempo maps."""
    model_config = ConfigDict(frozen=True)

    decision: ConsensusDecision
    primary_asset_link_id: Optional[str] = None
    primary_sha256: Optional[str] = None
    source_count: int
    agreement_metrics: dict = {}
    per_source_warnings: dict[str, list[Warning]] = {}
    conflict_regions: list[dict] = []
    selection_reason: str = ""


class ReferenceTimeAnalysis(BaseModel):
    """Top-level analysis result document."""

    schema_version: str = "1.0.0"
    analysis_id: str
    project_id: str
    gp_revision_sha256: str
    gp_revision_number: Optional[int] = None
    processor_versions: dict[str, str] = {}
    parameters: dict = {}
    input_identities: dict[str, str] = {}
    source_evidence: list[SourceTempoEvidence] = []
    midi_consensus: Optional[MidiConsensus] = None
    source_measures: list[SourceMeasure] = []
    gp_measures: list[GPMeasure] = []
    mappings: list[MeasureMapping] = []
    global_confidence: float = 0.0
    global_warnings: list[Warning] = []
    cache_key: str = ""
    created_at: datetime = None
    completed_at: Optional[datetime] = None

    @field_validator("global_confidence")
    @classmethod
    def _finite(cls, v: float) -> float:
        return _check_finite(v)

    @model_validator(mode="after")
    def _set_created_at(self):
        if self.created_at is None:
            object.__setattr__(self, "created_at", datetime.now(timezone.utc))
        return self

    @model_validator(mode="after")
    def _confidence_bounds(self):
        if not (0.0 <= self.global_confidence <= 1.0):
            raise ValueError(
                f"global_confidence must be in [0,1], got {self.global_confidence}"
            )
        return self


class AnalysisJob(BaseModel):
    """Durable job tracking for analysis requests."""

    job_id: str
    analysis_id: str
    project_id: str
    status: JobStatus = JobStatus.QUEUED
    progress_phase: str = ""
    progress_message: str = ""
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    cache_key: str = ""
    created_at: datetime = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _set_created_at(self):
        if self.created_at is None:
            object.__setattr__(self, "created_at", datetime.now(timezone.utc))
        return self
