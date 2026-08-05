"""Темповые автоматизации GPIF: единица темпа, позиция в такте, базовый темп.

Синтетика вместо фикстур: известные .gp-файлы пишут автоматизацию в такт 0
с Position 0 и единицей 2 (четверть), поэтому регрессионные фикстуры эти
ветки не покрывают.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from gp_import import (
    GPTempoAutomation,
    _extract_gpif_tempo_automations_root,
    adapt_apollotab_measure,
    adapt_apollotab_song,
)


def _gpif_root(*automations: str) -> ET.Element:
    return ET.fromstring(
        "<GPIF><MasterTrack><Automations>"
        + "".join(automations)
        + "</Automations></MasterTrack></GPIF>"
    )


def _automation(value: str, bar: int = 0, position: str = "0") -> str:
    return (
        "<Automation><Type>Tempo</Type><Linear>false</Linear>"
        f"<Bar>{bar}</Bar><Position>{position}</Position>"
        f"<Visible>true</Visible><Value>{value}</Value></Automation>"
    )


@pytest.mark.parametrize(
    ("value", "expected_bpm"),
    [
        ("120 2", 120.0),   # четверти — как есть
        ("240 1", 120.0),   # восьмые: 240 восьмых/мин = 120 четвертей/мин
        ("80 3", 120.0),    # четверть с точкой: 80 * 1.5
        ("60 4", 120.0),    # половинная: 60 * 2
        ("40 5", 120.0),    # половинная с точкой: 40 * 3
        ("120", 120.0),     # без единицы — как есть
    ],
)
def test_gpif_tempo_value_unit_is_normalized_to_quarter_bpm(value, expected_bpm):
    automations = _extract_gpif_tempo_automations_root(_gpif_root(_automation(value)))
    assert len(automations) == 1
    assert automations[0].value == pytest.approx(expected_bpm)


def test_gpif_tempo_unknown_unit_falls_back_to_quarters():
    automations = _extract_gpif_tempo_automations_root(_gpif_root(_automation("120 9")))
    assert automations[0].value == pytest.approx(120.0)


def _rest_beat():
    return SimpleNamespace(
        duration=SimpleNamespace(value=4),  # четверть
        is_dotted=False,
        tuplet_numerator=-1,
        tuplet_denominator=-1,
        notes=[],
        is_rest=True,
        dynamics=None,
        crescendo=None,
        grace_type=None,
        tremolo_picking=0,
    )


def test_tempo_automation_position_is_quarters_not_ticks():
    """Position=2 — третья доля. Раньше "2 <= rel_start(тики)" схлопывал любую
    ненулевую позицию к началу такта (условие выполнялось уже на второй доле)."""
    measure = SimpleNamespace(time_signature=(4, 4), beats=[_rest_beat() for _ in range(4)])

    adapted = adapt_apollotab_measure(
        measure, 960,
        tempo_automations=[GPTempoAutomation(bar=0, position=2.0, value=140.0)],
    )

    beats = adapted.voices[0].beats
    with_change = [index for index, beat in enumerate(beats) if beat.effect.mixTableChange is not None]
    assert with_change == [2]
    assert beats[2].effect.mixTableChange.tempo.value == pytest.approx(140.0)


def test_tempo_automation_at_bar_start_still_lands_on_first_beat():
    measure = SimpleNamespace(time_signature=(4, 4), beats=[_rest_beat() for _ in range(4)])

    adapted = adapt_apollotab_measure(
        measure, 960,
        tempo_automations=[GPTempoAutomation(bar=0, position=0.0, value=140.0)],
    )

    with_change = [index for index, beat in enumerate(adapted.voices[0].beats)
                   if beat.effect.mixTableChange is not None]
    assert with_change == [0]


def _song(tempo: float):
    return SimpleNamespace(title="", artist="", album="", tempo=tempo, tracks=[])


def test_base_tempo_ignores_automation_that_is_not_at_score_start():
    """Автоматизация в такте 3 не должна становиться темпом вступления: иначе
    вступление играет чужим темпом, а в реальной точке смены set_tempo
    дедуплицируется как равное значение."""
    song = adapt_apollotab_song(
        _song(90.0),
        tempo_automations=[GPTempoAutomation(bar=3, position=0.0, value=180.0)],
    )
    assert song.tempo == pytest.approx(90.0)


def test_base_tempo_uses_automation_at_bar_zero_position_zero():
    song = adapt_apollotab_song(
        _song(90.0),
        tempo_automations=[GPTempoAutomation(bar=0, position=0.0, value=180.0)],
    )
    assert song.tempo == pytest.approx(180.0)
