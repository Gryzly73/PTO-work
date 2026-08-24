"""Детерминированная сборка таблиц из ТЕКСТОВОГО СЛОЯ PDF (без VLM).

Идея: у векторных CAD/офисных PDF таблица уже описана линиями и словами с
координатами. MuPDF умеет находить сетку (`page.find_tables()`), а слова с
bbox дают точные значения ячеек. Значит таблицу можно собрать точно, бесплатно
и без единого шанса на галлюцинацию — VLM остаётся описание и те листы, где
слой битый или его нет (сканы).

Универсальность: ни одного правила, привязанного к конкретному документу.
Работает на любом PDF с читаемым текстовым слоем; на сканах молча возвращает
пустой список, и конвейер откатывается на VLM.

Ключевая деталь — «расслоение» строк: в инженерных таблицах одна логическая
строка часто содержит два яруса значений (сут/час, норма/факт). MuPDF склеивает
их в одну ячейку («28 28»). Если во ВСЕХ непустых ячейках строки одинаковое
число ярусов k>1, строка разворачивается в k строк; иначе ярусы сохраняются
через <br>, чтобы ничего не потерять.
"""

from __future__ import annotations

import re

import fitz

# ── настройки качества (общие, не под конкретный документ) ────────────────
MIN_ROWS = 2
MIN_COLS = 2
MIN_FILL = 0.05  # доля непустых ячеек — грубый отсев пустых сеток
# Меньше стольких непустых ячеек — это не таблица, а рамочка на чертеже:
# выноска, штучное обозначение, ярлык у оборудования. На листе 41 таких
# сеток 2x2 с двумя значениями было десять штук, и каждая шла как таблица.
MIN_FILLED_CELLS = 6
# Ячейка, в которой лежит такая доля текста листа, — не ячейка, а рамка.
SWALLOW_SHARE = 0.35
# На листах, где текста почти нет, доля скачет от одного слова — не судим.
SWALLOW_MIN_CHARS = 200
MIN_MULTI_ROWS = 0.15  # доля строк, где заполнено ≥2 ячеек
LINE_TOL = 2.0  # px: слова с близким центром по Y — один ярус
FRAME_SHARE = 0.3  # доля листов, на которых повторяется сетка рамки
FRAME_COVER = 0.85  # рамка занимает почти весь лист, таблица — нет
GAP_SPACE = 1.2  # px: зазор между словами, начиная с которого это пробел


def _cell_lines(words: list, rect: fitz.Rect) -> list[str]:
    """Текст ячейки, разбитый на ярусы (строки) по координатам слов.

    Слово относится к ячейке по своему ЦЕНТРУ: пересечение рамок приписывало
    пограничные слова сразу двум соседним ячейкам и строки «протекали»."""
    inside = [
        w
        for w in words
        if rect.x0 <= (w[0] + w[2]) / 2 <= rect.x1
        and rect.y0 <= (w[1] + w[3]) / 2 <= rect.y1
    ]
    if not inside:
        return []
    rows: list[tuple[float, list[tuple[float, float, str]]]] = []
    for x0, y0, x1, y1, txt, *_ in inside:
        yc = (y0 + y1) / 2
        for ry, bucket in rows:
            if abs(ry - yc) <= LINE_TOL:
                bucket.append((x0, x1, txt))
                break
        else:
            rows.append((yc, [(x0, x1, txt)]))
    rows.sort(key=lambda r: r[0])
    out: list[str] = []
    for _, bucket in rows:
        bucket.sort(key=lambda t: t[0])
        # Пробел ставим только там, где он есть на листе. MuPDF отдаёт «28»,
        # «-», «ХСА» отдельными словами, и склейка через пробел превращала
        # шифр 28-ХСА-1/25-ПЗ в «28 ХСА 1 25 ПЗ». Ориентируемся на реальный
        # зазор между словами, а не на факт их раздельной выдачи.
        parts: list[str] = []
        prev_x1: float | None = None
        for x0, x1, txt in bucket:
            if prev_x1 is not None:
                parts.append(" " if (x0 - prev_x1) > GAP_SPACE else "")
            parts.append(txt)
            prev_x1 = x1
        line = "".join(parts).strip()
        if line:
            out.append(line)
    return out


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ").strip()


