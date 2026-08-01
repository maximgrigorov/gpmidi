from __future__ import annotations

import math

import pytest
from transcription_spike.evaluation import evaluate_events
from transcription_spike.models import EventKind, TranscriptionEvent


def pitched(onset: float, pitch: int, *, confidence: float | None = None) -> TranscriptionEvent:
    return TranscriptionEvent(
        onset_seconds=onset,
        offset_seconds=onset + 0.25,
        kind=EventKind.PITCHED,
        instrument="bass",
        midi_pitch=pitch,
        confidence=confidence,
    )


def drum(onset: float, event_class: str) -> TranscriptionEvent:
    return TranscriptionEvent(
        onset_seconds=onset,
        kind=EventKind.PERCUSSIVE,
        instrument="drums",
        event_class=event_class,
    )


def test_exact_pitched_match_has_perfect_metrics() -> None:
    report = evaluate_events([pitched(1.0, 40)], [pitched(1.0, 40)], tolerance_seconds=0.05)

    assert (report.true_positives, report.false_positives, report.false_negatives) == (1, 0, 0)
    assert (report.precision, report.recall, report.f1) == (1.0, 1.0, 1.0)
    assert report.onset_error_p50_seconds == 0.0
    assert report.onset_error_p95_seconds == 0.0
    assert [item.label for item in report.classes] == ["bass"]


def test_match_at_tolerance_boundary_is_inclusive() -> None:
    report = evaluate_events([pitched(1.0, 40)], [pitched(1.05, 40)], tolerance_seconds=0.05)

    assert report.true_positives == 1
    assert report.onset_error_p95_seconds == pytest.approx(0.05)


def test_duplicate_prediction_can_match_reference_only_once() -> None:
    report = evaluate_events(
        [pitched(1.0, 40)],
        [pitched(0.99, 40), pitched(1.01, 40)],
        tolerance_seconds=0.05,
    )

    assert (report.true_positives, report.false_positives, report.false_negatives) == (1, 1, 0)


def test_wrong_pitch_does_not_match_pitched_event() -> None:
    report = evaluate_events([pitched(1.0, 40)], [pitched(1.0, 41)], tolerance_seconds=0.05)

    assert (report.true_positives, report.false_positives, report.false_negatives) == (0, 1, 1)
    assert report.f1 == 0.0


def test_percussive_events_match_by_class_not_optional_midi_pitch() -> None:
    report = evaluate_events(
        [drum(1.0, "kick"), drum(2.0, "snare")],
        [drum(1.01, "kick"), drum(2.01, "hi_hat")],
        tolerance_seconds=0.05,
    )

    assert (report.true_positives, report.false_positives, report.false_negatives) == (1, 1, 1)
    assert [(item.label, item.true_positives) for item in report.classes] == [
        ("hi_hat", 0),
        ("kick", 1),
        ("snare", 0),
    ]


def test_empty_inputs_are_a_finite_perfect_noop() -> None:
    report = evaluate_events([], [], tolerance_seconds=0.05)

    assert report.classes == []
    assert (report.precision, report.recall, report.f1) == (1.0, 1.0, 1.0)
    assert report.onset_error_p50_seconds is None
    assert report.onset_error_p95_seconds is None
    assert all(math.isfinite(value) for value in (report.precision, report.recall, report.f1))


def test_event_validation_rejects_invalid_or_ambiguous_contracts() -> None:
    with pytest.raises(ValueError):
        pitched(-0.1, 40)
    with pytest.raises(ValueError):
        TranscriptionEvent(
            onset_seconds=1.0,
            kind=EventKind.PITCHED,
            instrument="bass",
        )
    with pytest.raises(ValueError):
        TranscriptionEvent(
            onset_seconds=1.0,
            kind=EventKind.PERCUSSIVE,
            instrument="drums",
        )
    with pytest.raises(ValueError):
        pitched(1.0, 128)


def test_matching_and_class_order_are_independent_of_input_order() -> None:
    reference = [drum(2.0, "snare"), drum(1.0, "kick"), pitched(3.0, 40)]
    prediction = [pitched(3.01, 40), drum(1.01, "kick"), drum(2.01, "snare")]

    first = evaluate_events(reference, prediction, tolerance_seconds=0.05)
    second = evaluate_events(list(reversed(reference)), list(reversed(prediction)), tolerance_seconds=0.05)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert [item.label for item in first.classes] == ["bass", "kick", "snare"]


def test_negative_or_non_finite_tolerance_is_rejected() -> None:
    for value in (-0.1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            evaluate_events([], [], tolerance_seconds=value)
