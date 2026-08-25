"""Отрисовка листа чертежа: SVG для показа и PNG для миниатюр.

Зачем это здесь. У PDF интерфейс рисует страницу сам через pdf.js, у чертежа
рисовать нечего: DWG браузер не открывает. Пока картинки нет, инженер видит
пустое место рядом с расшифровкой и не может сверить одно с другим.

Отрисовка ezdxf на выходе LibreDWG требует трёх поправок, без которых
получается чёрный лист или точка посреди пустоты:

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

from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.addons.drawing import Frontend, RenderContext, config, layout, svg
from ezdxf.math import BoundingBox2d

# Маскировки, подложки и размеры — см. шапку модуля.
SKIP_TYPES = ("WIPEOUT", "IMAGE", "DIMENSION")

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
    """Объекты листа, годные к отрисовке, не больше MAX_ENTITIES."""
    picked = []
    for entity in doc.modelspace():
        if entity.dxftype() in SKIP_TYPES or not _on_sheet(entity, box):
            continue
        picked.append(entity)
        if len(picked) >= MAX_ENTITIES:
            break
    return picked


def _configuration() -> config.Configuration:
    return config.Configuration(
        color_policy=config.ColorPolicy.COLOR_SWAP_BW,
        background_policy=config.BackgroundPolicy.OFF,
        hatch_policy=config.HatchPolicy.IGNORE,
        lineweight_scaling=0.5,
    )


def _draw(frontend, entities) -> int:
    """Рисует по одному объекту: вырожденная геометрия иначе роняет весь лист."""
    drawn = 0
    for entity in entities:
        try:
            frontend.draw_entities([entity])
            drawn += 1
        except Exception:
            continue
    return drawn


def render_svg(doc, box, width_mm: float = 1000.0) -> str:
    """Лист как SVG. Пустая строка — если рисовать нечего."""
    entities = _entities(doc, box)
    if not entities or box is None:
        return ""
    backend = svg.SVGBackend()
    frontend = Frontend(RenderContext(doc), backend, config=_configuration())
    if not _draw(frontend, entities):
        return ""
    x0, y0, x1, y1 = box
    height_mm = max(50.0, width_mm * (y1 - y0) / max(x1 - x0, 1e-9))
    page = layout.Page(width_mm, height_mm, layout.Units.mm, layout.Margins.all(2))
    image = backend.get_string(
        page,
        settings=layout.Settings(fit_page=True),
        render_box=BoundingBox2d([(x0, y0), (x1, y1)]),
    )
    return _with_white_background(image)


def _with_white_background(image: str) -> str:
    """Подкладывает лист белым.

    Без подложки SVG прозрачен, и в браузере с тёмной темой чёрные линии
    чертежа оказываются на тёмном фоне — лист выглядит пустым.
    """
    if not image:
        return image
    end = image.find(">", image.find("<svg"))
    if end < 0:
        return image
    return (
        image[: end + 1]
        + '<rect width="100%" height="100%" fill="#ffffff"/>'
        + image[end + 1 :]
    )


def render_png(doc, box, target: Path, width_px: int = 1400) -> Path | None:
    """Лист как PNG — для ленты миниатюр. None, если рисовать нечего."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    entities = _entities(doc, box)
    if not entities or box is None:
        return None
    x0, y0, x1, y1 = box
    ratio = (y1 - y0) / max(x1 - x0, 1e-9)
    height_px = max(300, min(int(width_px * ratio), 3000))
    dpi = 100
    figure = plt.figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    axes = figure.add_axes([0, 0, 1, 1])
    figure.patch.set_facecolor("white")
    axes.set_facecolor("white")
    backend = MatplotlibBackend(axes)
    frontend = Frontend(RenderContext(doc), backend, config=_configuration())
    drawn = _draw(frontend, entities)
    backend.finalize()
    # Границы ставим после отрисовки: автомасштаб уводит вид на выбросы
    # координат, которые оставляет вырожденная геометрия.
    axes.set_xlim(x0, x1)
    axes.set_ylim(y0, y1)
    axes.axis("off")
    target.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(target, dpi=dpi, facecolor="white")
    plt.close(figure)
    return target if drawn else None


def sheet_preview(path: Path, page_number: int, fmt: str = "svg", target: Path | None = None):
    """Готовый предпросмотр листа документа: SVG-строка или путь к PNG."""
    from dwg_sheets import sheets_for

    dxf_path, sheets = sheets_for(path)
    if not 1 <= page_number <= len(sheets):
        raise IndexError(f"в чертеже {len(sheets)} листов, запрошен {page_number}")
    sheet = sheets[page_number - 1]
    box = sheet_box(sheet)
    if box is None:
        return "" if fmt == "svg" else None
    doc = ezdxf.readfile(str(dxf_path))
    if fmt == "svg":
        return render_svg(doc, box)
    if target is None:
        raise ValueError("для PNG нужен путь к файлу")
    return render_png(doc, box, target)