def _destack(grid: list[list[list[str]]]) -> list[list[str]]:
    """grid[row][col] = список ярусов → плоские строки таблицы."""
    out: list[list[str]] = []
    for row in grid:
        counts = {len(c) for c in row if c}
        if len(counts) == 1 and (k := counts.pop()) > 1:
            # все непустые ячейки имеют k ярусов → разворачиваем в k строк
            for i in range(k):
                out.append([(c[i] if c else "") for c in row])
        else:
            out.append([" <br> ".join(c) if c else "" for c in row])
    return out


def _drop_empty_columns(rows: list[list[str]]) -> list[list[str]]:
    """Убирает колонки, пустые во ВСЕХ строках.

    Линии рамки и разделители внутри листа режут сетку на десятки колонок,
    из которых данные несут единицы. Пустая по всей высоте колонка не несёт
    ничего, а таблицу делает нечитаемой."""
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(r[i].strip() for r in rows)]
    if not keep or len(keep) == width:
        return rows
    return [[r[i] for i in keep] for r in rows]


def _to_gfm(rows: list[list[str]]) -> str:
    rows = _drop_empty_columns(rows)
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head = rows[0]
    # шапка без единого непустого значения читается плохо → нумеруем колонки
    if not any(h.strip() for h in head):
        head = [f"кол.{i+1}" for i in range(width)]
        body = rows[1:]
    else:
        body = rows[1:]
    lines = [
        "| " + " | ".join(_md_escape(h) or " " for h in head) + " |",
        "|" + "---|" * width,
    ]
    for r in body:
        lines.append("| " + " | ".join(_md_escape(c) or "-" for c in r) + " |")
    return "\n".join(lines)


def _table_sig(page: fitz.Page, tab) -> tuple:
    """Отпечаток сетки: положение и размер на листе с грубым округлением.

    Числа строк и колонок в отпечатке НЕТ намеренно. Рамка листа — одна и та
    же по геометрии, но MuPDF режет её на разное число ячеек: линии чертежа
    подходят к рамке вплотную и достраивают ей столбцы. На ИОС2 одна и та же
    рамка выходила как 8x10 на текстовых листах, 18x19 на листе 34, 18x18 на
    41, 13x11 на 35 — и с номерами в отпечатке совпадала сама с собой только
    на A4. Из-за этого на планах рамка проходила как содержательная таблица и
    забирала в одну ячейку весь текст листа.
    """
    r = fitz.Rect(tab.bbox)
    pr = page.rect
    return (
        round(r.x0 / max(pr.width, 1), 1),
        round(r.y0 / max(pr.height, 1), 1),
        round(r.width / max(pr.width, 1), 1),
        round(r.height / max(pr.height, 1), 1),
    )


def _swallowed_page(page: fitz.Page, raw: list) -> bool:
    """В одной ячейке лежит весь лист — значит это рамка, а не таблица.

    Геометрия для такого отсева не годится: MuPDF на A4 склеивает рамку и
    настоящую таблицу в ОДНУ сетку во весь лист, и правило «во весь лист —
    рамка» выбрасывало содержимое вместе с ней (лист 44, таблица на 76 строк).

    Работает признак содержания. У рамки вокруг чертежа или прозы внутренних
    линий нет, поэтому весь свободный текст листа ссыпается в одну ячейку. На
    ИОС2 разделение полное: у настоящих таблиц самая большая ячейка держит
    1–20% текста листа, у рамок — 51–97%. Порог посередине, между 20% и 51%.
    """
    total = len("".join(page.get_text("text").split()))
    if total < SWALLOW_MIN_CHARS:
        return False
    biggest = max(
        (len("".join((c or "").split())) for row in raw for c in row), default=0
    )
    return biggest >= SWALLOW_SHARE * total


def frame_signatures(doc: fitz.Document) -> set[tuple]:
    """Сетки, повторяющиеся почти на всех листах, — это рамка со штампом.

    Признак универсален для проектной документации: основная надпись по ГОСТ
    присутствует на каждом листе комплекта, а содержательная таблица — нет.
    Никаких порогов «на глазок» и никакой привязки к конкретному документу.
    """
    counts: dict[tuple, int] = {}
    pages = 0
    for i in range(doc.page_count):
        page = doc[i]
        try:
            tabs = list(page.find_tables().tables)
        except Exception:
            continue
        pages += 1
        for sig in {_table_sig(page, t) for t in tabs}:
            counts[sig] = counts.get(sig, 0) + 1
    if pages < 3:
        return set()
    # Рамка отличается двумя свойствами сразу: повторяется на многих листах И
    # занимает лист целиком. Одной повторяемости мало — типовая расчётная
    # таблица тоже может идти подряд на нескольких листах, но она не во весь
    # лист; одной геометрии тоже мало — крупная таблица бывает на всю страницу.
    return {
        s
        for s, c in counts.items()
        if c >= FRAME_SHARE * pages and s[2] >= FRAME_COVER and s[3] >= FRAME_COVER
    }


