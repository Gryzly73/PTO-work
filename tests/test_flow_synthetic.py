"""Сборка листа в порядке исходника на синтетическом PDF.

PDF рисуется прямо в тесте: заголовок, таблица с сеткой, абзац под ней,
строки, которые лежат внутри общей рамки листа, но не в сетке таблицы, и
штамп. Так проверяется само правило отсева: текст, разложенный по ячейкам,
не печатается дважды, а текст под рамкой, которого в таблице нет, не
теряется.
"""
from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from service.convert import page_to_frontend
from service.flow import _covered, _words, page_elements

# Встроенный шрифт MuPDF с кириллицей, без файлов: у base14 («helv»)
# кириллицы нет, текст на странице превращается в точки.
FONT = "china-s"


def _make_pdf(path: Path, *, frame: bool) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    # Заголовок
    page.insert_text((60, 60), "1 ОБЩИЕ СВЕДЕНИЯ", fontname=FONT, fontsize=14)
    page.insert_text((60, 85), "Расход воды 12,5 куб.м в сутки, напор 34 м.", fontname=FONT, fontsize=11)
    # Таблица 3×4 с сеткой
    # Колонки широкие, кегль мелкий: у «china-s» широкие глифы, и при 150 pt
    # хвост «сетевой» вылезал в соседнюю ячейку.
    x0, y0, cw, rh = 45, 110, 170, 22
    rows = [
        ["Позиция", "Наименование", "Кол-во"],
        ["1", "Насос К-45, сетевой", "2"],
        ["2", "Задвижка 30ч6бр", "4"],
        ["3", "Фильтр ФС-100", "1"],
    ]
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            rect = fitz.Rect(x0 + c * cw, y0 + r * rh, x0 + (c + 1) * cw, y0 + (r + 1) * rh)
            page.draw_rect(rect, width=0.7)
            page.insert_text((rect.x0 + 4, rect.y1 - 6), text, fontname=FONT, fontsize=8)
    # Абзац под таблицей и строки, которых в сетке нет
    page.insert_text((60, 230), "Примечание: количество уточняется по спецификации.", fontname=FONT, fontsize=11)
    page.insert_text((60, 260), "Прилагаемые документы", fontname=FONT, fontsize=11)
    page.insert_text((60, 280), "Приложение 1. Баланс водопотребления, том 5.2", fontname=FONT, fontsize=11)
    # Штамп
    page.insert_text((60, 800), "Изм. Кол.уч. Лист №док Подп. Дата   Взам. инв. №", fontname=FONT, fontsize=8)
    if frame:
        # Рамка листа во весь лист — как на страницах «Содержание».
        page.draw_rect(fitz.Rect(40, 40, 555, 820), width=1.0)
    doc.save(path)
    doc.close()


@pytest.mark.parametrize("frame", [False, True])
def test_flow_keeps_text_outside_grid(tmp_path: Path, frame: bool) -> None:
    pdf = tmp_path / f"synthetic_{int(frame)}.pdf"
    _make_pdf(pdf, frame=frame)
    page = page_to_frontend(page_number=1, file_name=pdf.name, raw_page_md="", pdf_path=pdf)
    body = page["markdown"].split("## Информация о листе")[0]
    assert page["trust"]["level"] == "layer"
    for needle in (
        "ОБЩИЕ СВЕДЕНИЯ",
        "12,5",
        "Примечание",
        "Прилагаемые документы",
        "Приложение 1",
        "5.2",
        "Штамп листа",
    ):
        assert needle in body, needle
    if page["tables"]:
        table = page["tables"][0]
        assert "Насос К-45, сетевой" in table
        assert "30ч6бр" in table
        # Строки таблицы не печатаются второй раз как текст.
        assert body.count("30ч6бр") == 1


def test_covered_rule() -> None:
    table_words = _words("| 1 | Насос К-45, сетевой | 2 |\n| 2 | Задвижка 30ч6бр | 4 |")
    assert _covered("Насос К-45, сетевой", table_words)
    assert not _covered("Прилагаемые документы", table_words)
    # Короткая строка из номера тома не считается пустой и не «покрыта».
    assert not _covered("5.2", table_words)
    assert "5.2" in _words("том 5.2")


def test_page_elements_are_in_reading_order(tmp_path: Path) -> None:
    pdf = tmp_path / "order.pdf"
    _make_pdf(pdf, frame=False)
    kinds = [e["kind"] for e in page_elements(pdf, 1)]
    assert kinds[0] == "heading"
    assert kinds[-1] == "stamp"
