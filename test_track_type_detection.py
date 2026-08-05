"""Детекция типа трека по имени: стоп-слова против гитарных ключевых слов."""
from __future__ import annotations

import pytest

from gp_to_shreddage import detect_track_type


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Стоп-слово "string" отправляло эти очень распространённые имена в
        # OTHER: ноты копировались как есть, маппинг артикуляций терялся.
        ("12 String Guitar", "GUITAR"),
        ("Steel String Acoustic", "GUITAR"),
        # Оркестровые/синтетические струнные — всегда во множественном числе.
        ("Strings", "OTHER"),
        ("Solo Strings", "OTHER"),
        ("Synth Strings", "OTHER"),
        ("String Ensemble", "OTHER"),   # нет ни стоп-, ни ключевых слов
        # Регресс-щиты для существующего порядка проверок.
        ("Lead Vocals", "OTHER"),       # "lead" не делает вокал гитарой
        ("Bass Guitar", "BASS"),        # бас проверяется раньше гитары
        ("Contrabass", "OTHER"),        # стоп-слово раньше "bass"
        ("Guitar", "GUITAR"),
        ("", "OTHER"),
    ],
)
def test_detect_track_type(name, expected):
    assert detect_track_type(name) == expected
