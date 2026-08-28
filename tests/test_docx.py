"""Чтение пояснительной записки .docx — текст, таблицы, основная надпись.

Записка — половина раздела: у комплекта «Жуковский» шесть таких файлов.
Читается данными, без модели, поэтому проверять можно на настоящих числах:
что прочитано — то и лежит в файле.

Фикстуры собираются здесь же, минимальным .docx из zip и XML: тащить в
репозиторий записку заказчика нельзя.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import docx_text as dx
from service.convert import TRUST_DOCX, page_to_frontend
from service.pipeline import document_sheets, is_docx, without_model

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _para(text: str, *, style: str = "", page_break: bool = False) -> str:
    props = ""
    if style or page_break:
        props = "<w:pPr>"
        if page_break:
            props += "<w:pageBreakBefore/>"
        if style:
            props += f'<w:pStyle w:val="{style}"/>'
        props += "</w:pPr>"
    return f"<w:p>{props}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _cell(text: str, *, span: int = 1, merged: bool = False) -> str:
    props = ""
    if span > 1 or merged:
        props = "<w:tcPr>"
        if span > 1:
            props += f'<w:gridSpan w:val="{span}"/>'
        if merged:
            props += "<w:vMerge/>"
        props += "</w:tcPr>"
    return f"<w:tc>{props}<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"


def _table(rows: list[list[str]]) -> str:
    out = ["<w:tbl>"]
    for row in rows:
        out.append("<w:tr>" + "".join(_cell(c) for c in row) + "</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def make_docx(path: Path, body: str, *, header: str = "") -> Path:
    document = f'<?xml version="1.0"?><w:document {NS}><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
        if header:
            archive.writestr(
                "word/header1.xml",
                f'<?xml version="1.0"?><w:hdr {NS}><w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>',
            )
    return path


@pytest.fixture()
def note(tmp_path: Path) -> Path:
    """Записка: заголовок, абзацы, таблица и основная надпись в колонтитуле."""
    body = (
        _para("1. Общие сведения", style="Heading1")
        + _para("Расход воды на хозяйственно-питьевые нужды 12,5 м3/сут.")
        + _table(
            [
                ["Позиция", "Наименование", "Кол-во"],
                ["1", "Насос К-45", "2"],
                ["2", "Задвижка 30ч6бр", "4"],
            ]
        )
        + _para("Примечание: количество уточняется по спецификации.")
    )
    header = 'Изм. Кол.уч №док. Подп. Дата 28-ХСА-1/25-КР 1 .ПЗ Лист 26 ООО « КУРСКРЕГИОНПРОЕКТ » Текстовая часть'
    return make_docx(tmp_path / "записка.docx", body, header=header)


def test_reads_text_and_tables(note: Path) -> None:
    sheets = dx.read_sheets(note)
    assert len(sheets) == 1
    kinds = [b.kind for b in sheets[0].blocks]
    assert kinds == ["heading", "text", "table", "text"]
    table = next(b for b in sheets[0].blocks if b.kind == "table")
    assert "| Позиция | Наименование | Кол-во |" in table.text
    assert "| 1 | Насос К-45 | 2 |" in table.text
    assert "30ч6бр" in table.text


def test_numbers_survive_verbatim(note: Path) -> None:
    body, _ = dx.page_markdown(note, 1)
    for number in ("12,5", "30ч6бр", "К-45"):
        assert number in body, number


def test_stamp_from_header(note: Path) -> None:
    found = dx.read_stamp(note)
    # В колонтитуле Word шифр разорван на прогоны: «28-ХСА-1/25-КР 1 .ПЗ».
    assert found.code == "28-ХСА-1/25-КР1.ПЗ"
    assert found.section == "28-ХСА-1/25-КР1", "записка должна встать в раздел чертежей"
    assert found.org == "ООО «КУРСКРЕГИОНПРОЕКТ»"
    assert found.title == "Текстовая часть"
    assert found.source == "DOCX"


def test_no_header_is_honest(tmp_path: Path) -> None:
    plain = make_docx(tmp_path / "без колонтитула.docx", _para("Текст"))
    found = dx.read_stamp(plain)
    assert found.code == ""
    assert "не прочитана" in found.note or "не найден" in found.note


def test_one_sheet_without_page_breaks(note: Path) -> None:
    assert dx.sheet_count(note) == 1
    body, _ = dx.page_markdown(note, 1)
    assert "разрывов страниц в файле нет" in body


def test_splits_on_explicit_page_breaks(tmp_path: Path) -> None:
    body = (
        _para("Первый лист")
        + _para("Второй лист", page_break=True)
        + _para("Третий лист", page_break=True)
    )
    path = make_docx(tmp_path / "с разрывами.docx", body)
    assert dx.sheet_count(path) == 3
    first, _ = dx.page_markdown(path, 1)
    third, _ = dx.page_markdown(path, 3)
    assert "Первый лист" in first and "Второй лист" not in first
    assert "Третий лист" in third


def test_merged_cells_do_not_shift_columns(tmp_path: Path) -> None:
    tbl = (
        "<w:tbl>"
        "<w:tr>" + _cell("Шапка", span=3) + "</w:tr>"
        "<w:tr>" + _cell("а") + _cell("б") + _cell("в") + "</w:tr>"
        "</w:tbl>"
    )
    path = make_docx(tmp_path / "объединения.docx", tbl)
    table = next(b for b in dx.read_sheets(path)[0].blocks if b.kind == "table")
    rows = [ln for ln in table.text.splitlines() if ln.startswith("|")]
    assert rows[0].count("|") == rows[-1].count("|"), "колонки разъехались"
    assert "а" in rows[-1] and "в" in rows[-1]


def test_table_heavy_sheet_is_kind_table(tmp_path: Path) -> None:
    rows = [["Позиция", "Наименование", "Кол-во"]] + [
        [str(i), f"Изделие {i} с длинным наименованием", "10"] for i in range(1, 12)
    ]
    path = make_docx(tmp_path / "спецификация.docx", _para("Итого") + _table(rows))
    _, kind = dx.page_markdown(path, 1)
    assert kind == "table"


def test_pipeline_treats_docx_as_data(note: Path) -> None:
    assert is_docx(note) and without_model(note)
    assert document_sheets(note) == 1


def test_page_for_frontend(note: Path) -> None:
    raw, _ = dx.page_markdown(note, 1)
    page = page_to_frontend(
        page_number=1, file_name=note.name, raw_page_md=raw, pdf_path=note
    )
    assert page["trust"]["level"] == TRUST_DOCX
    assert page["warnings"] == []
    # Модели не было — проверять числа не с чем, и в листе этой строки нет.
    assert page["numbers"] is None
    assert "Проверка чисел" not in page["markdown"]
    assert "Текст записки (из DOCX, дословно)" in page["markdown"]
    assert "12,5" in page["markdown"]


def test_bundle_puts_note_next_to_drawings(note: Path) -> None:
    """Записка и чертёж одного раздела должны попасть в один раздел отчёта."""
    import bundle

    variants = bundle.read_source(note, name="записка.docx")
    assert len(variants) == 1
    variant = variants[0]
    assert variant.source_kind == "DOCX"
    assert variant.stamp.section == "28-ХСА-1/25-КР1"
    sections = bundle.build(variants)
    assert [s.code for s in sections] == ["28-ХСА-1/25-КР1"]
    assert sections[0].documents[0].kind == "текстовая часть"


def test_broken_file_raises(tmp_path: Path) -> None:
    broken = tmp_path / "битый.docx"
    broken.write_bytes(b"not a zip at all")
    with pytest.raises(Exception):
        dx.read_sheets(broken)
    # Штамп на битом файле не падает, а честно молчит.
    assert dx.read_stamp(broken).code == ""
