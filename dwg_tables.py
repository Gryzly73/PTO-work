"""Таблицы чертежа: собираются из сетки линий и текста, лежащего в ячейках.

Зачем. Состав проекта, экспликация помещений, спецификация оборудования,
ведомость чертежей — в чертеже это не сущность «таблица», а полсотни отрезков
и десятки подписей рядом. В потоке текста такая таблица разваливается: у
инженера на экране лента значений, где не видно, к какой строке относится
«28-ХСА-1/25-ИОС5» и что стоит в соседней колонке. AutoCAD-сущность ACAD_TABLE
в присланных комплектах не встретилась ни разу — рисуют линиями.

Приём тот же, что в `pdf_tables.py` для PDF: сетку берём из геометрии, текст
раскладываем по ячейкам координатами. Ни одного обращения к модели, ни одного
шанса на выдумку — значения переносятся ровно те, что стоят в файле.
"""
from __future__ import annotations

from collections import defaultdict

# Насколько две линии считаются лежащими на одной прямой: доля от размера
# таблицы. Чертёж рисуют по сетке, но конвертер оставляет расхождения в
# десятые доли единицы.
_SNAP = 0.004

# Минимальный размер сетки, ниже которого это не таблица, а рамка или выноска.
_MIN_ROWS = 2
_MIN_COLS = 2

# Доля ячеек, которые должны быть заполнены. Пустая сетка — это разграфка
# штампа или рамка листа, а не таблица с данными.
_MIN_FILL = 0.25

# Слова основной надписи (штампа по ГОСТ 21.101). Её разграфка выглядит как
# таблица, но данных в ней нет — они уходят в паспорт листа.
_STAMP_WORDS = ("изм.", "кол.уч", "подпись и дата", "инв. № подл", "взам", "н.контр")