def page_tables_md(
    page: fitz.Page,
    glyph_map: dict[str, str] | None = None,
    frames: set[tuple] | None = None,
) -> list[str]:
    """Все пригодные таблицы страницы как GFM. [] — если слой не даёт таблиц.

    glyph_map — отображение из `deglyph` для листов со сломанным ToUnicode:
    сетка у таких страниц определяется нормально, чинить нужно только текст.
    """
    try:
        finder = page.find_tables()
    except Exception:
        return []
    words = page.get_text("words")  # один раз на страницу, не на ячейку
    if glyph_map:
        words = [
            (w[0], w[1], w[2], w[3], "".join(glyph_map.get(c, c) for c in w[4]), *w[5:])
            for w in words
        ]
    out: list[str] = []
    for tab in finder.tables:
        if frames and _table_sig(page, tab) in frames:
            continue  # рамка листа со штампом, а не содержательная таблица
        try:
            raw = tab.extract()
        except Exception:
            continue
        if not raw or len(raw) < MIN_ROWS or len(raw[0]) < MIN_COLS:
            continue
        if _swallowed_page(page, raw):
            continue  # рамка листа, в которую ссыпался весь свободный текст
        if glyph_map:  # оценки заполненности считаем уже по починенному тексту
            raw = [
                ["".join(glyph_map.get(ch, ch) for ch in (c or "")) for c in r]
                for r in raw
            ]
        filled = sum(1 for r in raw for c in r if (c or "").strip())
        if filled < MIN_FILLED_CELLS:
            continue
        if filled / max(1, sum(len(r) for r in raw)) < MIN_FILL:
            continue
        # Штамп/рамка листа тоже распознаётся как «сетка», но у неё почти
        # каждая строка — одна подпись в одной ячейке. У настоящей таблицы
        # строки многоколоночные. Признак универсальный, без привязки к
        # конкретному документу.
        multi = sum(1 for r in raw if sum(1 for c in r if (c or "").strip()) >= 2)
        if multi / len(raw) < MIN_MULTI_ROWS:
            continue
        # пересобираем ячейки по координатам, чтобы знать ярусы
        grid: list[list[list[str]]] = []
        try:
            for row in getattr(tab, "rows", []):
                grid.append(
                    [
                        _cell_lines(words, fitz.Rect(c)) if c else []
                        for c in getattr(row, "cells", [])
                    ]
                )
        except Exception:
            grid = []
        if not grid or len(grid) != len(raw):
            # запасной путь: значения из extract() без расслоения ярусов
            grid = [[[(c or "").strip()] if (c or "").strip() else [] for c in r] for r in raw]
        out.append(_to_gfm(_destack(grid)))
    return out


def page_tables_stats(page: fitz.Page) -> dict:
    """Диагностика: сколько таблиц и ячеек даёт слой (для отчётов)."""
    mds = page_tables_md(page)
    cells = sum(
        len([c for c in ln.split("|") if c.strip()])
        for md in mds
        for ln in md.splitlines()
        if not re.fullmatch(r"[|\s:-]+", ln)
    )
    return {"tables": len(mds), "cells": cells}


if __name__ == "__main__":  # быстрый ручной прогон
    import argparse

    ap = argparse.ArgumentParser(description="Таблицы из текстового слоя PDF")
    ap.add_argument("pdf")
    ap.add_argument("--pages", default="1")
    args = ap.parse_args()
    doc = fitz.open(args.pdf)
    for spec in args.pages.split(","):
        if "-" in spec:
            a, b = spec.split("-", 1)
            rng = range(int(a), int(b) + 1)
        else:
            rng = [int(spec)]
        for p in rng:
            mds = page_tables_md(doc[p - 1])
            print(f"\n===== стр. {p}: таблиц {len(mds)} =====")
            for md in mds:
                print(md)
    doc.close()
