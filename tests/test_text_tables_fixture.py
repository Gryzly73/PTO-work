"""Регрессия пути текста и таблиц на настоящих страницах ИОС2.

Фикстура — страницы 3–20 документа: «Содержание» и «Состав проекта» на
сломанном шрифте ISOCPEUR, где 28.08.2026 нашлись три дефекта:

  * рамка таблицы во весь лист съедала текст вне сетки (приложения, штамп,
    номера томов) — страница 5 давала 51 % слов;
  * карта глифов портила здоровый шрифт: «Block 1-7» → «#MPDL 1-7»;
  * запятая сломанного шрифта приходила как «\\r» и терялась.

Проверяется тем же способом, что и замер (`bench_text_layer.py`): лист
собирается без модели и сверяется с текстовым слоем той же страницы.
"""
from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from score_vs_pdftext import score_page
from service.convert import page_to_frontend


def _page(pdf: Path, number: int) -> tuple[dict, str, dict]:
    page = page_to_frontend(page_number=number, file_name="ios2.pdf", raw_page_md="", pdf_path=pdf)
    body = "\n".join(
        ln
        for ln in page["markdown"].split("## Информация о листе")[0].splitlines()
        if not ln.startswith("# Лист ")
    )
    with fitz.open(pdf) as doc:
        raw = doc[number - 1].get_text()
    layer = page["extractedText"] or raw
    return page, body, score_page(layer, body)


@pytest.mark.parametrize("number", [1, 2, 3, 4, 5])
def test_contents_pages_keep_words_and_numbers(ios2_pdf: Path, number: int) -> None:
    page, body, score = _page(ios2_pdf, number)
    assert page["trust"]["level"] == "layer"
    assert len(page["tables"]) == 1, "на странице «Содержание» одна таблица"
    assert score["token_recall"] >= 98.0, score["missed_words"]
    assert score["num_recall"] == 100.0, score["missed_nums"]
    assert score["num_precision"] == 100.0, score["hallucinated_nums"]
    assert "Штамп листа" in body, "штамп под рамкой таблицы терялся"
    assert "#MPDL" not in body, "карта глифов портит здоровый шрифт"


def test_rows_outside_grid_survive(ios2_pdf: Path) -> None:
    # Страница 5 документа (3-я в фикстуре): приложения ниже сетки таблицы.
    _, body, _ = _page(ios2_pdf, 3)
    for needle in ("Прилагаемые документы", "Приложение 1", "Приложение 6", "Block 1-7"):
        assert needle in body, needle


def test_commas_in_table_cells(ios2_pdf: Path) -> None:
    page, _, _ = _page(ios2_pdf, 3)
    table = page["tables"][0]
    assert "В0, В1, В3" in table, "запятая сломанного шрифта («\\r») теряется"


def test_volume_numbers_survive(ios2_pdf: Path) -> None:
    # Страница 6 документа: номера томов «5.2»…«5.5» стоят отдельными
    # строками и раньше считались «разложенными по ячейкам».
    _, body, score = _page(ios2_pdf, 4)
    assert score["num_recall"] == 100.0, score["missed_nums"]


@pytest.mark.parametrize("number", [6, 9, 12, 18])
def test_plain_text_pages(ios2_pdf: Path, number: int) -> None:
    page, body, score = _page(ios2_pdf, number)
    assert page["trust"]["level"] == "layer"
    assert score["token_recall"] >= 98.0, score["missed_words"]
    assert score["num_recall"] >= 99.0, score["missed_nums"]
    assert page["numbers"] is None or page["numbers"]["total"] == 0, "модели не было — проверять нечего"