def _segments(entity) -> list[tuple[float, float, float, float]]:
    """Отрезки сущности: сама линия или звенья полилинии."""
    kind = entity.dxftype()
    try:
        if kind == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            return [(start.x, start.y, end.x, end.y)]
        if kind == "LWPOLYLINE":
            points = [(p[0], p[1]) for p in entity.get_points("xy")]
            if getattr(entity, "closed", False) and len(points) > 2:
                points.append(points[0])
            return [
                (points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
                for i in range(len(points) - 1)
            ]
        if kind == "POLYLINE":
            points = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            return [
                (points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
                for i in range(len(points) - 1)
            ]
    except Exception:
        return []
    return []


def _collect_lines(space, box, depth: int = 0):
    """Горизонтальные и вертикальные отрезки в границах листа.

    Блоки разворачиваем: рамка таблицы часто вставлена блоком целиком, и без
    разворота сетки не видно вовсе.
    """
    horizontals: list[tuple[float, float, float]] = []  # (x0, x1, y)
    verticals: list[tuple[float, float, float]] = []  # (y0, y1, x)
    if depth > 3:
        return horizontals, verticals
    x0b, y0b, x1b, y1b = box
    for entity in space:
        try:
            if entity.dxftype() == "INSERT":
                sub_h, sub_v = _collect_lines(entity.virtual_entities(), box, depth + 1)
                horizontals += sub_h
                verticals += sub_v
                continue
            for sx, sy, ex, ey in _segments(entity):
                if not (x0b <= (sx + ex) / 2 <= x1b and y0b <= (sy + ey) / 2 <= y1b):
                    continue
                if abs(sy - ey) <= abs(sx - ex) * 0.02 and abs(sx - ex) > 0:
                    horizontals.append((min(sx, ex), max(sx, ex), (sy + ey) / 2))
                elif abs(sx - ex) <= abs(sy - ey) * 0.02 and abs(sy - ey) > 0:
                    verticals.append((min(sy, ey), max(sy, ey), (sx + ex) / 2))
        except Exception:
            continue
    return horizontals, verticals


def _cluster(values: list[float], tolerance: float) -> list[float]:
    """Схлопывает близкие координаты в одну линию сетки."""
    if not values:
        return []
    values = sorted(values)
    groups = [[values[0]]]
    for value in values[1:]:
        if value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(g) / len(g) for g in groups]


def _merge_lines(lines, tolerance):
    """Линии, лежащие на одной прямой, в одну запись с общим диапазоном.

    Строку таблицы часто рисуют не одним отрезком, а звеньями по числу ячеек.
    Без склейки такая строка выглядит как десяток коротких линий, и сетка не
    находится вовсе.
    """
    merged: dict[float, list[float]] = {}
    for a0, a1, coordinate in lines:
        key = None
        for existing in merged:
            if abs(existing - coordinate) <= tolerance:
                key = existing
                break
        if key is None:
            merged[coordinate] = [a0, a1]
        else:
            merged[key][0] = min(merged[key][0], a0)
            merged[key][1] = max(merged[key][1], a1)
    return sorted((c, span[0], span[1]) for c, span in merged.items())


def _grid_candidates(horizontals, verticals, tolerance):
    """Прямоугольные сетки: подряд идущие строки с общими вертикалями.

    Идём сверху вниз по горизонталям и накапливаем строки, пока их отрезки
    перекрываются по X и между ними есть хотя бы две вертикали. Так таблица
    отделяется от рамки листа и от случайных параллельных линий чертежа.
    """
    rows_all = _merge_lines(horizontals, tolerance)
    columns_all = _merge_lines(verticals, tolerance)
    if len(rows_all) < _MIN_ROWS + 1 or len(columns_all) < _MIN_COLS + 1:
        return []

    candidates = []
    used: set[int] = set()
    order = sorted(range(len(rows_all)), key=lambda i: -rows_all[i][0])
    for start_index in range(len(order)):
        if order[start_index] in used:
            continue
        group = [order[start_index]]
        left = rows_all[order[start_index]][1]
        right = rows_all[order[start_index]][2]
        for next_index in order[start_index + 1:]:
            y, x0, x1 = rows_all[next_index]
            overlap = min(right, x1) - max(left, x0)
            if overlap <= (right - left) * 0.6:
                break
            gap = rows_all[group[-1]][0] - y
            if gap > (right - left):  # строки таблицы не бывают выше её ширины
                break
            group.append(next_index)
            left, right = max(left, x0), min(right, x1)
        if len(group) < _MIN_ROWS + 1:
            continue
        top = rows_all[group[0]][0]
        bottom = rows_all[group[-1]][0]
        height = top - bottom
        columns = [
            x
            for x, y0, y1 in columns_all
            if left - tolerance <= x <= right + tolerance
            and (min(y1, top) - max(y0, bottom)) > height * 0.6
        ]
        if len(columns) < _MIN_COLS + 1:
            continue
        used.update(group)
        candidates.append(
            {
                "rows": [rows_all[i][0] for i in group],
                "columns": sorted(columns),
                "box": (min(columns), bottom, max(columns), top),
            }
        )
    return candidates


def _cell_text(texts, x0, y0, x1, y1) -> str:
    """Текст, попавший в ячейку, строками сверху вниз."""
    inside = [t for t in texts if x0 <= t.x <= x1 and y0 <= t.y <= y1]
    if not inside:
        return ""
    inside.sort(key=lambda t: (-t.y, t.x))
    return " ".join(t.text.replace("\n", " ").strip() for t in inside).strip()


def _to_gfm(rows: list[list[str]]) -> str:
    """Сетка в GFM. Первая строка — заголовок, как и в исходной таблице."""
    width = max(len(r) for r in rows)
    normalized = [r + [""] * (width - len(r)) for r in rows]
    head = normalized[0]
    body = normalized[1:]
    lines = ["| " + " | ".join(c or " " for c in head) + " |",
             "|" + "---|" * width]
    for row in body:
        lines.append("| " + " | ".join(c or "" for c in row) + " |")
    return "\n".join(lines)


def sheet_tables(space, sheet, texts) -> list[tuple[tuple[float, float, float, float], str]]:
    """Таблицы листа: [(границы, GFM), ...], сверху вниз.

    `texts` — подписи листа (TextItem из dwg_sheets): по ним заполняются
    ячейки, и по ним же определяется, что сетка не пустая.
    """
    box = sheet.extent()
    if box is None or not texts:
        return []
    x0b, y0b, x1b, y1b = box
    size = max(x1b - x0b, y1b - y0b)
    tolerance = max(size * _SNAP, 1e-6)

    horizontals, verticals = _collect_lines(space, box)
    if not horizontals or not verticals:
        return []

    found: list[tuple[tuple[float, float, float, float], str]] = []
    for candidate in _grid_candidates(horizontals, verticals, tolerance):
        rows = sorted(candidate["rows"], reverse=True)  # сверху вниз
        columns = sorted(candidate["columns"])
        grid: list[list[str]] = []
        filled = 0
        for i in range(len(rows) - 1):
            row: list[str] = []
            for j in range(len(columns) - 1):
                cell = _cell_text(
                    texts, columns[j], rows[i + 1], columns[j + 1], rows[i]
                )
                if cell:
                    filled += 1
                row.append(cell)
            grid.append(row)
        cells = max(len(grid) * (len(columns) - 1), 1)
        if filled / cells < _MIN_FILL or filled < 6:
            continue  # разграфка без данных — это рамка, а не таблица
        # У штампа почти каждая строка — одна подпись в одной ячейке, у
        # настоящей таблицы строки многоколоночные. Признак тот же, что в
        # pdf_tables: он не привязан к конкретному чертежу.
        multi = sum(1 for row in grid if sum(1 for c in row if c.strip()) >= 2)
        if multi / max(len(grid), 1) < 0.5:
            continue
        if any(word in " ".join(row).lower() for row in grid for word in _STAMP_WORDS):
            continue  # основная надпись листа, а не таблица с данными
        # В таблице есть подписи, а не одни числа. Без этого в «таблицы»
        # попадает разметка узла: сетка арматуры с размерами 300, 600, 180
        # выглядит заполненной сеткой, но таблицей не является.
        worded = sum(
            1
            for row in grid
            for cell in row
            if sum(1 for ch in cell if ch.isalpha()) >= 3
        )
        if worded < max(3, len(grid) // 2):
            continue
        found.append((candidate["box"], _to_gfm(grid)))

    # Вложенные сетки (таблица внутри рамки) дают дубли: оставляем внешнюю.
    found.sort(key=lambda item: (item[0][2] - item[0][0]) * (item[0][3] - item[0][1]), reverse=True)
    kept: list[tuple[tuple[float, float, float, float], str]] = []
    for box_i, md in found:
        if any(_inside(box_i, box_j) for box_j, _ in kept):
            continue
        kept.append((box_i, md))
    kept.sort(key=lambda item: -item[0][3])  # сверху вниз
    return kept


def _inside(inner, outer) -> bool:
    return (
        inner[0] >= outer[0] - 1e-6
        and inner[1] >= outer[1] - 1e-6
        and inner[2] <= outer[2] + 1e-6
        and inner[3] <= outer[3] + 1e-6
    )
