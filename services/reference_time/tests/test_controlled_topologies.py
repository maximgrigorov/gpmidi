"""Controlled topology fixtures for gap, repeat, and one-to-many mapping."""

from reference_time.mapping import align_measures
from reference_time.models import GPMeasure, SourceMeasure

GP_SHA = "f" * 64


def _sources(count: int) -> list[SourceMeasure]:
    return [
        SourceMeasure(
            index=index,
            tick_start=index * 1920,
            tick_end=(index + 1) * 1920,
            seconds_start=index * 2.0,
            seconds_end=(index + 1) * 2.0,
            numerator=4,
            denominator=4,
            tempo_bpm=120.0,
            confidence=1.0,
        )
        for index in range(count)
    ]


def _gp(count: int, *, repeat_span: bool = False) -> list[GPMeasure]:
    return [
        GPMeasure(
            gp_revision_sha256=GP_SHA,
            measure_index=index,
            measure_number=index + 1,
            tick_start=index * 3840,
            tick_end=(index + 1) * 3840,
            numerator=4,
            denominator=4,
            has_repeat_open=repeat_span and index == 0,
            has_repeat_close=repeat_span and index == count - 1,
            repeat_close_count=2 if repeat_span and index == count - 1 else 0,
        )
        for index in range(count)
    ]


def _topology(source_count: int, gp_count: int, *, repeat_span: bool = False):
    result = align_measures(_sources(source_count), _gp(gp_count, repeat_span=repeat_span))
    return [
        (mapping.source_measure_index, mapping.gp_measure_index, mapping.mapping_type.value)
        for mapping in result.mappings
    ]


def test_controlled_source_gap_topology():
    assert _topology(4, 2) == [
        (0, None, "source_gap"),
        (1, None, "source_gap"),
        (2, 0, "one_to_one"),
        (3, 1, "one_to_one"),
    ]


def test_controlled_gp_gap_topology():
    assert _topology(2, 4) == [
        (0, 0, "one_to_one"),
        (1, 1, "one_to_one"),
        (-1, 2, "gp_gap"),
        (-1, 3, "gp_gap"),
    ]


def test_controlled_one_to_many_requires_declared_repeat_span():
    without_repeat = _topology(4, 2)
    with_repeat = _topology(4, 2, repeat_span=True)

    assert all(mapping_type != "repeat" for _, _, mapping_type in without_repeat)
    assert with_repeat == [
        (0, 0, "one_to_one"),
        (1, 0, "repeat"),
        (2, 0, "repeat"),
        (3, 1, "one_to_one"),
    ]
