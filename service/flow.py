"""Лист в том порядке, в каком он свёрстан в исходнике.

Раньше страница собиралась фиксированными секциями: сначала описание, потом
все таблицы, потом весь текст сплошным куском. Для инженера это разрывало
документ: примечание, которое в исходнике стоит под таблицей, уезжало на
экран за десяток абзацев от неё, а из потока слов уже не было видно, к какой
таблице оно относится.

Здесь лист собирается так же, как он напечатан: блок за блоком сверху вниз, и
таблица стоит на своём месте между абзацами. Порядок и разбиение берутся из
самого PDF (координаты блоков текстового слоя и рамки таблиц), а не выводятся
моделью, поэтому и структура ответа совпадает с исходником, а не пересказывает
его.

Здесь только PDF. У чертежа тот же порядок строится из данных DXF —
`dwg_sheets.py`.
"""
from __future__ import annotations

from pathlib import Path

import fitz

# Слова, по которым узнаётся основная надпись листа (штамп по ГОСТ 21.101).
# Геометрией его не отличить: на альбомном листе он в правом нижнем углу, на
# книжном — там же, но занимает всю ширину, а на титуле штампа нет вовсе.
_STAMP_WORDS = ("изм.", "кол.уч", "№ док", "n°док", "подп.", "инв. №", "взам")

# Заголовок раздела: «1 ОБЩИЕ СВЕДЕНИЯ», «1.1. Исходные данные», «Приложение А».
_HEADING_STARTS = ("приложение", "раздел", "подраздел", "таблица", "рисунок")

_MAX_HEADING_CHARS = 90


def _zone(rect: tuple[float, float, float, float], page: fitz.Rect) -> str:
    """Где блок лежит на листе, словами. Словарь тот же, что у чертежей.

    Ось Y в PDF растёт вниз, поэтому «север» — это малые y, а не большие: без
    поправки карта листа получилась бы перевёрнутой.
    """
    width = max(page.width, 1e-9)
    height = max(page.height, 1e-9)
    if (rect[2] - rect[0]) / width >= 0.7 and (rect[3] - rect[1]) / height >= 0.7:
        return "по всему листу"
    cx = ((rect[0] + rect[2]) / 2 - page.x0) / width
    cy = ((rect[1] + rect[3]) / 2 - page.y0) / height
    vertical = "север" if cy < 1 / 3 else ("юг" if cy > 2 / 3 else "")
    horizontal = "запад" if cx < 1 / 3 else ("восток" if cx > 2 / 3 else "")
    if vertical and horizontal:
        return f"{vertical}о-{horizontal}"
    return vertical or horizontal or "центр"


