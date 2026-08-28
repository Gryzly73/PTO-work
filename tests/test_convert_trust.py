"""Надёжность листа, предупреждения и проверка чисел (service/convert.py).

Это то, чем модель-сверщик отличает текст из слоя от описания по картинке
и от дыры на месте упавшего вызова модели. Ломаться здесь нельзя молча.
"""
from __future__ import annotations

from service import convert


def test_strip_model_errors_handles_nested_parens_and_json() -> None:
    raw = (
        "--- r1c1 ---\n"
        "(ошибка тайла: (Request ID: Root=1-abc;def)\n\n"
        "Bad request:\n{'message': \"model 'X' is not supported\"})\n"
        "нормальный текст\n"
        "(ошибка описания: timeout)\n"
    )
    text, count = convert.strip_model_errors(raw)
    assert count == 2
    assert "Request ID" not in text
    assert "Bad request" not in text
    assert "нормальный текст" in text
    assert text.count(convert._MODEL_ERROR_MARK) == 2


def test_strip_model_errors_is_noop_without_errors() -> None:
    text, count = convert.strip_model_errors("просто текст (в скобках)")
    assert count == 0
    assert text == "просто текст (в скобках)"


def test_trust_levels() -> None:
    base = dict(layer_is_source=False, layer_text="", flow="", tables=[], pass_a="", pass_b="")
    assert convert.assess_trust(vector=True, **base)["level"] == convert.TRUST_DWG
    layer = convert.assess_trust(
        vector=False, layer_is_source=True, layer_text="текст", flow="текст",
        tables=[], pass_a="", pass_b="",
    )
    assert layer["level"] == convert.TRUST_LAYER
    assert layer["warnings"] == []
    vlm = convert.assess_trust(
        vector=False, layer_is_source=False, layer_text="", flow="", tables=[],
        pass_a="описание по картинке", pass_b="",
    )
    assert vlm["level"] == convert.TRUST_VLM
    assert any("по изображению" in w for w in vlm["warnings"])
    none = convert.assess_trust(vector=False, **base)
    assert none["level"] == convert.TRUST_NONE
    assert none["warnings"]


def test_trust_reports_model_errors_and_mock() -> None:
    trust = convert.assess_trust(
        vector=False, layer_is_source=True, layer_text="текст", flow="текст",
        tables=[], pass_a="", pass_b="", model_errors=3, mock=True,
    )
    joined = " ".join(trust["warnings"])
    assert "3 фрагмента" in joined
    assert "mock" in joined


def test_check_numbers_flags_numbers_missing_from_document() -> None:
    reference = "Расход воды 12,5 м³/сут, напор 34 м, насос марки К-45."
    model = "Расход 12,5 м³/сут при напоре 34 м; насос К-45, мощность 7,5 кВт, 220 В."
    result = convert.check_numbers(reference, model)
    assert result is not None and result["checked"]
    assert "7.5" in result["suspect"] and "220" in result["suspect"]
    assert "12.5" not in result["suspect"]
    assert result["found"] == result["total"] - len(result["suspect"])
    assert 0 < result["precision"] < 100


def test_check_numbers_needs_a_layer() -> None:
    assert convert.check_numbers("", "число 123") is None
    assert convert.check_numbers("слишком короткий эталон", "число 123") is None


def test_numbers_line_wording() -> None:
    assert "не выполнялась" in convert.numbers_line(None)
    ok = {"checked": True, "total": 4, "found": 4, "precision": 100.0, "suspect": []}
    assert "все 4 чисел" in convert.numbers_line(ok)
    bad = {"checked": True, "total": 4, "found": 2, "precision": 50.0, "suspect": ["77", "88"]}
    line = convert.numbers_line(bad)
    assert "2 из 4" in line and "77" in line and "выдуманы" in line


def test_build_page_markdown_prints_trust_and_numbers() -> None:
    trust = {"level": convert.TRUST_LAYER, "title": "высокая", "warnings": ["дыра"]}
    numbers = {"checked": True, "total": 1, "found": 0, "precision": 0.0, "suspect": ["99"]}
    md = convert.build_page_markdown(
        page_number=1, file_name="x.pdf", kind="text", flow="Абзац",
        trust=trust, numbers=numbers,
    )
    assert "**Надёжность:** высокая" in md
    assert "- дыра" in md
    assert "**Проверка чисел:**" in md and "99" in md
    # Метка «**Файл:**» — контракт с фронтом, без неё лист считается пустым.
    assert "**Файл:** `x.pdf`" in md


def test_layer_sourced_page_has_no_duplicate_description() -> None:
    """На текстовом листе со слоем описание повторяло весь слой — убрано."""
    md = convert.build_page_markdown(
        page_number=1, file_name="x.pdf", kind="text", flow="Уникальная фраза",
        pass_a="", trust={"level": convert.TRUST_LAYER, "title": "", "warnings": []},
    )
    assert md.count("Уникальная фраза") == 1


def test_sheet_map_only_for_drawing_pages() -> None:
    from service.flow import empty_sheet_map

    for kind in ("text", "table", "mixed"):
        md = convert.build_page_markdown(page_number=1, file_name="x.pdf", kind=kind, flow="Абзац")
        assert "Что где на листе" not in md
    md = convert.build_page_markdown(
        page_number=1, file_name="x.pdf", kind="drawing", flow="Абзац", sheet_map=empty_sheet_map()
    )
    assert "Что где на листе" in md
    assert "| Блок | Где на листе | Объём |" in md
    assert "| подписи |" not in md
