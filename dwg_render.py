"""Отрисовка листа чертежа: SVG для показа и PNG для миниатюр.

Зачем это здесь. У PDF интерфейс рисует страницу сам через pdf.js, у чертежа
рисовать нечего: DWG браузер не открывает. Пока картинки нет, инженер видит
пустое место рядом с расшифровкой и не может сверить одно с другим.

Картинка собирается из тех же примитивов, что уходят интерфейсу в CSV
(`dwg_geometry.sheet_primitives`), а не по чертежу заново. Так она совпадает с
геометрией точка в точку: на неё ложатся те же замечания, и в кадре тот же
лист. Пока картинка рисовалась своим путём, по координатам модели, у листа ПЗУ
чертёж занимал 0,2% кадра — охват считался по подписям бумаги вместе с окнами
вида, то есть по двум системам координат сразу.

Ниже — разбор чертежа, общий для картинки и для CSV. Отрисовка ezdxf на
выходе LibreDWG требует трёх поправок, без которых получается чёрный лист или
точка посреди пустоты:

  * цвета чертежа рассчитаны на чёрный фон модели — меняем чёрный и белый
    местами, иначе белые линии сливаются с белым листом;
  * WIPEOUT (маскировка) и IMAGE (внешняя подложка) заливают лист сплошным
    прямоугольником — пропускаем;
  * у DIMENSION после конвертации точки битые, и рисовальщик уводит вид на
    10¹² единиц: лист сжимается в точку. Размеры пропускаем — сами размерные
    числа не теряются, они есть в тексте листа.

Границы вида задаём сами, по подписям и окнам вьюпортов листа: автомасштаб
опирается на габариты нарисованного и на одном испорченном объекте
разъезжается.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ezdxf import bbox
from ezdxf.addons.drawing import config

from dwg_sheets import text_span

# Маскировки и подложки — см. шапку модуля. Размеры (DIMENSION) здесь больше
# не значатся: они несут то, что инженер и сверяет. Битые экземпляры, из-за
# которых их когда-то выключили целиком, отсекаются по габаритам — так же, как
# любой другой объект не с этого листа.
SKIP_TYPES = ("WIPEOUT", "IMAGE")

# Потолок на число объектов листа. Стройгенплан с 12 тысячами линий рисуется
# около минуты, а разборчивее от этого не становится.
MAX_ENTITIES = 12000

# Запас вокруг содержимого, чтобы рамка не упиралась в край картинки.
_PAD = 0.06


def sheet_box(sheet) -> tuple[float, float, float, float] | None:
    """Границы листа в координатах модели: подписи плюс окна вьюпортов.

    По одним подписям лист с двумя надписями сжимается в точку и рамки не
    видно; по одним окнам чертёж тонет в пустоте, когда вьюпорт настроен шире
    начерченного. Вместе они дают то, что инженер и ожидает увидеть.
    """
    xs = [t.x for t in sheet.texts]
    ys = [t.y for t in sheet.texts]
    for window in sheet.windows:
        xs += [window[0], window[2]]
        ys += [window[1], window[3]]
    if not xs:
        return None
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    dx, dy = (x1 - x0) or 1.0, (y1 - y0) or 1.0
    return (x0 - dx * _PAD, y0 - dy * _PAD, x1 + dx * _PAD, y1 + dy * _PAD)


_BOX_CACHE: dict[int, tuple[float, float, float, float] | None] = {}


def _entity_box(entity):
    """Габариты объекта, посчитанные один раз на объект.

    В файле с тринадцатью листами один и тот же объект иначе меряется
    тринадцать раз, и сборка растягивается на часы.
    """
    key = id(entity)
    if key in _BOX_CACHE:
        return _BOX_CACHE[key]
    try:
        extents = bbox.extents([entity], fast=True)
        value = (
            (extents.extmin.x, extents.extmin.y, extents.extmax.x, extents.extmax.y)
            if extents.has_data
            else None
        )
    except Exception:
        value = None
    _BOX_CACHE[key] = value
    return value


def _on_sheet(entity, box) -> bool:
    """Объект относится к этому листу и соразмерен ему."""
    if box is None:
        return True
    extents = _entity_box(entity)
    if extents is None:
        return False
    x0, y0, x1, y1 = box
    ex0, ey0, ex1, ey1 = extents
    if ex1 < x0 or ex0 > x1 or ey1 < y0 or ey0 > y1:
        return False
    pad_x, pad_y = (x1 - x0) * 0.3, (y1 - y0) * 0.3
    return (
        ex0 > x0 - pad_x
        and ex1 < x1 + pad_x
        and ey0 > y0 - pad_y
        and ey1 < y1 + pad_y
    )


def _entities(doc, box):
    """Объекты листа, годные к отрисовке, не больше MAX_ENTITIES.

    Слои, отключённые или замороженные в самом чертеже, пропускаем: на печать
    они не идут, а у нас рисовались поверх — в комплекте «Жуковский» таких
    слоёв 45 из 450, включая варианты границ участка «ВАР1/ВАР2».
    """
    from dwg_sheets import hidden_layers

    hidden = hidden_layers(doc)
    picked = []
    for entity in doc.modelspace():
        if entity.dxftype() in SKIP_TYPES or not _on_sheet(entity, box):
            continue
        try:
            if hidden and entity.dxf.layer in hidden:
                continue
        except Exception:
            pass
        picked.append(entity)
        if len(picked) >= MAX_ENTITIES:
            break
    return picked


def _configuration() -> config.Configuration:
    return config.Configuration(
        color_policy=config.ColorPolicy.COLOR_SWAP_BW,
        background_policy=config.BackgroundPolicy.OFF,
        # Штриховку рисуем контуром, без заливки: разрез перестаёт выглядеть
        # пустым, а лист не заливается чёрным и не тяжелеет вдвое, как было бы
        # с настоящим узором.
        hatch_policy=config.HatchPolicy.SHOW_OUTLINE,
        lineweight_scaling=0.5,
    )


# ── картинка листа ──────────────────────────────────────────────────────────
#
# Рисуем не по чертежу напрямую, а по тем же примитивам, что уходят в CSV
# (`dwg_geometry.sheet_primitives`). Раньше картинка строилась своим путём, по
# координатам модели, и расходилась с геометрией: у листа ПЗУ рамка со штампом
# лежит на бумаге (0…594 мм), а сам чертёж — в координатах площадки (около
# 2 226 000), и охват, посчитанный по обоим сразу, выходил шириной в два с
# половиной километра. Чертёж занимал в нём 0,2% площади, то есть картинка была
# пустой.
#
# Теперь источник один, и картинка совпадает с геометрией точка в точку: на неё
# ложатся те же замечания, и на ней есть рамка со штампом.

# Экранных пикселей на единицу чертежа. При 1400 px по ширине лист А2
# читается, а файл остаётся лёгким.
_PREVIEW_WIDTH_PX = 1400
_MIN_STROKE_MM = 0.05

_ANCHOR_TO_SVG = {"left": "start", "center": "middle", "right": "end"}


# ── сколько места занимает подпись ──────────────────────────────────────────
#
# Мерка ширины живёт в разборе (`dwg_sheets.text_span`), а не здесь: место,
# занятое подписью на листе, — свойство чертежа, а не картинки. По ней тут
# вписывается строка в её место, там же — переносятся строки по ширине блока
# и разделяются подписи, стоящие в одной строке далеко друг от друга.

# Межстрочный интервал MTEXT: так строки расставляет AutoCAD (тот же
# коэффициент в dwg_sheets считает отступ пустых строк).
_LINE_SPACING = 1.5

# Какую долю кегля занимает прописная буква в шрифте картинки (DejaVu Sans).
# Высота текста в чертеже — это высота прописной, а не кегль, поэтому без
# поправки подпись рисуется на четверть мельче, чем стоит в файле.
_CAP_RATIO = 0.73

# Насколько «у» и «р» свисают ниже строки — долями высоты прописной.
_DESCENT = 0.25


def _first_baseline(lines: int, size: float, valign: str) -> float:
    """На сколько первая строка ниже точки привязки, в единицах чертежа.

    Считаем сами, а не отдаём выравнивание рисовальщику: у SVG и matplotlib
    оно устроено по-разному (`dominant-baseline` против `va`), и один и тот
    же лист выходил в двух картинках со сдвигом на полстроки.
    """
    step = size * _LINE_SPACING
    if valign == "top":
        return size
    if valign == "middle":
        return size - ((lines - 1) * step + size) / 2
    if valign == "bottom":
        # Низ подписи — это низ выносных элементов «у» и «р», а не строка
        # букв: иначе подпись в графе штампа ложится прямо на её линейку.
        return -(lines - 1) * step - size * _DESCENT
    return 0.0


def _sheet_frame(meta: dict) -> tuple[float, float, float, float, float, float]:
    """Габариты листа из его метаданных: (x0, y0, x1, y1, ширина, высота)."""
    x0, y0, x1, y1 = meta.get("bbox") or (0.0, 0.0, 1.0, 1.0)
    return x0, y0, x1, y1, max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)


# ── лист без текста ─────────────────────────────────────────────────────────
#
# Пустой картинки быть не должно. Если на листе нет подписей, у этого ровно две
# причины, и обе надо назвать прямо на превью: либо лист графический — чертёж
# или схема, где текста и не было, — либо лист не пережил конвертацию. Инженер,
# листающий документ, должен видеть разницу, не открывая исходник.

BANNER_DRAWING = (
    "ЧЕРТЁЖ · ТЕКСТА НЕТ",
    "на листе только графика — подписей в файле нет",
)
BANNER_LOST = (
    "ЛИСТ НЕ ПРОЧИТАН",
    "потеря при конвертации DWG → DXF — смотрите исходный чертёж",
)
BANNER_BLANK = (
    "ЛИСТ ПУСТ В ЧЕРТЕЖЕ",
    "заготовка: содержимое вычерчено в модели и отдано отдельным листом",
)

# Лист-заглушка, когда рисовать нечего вовсе: А4 в миллиметрах.
_BLANK_SHEET = (0.0, 0.0, 210.0, 297.0)


def banner_for(primitives: list[dict], meta: dict | None = None) -> tuple[str, str] | None:
    """Надпись для превью листа без текста. None — если подписи есть.

    Потерянным лист считается по метке из разбора, а не по пустоте примитивов:
    у листа опорных точек ПОС рисовать нечего лишь потому, что окно вида
    нечитаемое, — но сам лист на месте.
    """
    if any(item.get("type") == "text" and item.get("text") for item in primitives):
        return None
    meta = meta or {}
    if meta.get("lost"):
        return BANNER_LOST
    if meta.get("blank"):
        return BANNER_BLANK
    return BANNER_DRAWING


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _text_lines(value: str) -> list[str]:
    """Подпись строками: в CSV переносы записаны литералом из двух символов."""
    return value.split(chr(92) + "n")


def _banner_svg(width: float, height: float, banner: tuple[str, str]) -> str:
    """Плашка поверх листа: крупный заголовок и строка пояснения под ним."""
    title, note = banner
    # Кегль от ширины листа, а не абсолютный: одна и та же плашка должна
    # читаться и на А4, и на стройгенплане в три метра.
    size = max(width * 0.035, 4.0)
    box_h = size * 3.4
    box_y = height * 0.5 - box_h / 2
    return (
        f'<g><rect x="{width * 0.06:.2f}" y="{box_y:.2f}" '
        f'width="{width * 0.88:.2f}" height="{box_h:.2f}" '
        'fill="#ffffff" fill-opacity="0.88" stroke="#b42318" '
        f'stroke-width="{max(size * 0.06, 0.3):.2f}"/>'
        f'<text x="{width / 2:.2f}" y="{box_y + box_h * 0.42:.2f}" '
        f'font-size="{size:.2f}" font-family="sans-serif" font-weight="bold" '
        'fill="#b42318" text-anchor="middle" dominant-baseline="central">'
        f"{_escape(title)}</text>"
        f'<text x="{width / 2:.2f}" y="{box_y + box_h * 0.78:.2f}" '
        f'font-size="{size * 0.42:.2f}" font-family="sans-serif" '
        'fill="#667085" text-anchor="middle" dominant-baseline="central">'
        f"{_escape(note)}</text></g>"
    )


def render_svg(primitives: list[dict], meta: dict) -> str:
    """Лист как SVG. Лист без подписей отдаётся с плашкой, а не пустым.

    Координаты сразу переводятся в экранные (начало в левом верхнем углу, Y
    вниз), поэтому в самом SVG нет ни одного преобразования: так его одинаково
    показывают браузер, конвертер и просмотрщик, и подписи не оказываются
    зеркальными — обычная плата за трюк со `scale(1,-1)`.
    """
    banner = banner_for(primitives, meta)
    if not primitives:
        # Рисовать нечего, но отдать пустоту нельзя: пустое место в интерфейсе
        # читается как «лист пустой», а он не пустой — он до нас не дошёл.
        x0, y0, x1, y1 = _BLANK_SHEET
        width, height = x1 - x0, y1 - y0
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.2f} {height:.2f}" '
            f'width="{width:.1f}mm" height="{height:.1f}mm">'
            '<rect width="100%" height="100%" fill="#ffffff"/>'
            + _banner_svg(width, height, banner or BANNER_DRAWING)
            + "</svg>"
        )
    x0, y0, x1, y1, width, height = _sheet_frame(meta)

    def to_screen(x: float, y: float) -> tuple[float, float]:
        return x - x0, y1 - y

    strokes: dict[tuple[str, float], list[str]] = {}
    texts: list[str] = []
    for item in primitives:
        points = item.get("points") or []
        if item.get("type") == "text":
            if not points or not item.get("text"):
                continue
            sx, sy = to_screen(*points[0])
            size = float(item.get("size") or 2.5)
            # Поворот в чертеже считается против часовой стрелки, на экране —
            # по часовой: без смены знака вертикальные подписи ложатся зеркально.
            rotation = -float(item.get("rot") or 0)
            anchor = _ANCHOR_TO_SVG.get(str(item.get("anchor") or "left"), "start")
            valign = str(item.get("valign") or "baseline")
            factor = float(item.get("wf") or 1.0)
            box = float(item.get("width") or 0.0)
            lines = _text_lines(str(item["text"]))
            first = _first_baseline(len(lines), size, valign)
            step = size * _LINE_SPACING
            spans = []
            for n, line in enumerate(lines):
                if not line.strip():
                    continue
                # Строку укладываем ровно в её место на листе: браузер сожмёт
                # или растянет знаки под заданную ширину сам. Без этого лист
                # рисуется шрифтом браузера, который шире чертёжного.
                span = text_span(line, size, factor)
                if box > size:
                    span = min(span, box)
                spans.append(
                    f'<tspan x="{sx:.2f}" y="{sy + first + n * step:.2f}" '
                    f'textLength="{span:.2f}" lengthAdjust="spacingAndGlyphs">'
                    f"{_escape(line)}</tspan>"
                )
            if not spans:
                continue
            transform = (
                f' transform="rotate({rotation:.1f} {sx:.2f} {sy:.2f})"'
                if rotation
                else ""
            )
            texts.append(
                # Кегль задаём от высоты прописной: в чертеже размер текста —
                # это её высота, а не кегль шрифта.
                f'<text font-size="{size / _CAP_RATIO:.2f}" '
                f'fill="{item.get("color") or "#000000"}" text-anchor="{anchor}" '
                f'dominant-baseline="auto"{transform}>{"".join(spans)}</text>'
            )
            continue
        if len(points) < 2:
            continue
        key = (str(item.get("color") or "#000000"), float(item.get("lw") or 0.25))
        path = []
        for index, (px, py) in enumerate(points):
            sx, sy = to_screen(px, py)
            path.append(f"{'M' if index == 0 else 'L'}{sx:.1f} {sy:.1f}")
        strokes.setdefault(key, []).append("".join(path))

    if not strokes and not texts:
        # Примитивы были, но ни одного рисуемого: те же две причины, что и у
        # пустого листа, — отвечаем плашкой, а не пустотой.
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.2f} {height:.2f}" '
            f'width="{width:.1f}mm" height="{height:.1f}mm">'
            '<rect width="100%" height="100%" fill="#ffffff"/>'
            + _banner_svg(width, height, banner or BANNER_DRAWING)
            + "</svg>"
        )

    # Линии одного цвета и толщины идут одним path: у стройгенплана это
    # десятки тысяч отрезков, и по отдельному элементу на каждый браузер
    # заметно тормозит.
    body = [
        f'<path d="{" ".join(paths)}" fill="none" stroke="{color}" '
        f'stroke-width="{max(lw, _MIN_STROKE_MM):.2f}" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        for (color, lw), paths in strokes.items()
    ]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.2f} {height:.2f}" '
        f'width="{width:.1f}mm" height="{height:.1f}mm">'
        # Белая подложка: без неё лист прозрачен, и в тёмной теме браузера
        # чёрные линии чертежа оказываются на тёмном фоне.
        '<rect width="100%" height="100%" fill="#ffffff"/>'
        + "".join(body)
        + "".join(texts)
        + (_banner_svg(width, height, banner) if banner else "")
        + "</svg>"
    )


_TEXT_FONT = None


@lru_cache(maxsize=4096)
def _line_path(line: str):
    """Контуры букв строки, кеглем в единицу. Размер задаётся преобразованием.

    Кеш держим по самой строке: на листе фундаментов из шести сотен подписей
    добрая половина — повторяющиеся отметки и номера свай, а разбор строки в
    контуры стоит около шести миллисекунд.
    """
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath

    global _TEXT_FONT
    if _TEXT_FONT is None:
        _TEXT_FONT = FontProperties()
    path = TextPath((0, 0), line, size=1.0, prop=_TEXT_FONT)
    return path, path.get_extents()


def _text_glyphs(item: dict):
    """Подпись листа глифами, вписанными в её место на чертеже.

    matplotlib умеет ставить текст только своим шрифтом и только целиком: ни
    сжать строку по ширине, ни задать высоту прописной он не даёт. Поэтому
    строку переводим в контуры букв и кладём их аффинным преобразованием —
    высота прописной ровно из чертежа, ширина строки ровно та, что отведена
    ей на листе. Иначе подписи наезжают друг на друга: экранный шрифт шире
    чертёжного примерно в полтора раза.
    """
    from matplotlib.transforms import Affine2D

    x, y = item["points"][0]
    size = float(item.get("size") or 2.5)
    factor = float(item.get("wf") or 1.0)
    box = float(item.get("width") or 0.0)
    anchor = str(item.get("anchor") or "left")
    rotation = float(item.get("rot") or 0)
    lines = _text_lines(str(item["text"]))
    first = _first_baseline(len(lines), size, str(item.get("valign") or "baseline"))
    step = size * _LINE_SPACING
    em = size / _CAP_RATIO

    out = []
    for n, line in enumerate(lines):
        if not line.strip():
            continue
        path, ink = _line_path(line)
        if ink.width <= 0:
            continue
        span = text_span(line, size, factor)
        if box > size:
            span = min(span, box)
        # Крайние значения обрезаем: подпись из одних скобок или редких знаков
        # наша мерка считает почти нулевой, и буквы схлопнулись бы в полоску.
        squeeze = min(max(span / (ink.width * em), 0.25), 1.6)
        # Привязку считаем по тому, что вышло, а не по расчётной ширине: у
        # обрезанной строки они расходятся, и подпись уехала бы от своей точки.
        drawn = ink.width * em * squeeze
        shift = {"center": -drawn / 2, "right": -drawn}.get(anchor, 0.0)
        out.append(
            Affine2D()
            .translate(-ink.x0, 0)
            .scale(em * squeeze, em)
            .translate(shift, -(first + n * step))
            .rotate_deg(rotation)
            .translate(x, y)
            .transform_path(path)
        )
    return out


def render_png(primitives: list[dict], meta: dict, target: Path) -> Path | None:
    """Лист как PNG — для ленты миниатюр и как запасной путь интерфейса.

    Лист без подписей рисуется с плашкой, а не отдаётся пустым: см. `banner_for`.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as GlyphPath

    banner = banner_for(primitives, meta)
    if not primitives:
        x0, y0, x1, y1 = _BLANK_SHEET
        meta = dict(meta or {}, bbox=(x0, y0, x1, y1))
    x0, y0, x1, y1, width, height = _sheet_frame(meta)
    dpi = 100
    px_per_unit = _PREVIEW_WIDTH_PX / width
    height_px = max(300, min(int(height * px_per_unit), 3000))
    figure = plt.figure(
        figsize=(_PREVIEW_WIDTH_PX / dpi, height_px / dpi), dpi=dpi, facecolor="white"
    )
    axes = figure.add_axes([0, 0, 1, 1])
    axes.set_facecolor("white")

    # Толщина и кегль заданы в единицах чертежа, а matplotlib ждёт пункты.
    # Переводим через масштаб картинки, иначе на листе А1 линии выходят
    # ниткой, а на фрагменте — жирными полосами.
    def to_points(value: float) -> float:
        return max(value, _MIN_STROKE_MM) * px_per_unit * 72.0 / dpi

    segments: dict[tuple[str, float], list] = {}
    glyphs: dict[str, list] = {}
    for item in primitives:
        points = item.get("points") or []
        if item.get("type") == "text":
            if not points or not item.get("text"):
                continue
            glyphs.setdefault(str(item.get("color") or "#000000"), []).extend(
                _text_glyphs(item)
            )
            continue
        if len(points) < 2:
            continue
        key = (str(item.get("color") or "#000000"), float(item.get("lw") or 0.25))
        segments.setdefault(key, []).append([(px, py) for px, py in points])

    for (color, lw), lines in segments.items():
        axes.add_collection(
            LineCollection(lines, colors=color, linewidths=to_points(lw))
        )

    # Все буквы одного цвета — одной фигурой: на листе фундаментов их шесть
    # сотен, и по отдельной фигуре на подпись отрисовка идёт вдесятеро дольше.
    for color, paths in glyphs.items():
        if not paths:
            continue
        axes.add_patch(
            PathPatch(
                GlyphPath.make_compound_path(*paths),
                facecolor=color,
                edgecolor="none",
                linewidth=0,
            )
        )

    if banner:
        title, note = banner
        axes.text(
            (x0 + x1) / 2,
            (y0 + y1) / 2 + height * 0.012,
            title,
            fontsize=22,
            color="#b42318",
            fontweight="bold",
            ha="center",
            va="center",
            bbox={"facecolor": "white", "alpha": 0.88, "edgecolor": "#b42318"},
        )
        axes.text(
            (x0 + x1) / 2,
            (y0 + y1) / 2 - height * 0.018,
            note,
            fontsize=11,
            color="#667085",
            ha="center",
            va="center",
        )

    axes.set_xlim(x0, x1)
    axes.set_ylim(y0, y1)
    axes.set_aspect("equal")
    axes.axis("off")
    target.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(target, dpi=dpi, facecolor="white")
    plt.close(figure)
    return target


def sheet_preview(path: Path, page_number: int, fmt: str = "svg", target: Path | None = None):
    """Готовый предпросмотр листа документа: SVG-строка или путь к PNG."""
    from dwg_geometry import sheet_primitives

    primitives, meta, _ = sheet_primitives(path, page_number)
    # Служебный лист без координат рисовать нечем и незачем: интерфейс покажет
    # его содержимое текстом, а на месте картинки — свою плашку.
    if meta.get("flat"):
        return "" if fmt == "svg" else None
    # В остальных случаях пустой ответ означал бы для интерфейса «картинки
    # нет», и лист, потерянный конвертером, выглядел бы так же, как сбой
    # отдачи. Рисуем плашку — она и есть ответ.
    if fmt == "svg":
        return render_svg(primitives, meta)
    if target is None:
        raise ValueError("для PNG нужен путь к файлу")
    return render_png(primitives, meta, target)