def _overlap(block: tuple[float, float, float, float], table: fitz.Rect) -> float:
    """Какая доля блока лежит внутри таблицы."""
    x0 = max(block[0], table.x0)
    y0 = max(block[1], table.y0)
    x1 = min(block[2], table.x1)
    y1 = min(block[3], table.y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    area = max((block[2] - block[0]) * (block[3] - block[1]), 1e-9)
    return (x1 - x0) * (y1 - y0) / area


def _looks_like_stamp(text: str) -> bool:
    low = text.lower()
    return sum(1 for word in _STAMP_WORDS if word in low) >= 2


def _looks_like_heading(text: str) -> bool:
    if len(text) > _MAX_HEADING_CHARS or "\n" in text.strip():
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[0].isdigit() and any(ch.isalpha() for ch in stripped):
        return True
    return stripped.lower().startswith(_HEADING_STARTS)


def page_elements(
    pdf_path: Path, page_number: int, *, extra_text: str = ""
) -> list[dict]:
    """Блоки листа в порядке исходника: [{kind, rect, zone, text|md}, ...].

    kind: `heading` | `text` | `table` | `stamp`.
    """
    from deglyph import map_for_doc, page_blocks_fixed
    from pdf_tables import frames_for_doc, page_tables_placed

    with fitz.open(pdf_path) as doc:
        page = doc[page_number - 1]
        try:
            glyph_map = map_for_doc(doc, quiet=True, extra_text=extra_text)
        except Exception:
            glyph_map = {}
        try:
            tables = page_tables_placed(page, glyph_map, frames_for_doc(doc))
        except Exception:
            tables = []
        try:
            blocks = page_blocks_fixed(page, extra_text=extra_text)
        except Exception:
            blocks = []
        page_rect = page.rect

        elements: list[dict] = []
        for rect, text in blocks:
            # Текст, попавший внутрь таблицы, второй раз не печатаем: он уже
            # разложен по её ячейкам.
            if any(_overlap(rect, table_rect) > 0.6 for table_rect, _ in tables):
                continue
            if _looks_like_stamp(text):
                kind = "stamp"
            elif _looks_like_heading(text):
                kind = "heading"
            else:
                kind = "text"
            elements.append(
                {
                    "kind": kind,
                    "rect": rect,
                    "zone": _zone(rect, page_rect),
                    "text": text,
                }
            )
        for index, (table_rect, table_md) in enumerate(tables, start=1):
            rect = (table_rect.x0, table_rect.y0, table_rect.x1, table_rect.y1)
            elements.append(
                {
                    "kind": "table",
                    "rect": rect,
                    "zone": _zone(rect, page_rect),
                    "text": table_md,
                    "number": index,
                }
            )

    elements.sort(key=lambda item: (round(item["rect"][1], 1), round(item["rect"][0], 1)))
    return elements


_KIND_TITLE = {
    "heading": "заголовок",
    "text": "текст",
    "table": "таблица",
    "stamp": "штамп",
}


def _volume(element: dict) -> str:
    """Объём блока — строками, а у таблицы размером сетки."""
    if element["kind"] == "table":
        rows = [ln for ln in element["text"].splitlines() if ln.strip().startswith("|")]
        columns = rows[0].count("|") - 1 if rows else 0
        # Вторая строка GFM — разделитель заголовка, в счёт строк не идёт.
        return f"{max(len(rows) - 1, 0)}×{max(columns, 0)}"
    lines = [ln for ln in element["text"].splitlines() if ln.strip()]
    return f"{len(lines)} стр."


# Шапка карты листа. Одна и та же и у заполненной карты, и у пустой
# заготовки для чертежа — чтобы разбор чертежей дописывал строки в готовую
# таблицу, а не придумывал свою.
SHEET_MAP_HEADER = ("| Блок | Где на листе | Объём |", "|---|---|---|")


def empty_sheet_map() -> str:
    """Пустая карта листа — заготовка для чертежа.

    Сервис сам её не заполняет: строки «текст — север — 1 стр.» по координатам
    текстового слоя на текстовом и табличном листе только повторяли то, что и
    так видно в потоке, а на чертеже были слишком грубы. Секция оставлена
    пустой, чтобы разбор чертежей положил сюда свою карту (см.
    `sheet_map_markdown` — готовая сборка по блокам, если она подойдёт).
    """
    return "\n".join(SHEET_MAP_HEADER)


def sheet_map_markdown(elements: list[dict]) -> str:
    """Карта листа: что за блок, где на листе, какого объёма.

    Пишется для модели, а не для чтения вслух: строки однообразны, содержимое
    не пересказывается — оно идёт ниже дословно. Задача карты — дать опору
    «таблица расходов в центре, примечания под ней, штамп в правом нижнем
    углу», чтобы вопрос про «правый нижний угол» не приходилось угадывать.

    Сервисом сейчас не вызывается: на листах текста и таблиц карта не нужна,
    а для чертежа выводится пустая заготовка (`empty_sheet_map`). Оставлена
    как готовый инструмент для разбора чертежей.
    """
    if not elements:
        return ""
    rows = list(SHEET_MAP_HEADER)
    counter = 0
    for element in elements:
        title = _KIND_TITLE.get(element["kind"], element["kind"])
        if element["kind"] == "table":
            counter += 1
            title = f"таблица {counter}"
        rows.append(f"| {title} | {element['zone']} | {_volume(element)} |")
    return "\n".join(rows)


def flow_markdown(elements: list[dict]) -> str:
    """Содержимое листа подряд, в порядке исходника.

    Заголовки остаются заголовками, таблица стоит между абзацами там, где она
    напечатана, штамп — в конце, как и на листе.
    """
    parts: list[str] = []
    counter = 0
    for element in elements:
        kind = element["kind"]
        text = element["text"].strip()
        if not text:
            continue
        if kind == "heading":
            parts += [f"### {text}", ""]
        elif kind == "table":
            counter += 1
            parts += [f"**Таблица {counter}**", "", text, ""]
        elif kind == "stamp":
            parts += ["**Штамп листа:**", "", text, ""]
        else:
            parts += [text, ""]
    return "\n".join(parts).strip()
