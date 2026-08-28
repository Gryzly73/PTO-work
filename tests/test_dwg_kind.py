"""Тип листа чертежа: таблица с сеткой — не чертёж."""
from __future__ import annotations

from collections import Counter

from dwg_sheets import KIND_PLAN, KIND_TEXT, LayerStat, sheet_kind


def _layers(length_m: float, entities: int) -> list[LayerStat]:
    return [LayerStat("0", entities, length_m, "центр")]


def test_no_geometry_is_text() -> None:
    assert sheet_kind([], texts=88) == KIND_TEXT


def test_table_grid_is_text() -> None:
    # Состав проекта на комплекте «Жуковский»: 3 м линий, 60 объектов, 24 подписи, 1 блок.
    assert sheet_kind(_layers(3.2, 60), texts=24, blocks=Counter({"штамп": 1})) == KIND_TEXT


def test_drawing_by_length() -> None:
    # Узел цоколя: 430 м линий.
    assert sheet_kind(_layers(430.4, 1326), texts=255, blocks=Counter()) == KIND_PLAN


def test_drawing_by_blocks() -> None:
    assert sheet_kind(_layers(5.0, 40), texts=10, blocks=Counter({"колонна": 5})) == KIND_PLAN


def test_sketch_with_few_labels_is_drawing() -> None:
    assert sheet_kind(_layers(10.0, 400), texts=3) == KIND_PLAN
