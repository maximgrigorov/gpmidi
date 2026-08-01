from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import ClassMetrics, EvaluationReport, TranscriptionEvent

_EPSILON = 1e-12


@dataclass(frozen=True)
class _Counts:
    references: int
    predictions: int
    true_positives: int
    errors: tuple[float, ...]


@dataclass
class _LabelCounts:
    references: int = 0
    predictions: int = 0
    true_positives: int = 0
    errors: list[float] = field(default_factory=list)


def _safe_metrics(true_positives: int, false_positives: int, false_negatives: int) -> tuple[float, float, float]:
    precision = true_positives / (true_positives + false_positives) if true_positives + false_positives else 1.0
    recall = true_positives / (true_positives + false_negatives) if true_positives + false_negatives else 1.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _percentile_nearest_rank(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _match_group(
    reference: list[TranscriptionEvent],
    prediction: list[TranscriptionEvent],
    tolerance_seconds: float,
) -> _Counts:
    reference = sorted(reference, key=lambda event: event.onset_seconds)
    prediction = sorted(prediction, key=lambda event: event.onset_seconds)
    ref_index = 0
    pred_index = 0
    errors: list[float] = []

    while ref_index < len(reference) and pred_index < len(prediction):
        ref_onset = reference[ref_index].onset_seconds
        pred_onset = prediction[pred_index].onset_seconds
        difference = pred_onset - ref_onset
        if abs(difference) <= tolerance_seconds + _EPSILON:
            errors.append(abs(difference))
            ref_index += 1
            pred_index += 1
        elif pred_onset < ref_onset:
            pred_index += 1
        else:
            ref_index += 1

    return _Counts(
        references=len(reference),
        predictions=len(prediction),
        true_positives=len(errors),
        errors=tuple(errors),
    )


def evaluate_events(
    reference: Iterable[TranscriptionEvent],
    prediction: Iterable[TranscriptionEvent],
    *,
    tolerance_seconds: float,
) -> EvaluationReport:
    """Evaluate normalized events without assuming a Guitar Pro measure grid."""
    if not math.isfinite(tolerance_seconds) or tolerance_seconds < 0:
        raise ValueError("tolerance_seconds must be finite and non-negative")

    reference_events = list(reference)
    prediction_events = list(prediction)
    reference_groups: dict[tuple[str, str, int | str], list[TranscriptionEvent]] = defaultdict(list)
    prediction_groups: dict[tuple[str, str, int | str], list[TranscriptionEvent]] = defaultdict(list)

    for event in reference_events:
        reference_groups[event.match_key].append(event)
    for event in prediction_events:
        prediction_groups[event.match_key].append(event)

    label_counts: dict[str, _LabelCounts] = defaultdict(_LabelCounts)
    all_errors: list[float] = []
    total_true_positives = 0

    for key in sorted(set(reference_groups) | set(prediction_groups)):
        references = reference_groups.get(key, [])
        predictions = prediction_groups.get(key, [])
        counts = _match_group(references, predictions, tolerance_seconds)
        sample = references[0] if references else predictions[0]
        label = sample.metric_label
        bucket = label_counts[label]
        bucket.references += counts.references
        bucket.predictions += counts.predictions
        bucket.true_positives += counts.true_positives
        bucket.errors.extend(counts.errors)
        all_errors.extend(counts.errors)
        total_true_positives += counts.true_positives

    class_metrics: list[ClassMetrics] = []
    for label in sorted(label_counts):
        bucket = label_counts[label]
        references = bucket.references
        predictions = bucket.predictions
        true_positives = bucket.true_positives
        errors = bucket.errors
        false_positives = predictions - true_positives
        false_negatives = references - true_positives
        precision, recall, f1 = _safe_metrics(true_positives, false_positives, false_negatives)
        class_metrics.append(
            ClassMetrics(
                label=label,
                references=references,
                predictions=predictions,
                true_positives=true_positives,
                false_positives=false_positives,
                false_negatives=false_negatives,
                precision=precision,
                recall=recall,
                f1=f1,
                onset_error_p50_seconds=_percentile_nearest_rank(errors, 0.50),
                onset_error_p95_seconds=_percentile_nearest_rank(errors, 0.95),
            )
        )

    false_positives = len(prediction_events) - total_true_positives
    false_negatives = len(reference_events) - total_true_positives
    precision, recall, f1 = _safe_metrics(total_true_positives, false_positives, false_negatives)
    return EvaluationReport(
        tolerance_seconds=tolerance_seconds,
        references=len(reference_events),
        predictions=len(prediction_events),
        true_positives=total_true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        precision=precision,
        recall=recall,
        f1=f1,
        onset_error_p50_seconds=_percentile_nearest_rank(all_errors, 0.50),
        onset_error_p95_seconds=_percentile_nearest_rank(all_errors, 0.95),
        classes=class_metrics,
    )
