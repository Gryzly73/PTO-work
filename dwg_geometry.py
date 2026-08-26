"""Геометрия листа чертежа таблицей: линии, дуги и подписи с координатами.

Зачем это, если есть картинка. Картинка — это пиксели: по ней нельзя ни найти
текст, ни подсветить найденное, ни привязать замечание к конкретной подписи, а
при увеличении она мылится. Здесь лист отдаётся ПЕРВИЧНЫМИ ДАННЫМИ — тем, из
чего он нарисован, — и интерфейс рисует его сам: зум без потери качества, текст
выделяется и ищется, отметка ложится на координаты чертежа, а не на растр.

Формат — CSV, одна строка на примитив, чтобы читалось чем угодно и без
библиотек. Подробности формата и как по нему рисовать — в шапке `to_csv()`.

Геометрию собираем не сами, а через рисовальщик ezdxf: он разворачивает блоки,
применяет повороты и масштабы вставок и приводит дуги к полилиниям. Мы
подставляем ему вместо холста запись (`Recorder`) и забираем готовые примитивы
в мировых координатах. Текст при этом не векторизуем (`TextPolicy.IGNORE`) —
он берётся строками из разбора листа, иначе подпись превратилась бы в сотню
кривых и потеряла смысл.
"""
from __future__ import annotations

import csv
import io

from ezdxf.addons.drawing import Frontend, RenderContext, config, recorder

from dwg_render import SKIP_TYPES, _configuration, _entities

# Точность координат в файле. Чертёж в миллиметрах, десятой доли достаточно:
# на листе это микрон, а объём файла падает вдвое против полной точности.
_ROUND = 1

# Насколько точно кривые превращаются в ломаные — в единицах чертежа.
_FLATTEN = 0.5

# Потолок на число примитивов. Стройгенплан целиком — это сотни тысяч
# отрезков: браузер такое рисует минуту, а инженеру столько и не нужно.
MAX_PRIMITIVES = 60000

_HEADER = [
    "type",
    "layer",
    "color",
    "lw",
    "geom",
    "text",
    "size",
    "rot",
    "anchor",
    "valign",
    "width",
]


def _one_line(text: str) -> str:
    """Подпись в одну строку CSV: настоящие переносы — двумя символами.

    В чертеже примечание на пол-листа — это один MTEXT с переносами. Свести
    их к пробелам нельзя: интерфейс нарисует строку в километр длиной, и она
    уедет за рамку. Настоящий перевод строки в CSV тоже не годится — запись
    станет многострочной. Отдаём литерал, а разрывает его интерфейс.
    """
    parts = text.replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))
    return (chr(92) + "n").join(
        " ".join(chunk.split()) for chunk in parts.split(chr(10))
    )


# Во сколько раз средняя ширина знака меньше его высоты. Чертёжные шрифты
# (ISOCPEUR и родня) узкие; коэффициент подобран по реальным подписям
# комплекта: при нём строка рвётся там же, где в самом чертеже.
_CHAR_RATIO = 0.55


def _wrapped(item) -> str:
    """Подпись строками, как она стоит на листе.

    В чертеже у текстового блока есть ширина, и AutoCAD переносит по ней
    длинную строку сам. Мы этой ширины не применяли, и название объекта из
    штампа — 178 знаков — рисовалось одной лентой поперёк листа.

    Переносы отдаём литералом «\n»: строка CSV остаётся одной строкой.
    """
    lines = item.text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    width = getattr(item, "width", 0.0) or 0.0
    size = item.height or 2.5
    limit = int(width / max(size * _CHAR_RATIO, 1e-6)) if width > 0 else 0
    out: list[str] = []
    for line in lines:
        line = " ".join(line.split())
        if limit < 4 or len(line) <= limit:
            out.append(line)
            continue
        current = ""
        for word in line.split(" "):
            if current and len(current) + 1 + len(word) > limit:
                out.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            out.append(current)
    # Перенос отдаём двумя символами, а не настоящим переводом строки: иначе
    # запись CSV станет многострочной, и интерфейсу понадобится тяжёлый парсер.
    return (chr(92) + "n").join(out)


def _points_of(record) -> list[tuple[float, float]]:
    """Вершины записи рисовальщика в мировых координатах."""
    if hasattr(record, "points"):
        return [(v.x, v.y) for v in record.points.vertices()]
    if hasattr(record, "path"):
        try:
            return [(v.x, v.y) for v in record.path.flattening(_FLATTEN)]
        except Exception:
            return [(v.x, v.y) for v in record.path.control_vertices()]
    if hasattr(record, "paths"):
        vertices: list[tuple[float, float]] = []
        for path in record.paths:
            try:
                vertices += [(v.x, v.y) for v in path.flattening(_FLATTEN)]
            except Exception:
                continue
        return vertices
    return []


def _settings() -> config.Configuration:
    base = _configuration()
    return config.Configuration(
        color_policy=base.color_policy,
        background_policy=base.background_policy,
        hatch_policy=base.hatch_policy,
        lineweight_scaling=base.lineweight_scaling,
        text_policy=config.TextPolicy.IGNORE,
    )


def _record(doc, entities) -> list[dict]:
    """Рисует объекты в запись и отдаёт их линиями."""
    rec = recorder.Recorder()
    frontend = Frontend(RenderContext(doc), rec, config=_settings())
    for entity in entities:
        try:
            frontend.draw_entities([entity])
        except Exception:
            continue  # вырожденная геометрия не должна стоить всего листа
    out: list[dict] = []
    for record, properties in rec.player().recordings():
        points = _points_of(record)
        if len(points) < 2:
            continue
        out.append(
            {
                "type": "polyline" if len(points) > 2 else "line",
                "layer": properties.layer or "",
                "color": properties.color[:7] if properties.color else "#000000",
                "lw": round(float(properties.lineweight or 0.25), 2),
                "points": points,
            }
        )
        if len(out) >= MAX_PRIMITIVES:
            break
    return out


def sheet_geometry(doc, box, sheet) -> tuple[list[dict], dict]:
    """Примитивы листа и его метаданные — всё в координатах листа.

    Лист собирается из двух источников сразу: рамка со штампом нарисованы на
    самой бумаге, а чертёж лежит в модели и показан через окна вида. Их
    координаты различаются на порядки (миллиметры листа против мировых
    координат площадки), поэтому содержимое модели переносится в лист тем же
    преобразованием, что и подписи. Без этого лист выглядит пустым: обе части
    сжимаются в точки по разным углам кадра.
    """
    primitives: list[dict] = []

    # 1. То, что нарисовано на самом листе: рамка, штамп, таблицы.
    layout = None
    if sheet.layout_name:
        try:
            layout = doc.layouts.get(sheet.layout_name)
        except Exception:
            layout = None
    if layout is not None:
        primitives.extend(_record(doc, [e for e in layout if e.dxftype() not in SKIP_TYPES]))

    # 2. Чертёж из модели — по каждому окну вида, с переносом в лист.
    if sheet.views:
        for view in sheet.views:
            in_view = _entities(doc, view.world)
            for item in _record(doc, in_view):
                item["points"] = [view.to_paper(x, y) for x, y in item["points"]]
                item["lw"] = item["lw"]
                primitives.append(item)
                if len(primitives) >= MAX_PRIMITIVES:
                    break
    else:
        # Лист найден по рамке в модели: чертёж и рамка уже в одной системе.
        primitives.extend(_record(doc, _entities(doc, box)))

    for item in sheet.texts:
        primitives.append(
            {
                "type": "text",
                "layer": "",
                "color": "#000000",
                "lw": 0,
                "points": [(item.x, item.y)],
                # Переносы внутри подписи сохраняем литералом: строка CSV
                # остаётся одной строкой, а где разрывать — знает интерфейс.
                "text": _wrapped(item),
                "width": round(item.width, 1) or "",
                "size": round(item.height, 2),
                "rot": round(item.rotation, 1) or "",
                "anchor": item.anchor,
                "valign": item.valign,
            }
        )

    # Границы кадра считаем по тому, что реально собрали, а не по окну
    # вьюпорта: на листе СПИС комплекта ПОС окно вида в 20 раз больше самого
    # чертежа, и по нему лист выглядит пустым — содержимое жмётся в угол.
    xs = [x for item in primitives for x, _ in item["points"]]
    ys = [y for item in primitives for _, y in item["points"]]
    if xs and ys:
        pad_x = (max(xs) - min(xs)) * 0.03 or 1.0
        pad_y = (max(ys) - min(ys)) * 0.03 or 1.0
        x0, y0 = min(xs) - pad_x, min(ys) - pad_y
        x1, y1 = max(xs) + pad_x, max(ys) + pad_y
    else:
        x0, y0, x1, y1 = box
    meta = {
        "bbox": [round(v, _ROUND) for v in (x0, y0, x1, y1)],
        "scale": sheet.scale(),
        "primitives": len(primitives),
        "texts": len(sheet.texts),
        # Лист не пережил конвертацию — отрисовке это нужно, чтобы отличить
        # «здесь чертёж без подписей» от «листа до нас не дошло».
        "lost": bool(sheet.lost),
        "blank": bool(sheet.blank),
    }
    return primitives, meta


def to_csv(primitives: list[dict], meta: dict, units: str = "mm") -> str:
    """Примитивы в CSV, как их ждёт интерфейс.

    Первая строка — метаданные листа, начинается с `#`; за ней обычный CSV с
    заголовком. Колонки:

      type    line | polyline | text
      layer   слой чертежа (для фильтра «показать только сети»)
      color   цвет линии, #rrggbb — уже приведён к светлому фону
      lw      толщина линии в миллиметрах
      geom    координаты через пробел: «x1 y1 x2 y2 …», у текста — точка вставки
      text    содержимое подписи (только у type=text)
      size    высота текста в единицах чертежа
      rot     поворот текста в градусах
      anchor  привязка по горизонтали: left | center | right
      valign  привязка по вертикали: top | middle | bottom | baseline
      width   ширина текстового блока: по ней подпись переносится по строкам

    Переносы внутри подписи записаны литералом «\n» (два символа), чтобы
    строка CSV осталась одной строкой.

    Координаты — в единицах чертежа, ось Y направлена ВВЕРХ (как в чертеже, а
    не как в экране). Интерфейсу нужно перевернуть Y и вписать bbox в холст.
    """
    buffer = io.StringIO()
    bbox = ",".join(str(v) for v in meta["bbox"])
    scale = f"1:{meta['scale']:g}" if meta.get("scale") else ""
    buffer.write(
        f"# bbox={bbox}; units={units}; scale={scale}; "
        f"primitives={meta['primitives']}; y=up\n"
    )
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_HEADER)
    for item in primitives:
        geom = " ".join(
            f"{round(value, _ROUND):g}" for point in item["points"] for value in point
        )
        writer.writerow(
            [
                item["type"],
                item["layer"],
                item["color"],
                item["lw"] or "",
                geom,
                item.get("text", ""),
                item.get("size", ""),
                item.get("rot", ""),
                item.get("anchor", ""),
                item.get("valign", ""),
                item.get("width", ""),
            ]
        )
    return buffer.getvalue()


# Разбор листа стоит от секунды до минуты, а интерфейс запрашивает подряд
# геометрию и картинку одного и того же листа — это три полных прохода по
# чертежу вместо одного. Держим последние разобранные листы; больше четырёх
# ни к чему: листают по одному, а лист стройгенплана весит десятки мегабайт.
_SHEET_CACHE: dict[tuple, tuple[list[dict], dict, str]] = {}
_SHEET_CACHE_SIZE = 4


def sheet_primitives(
    path, page_number: int, units: str = "mm"
) -> tuple[list[dict], dict, str]:
    """Лист документа: примитивы, метаданные и единицы измерения.

    Общий вход и для CSV, и для картинки листа. Раньше картинку рисовал
    отдельный код, прямо по координатам модели, и она не совпадала с
    геометрией: на листе ПЗУ чертёж занимал две десятых процента кадра,
    потому что охват считался по подписям бумаги вместе с окнами вида —
    по двум системам координат сразу.
    """
    import ezdxf

    from dwg_render import sheet_box
    from dwg_sheets import sheets_for

    from pathlib import Path as _Path

    source = _Path(path)
    try:
        key = (str(source.resolve()), source.stat().st_mtime, page_number)
    except OSError:
        key = None
    if key is not None and key in _SHEET_CACHE:
        return _SHEET_CACHE[key]

    dxf_path, sheets = sheets_for(path)
    if not 1 <= page_number <= len(sheets):
        raise IndexError(f"в чертеже {len(sheets)} листов, запрошен {page_number}")
    sheet = sheets[page_number - 1]
    # Служебный список подписей без координат («Текст из исходного DWG, не
    # найденный на листах»). Рисовать его нельзя: всё легло бы в одну точку.
    # Пустой ответ честнее — интерфейс покажет содержимое текстом.
    if getattr(sheet, "flat", False):
        return [], {"flat": True, "primitives": 0, "texts": len(sheet.texts)}, units
    box = sheet_box(sheet)
    if box is None:
        # Рисовать нечего, но отрисовке всё равно надо знать, почему: лист
        # потерян конвертером или просто не дал кадра.
        return [], {"lost": bool(sheet.lost), "blank": bool(sheet.blank), "primitives": 0, "texts": 0}, units
    doc = ezdxf.readfile(str(dxf_path))
    if sheet.views:
        # Лист собран в координатах бумаги, а бумага всегда в миллиметрах —
        # независимо от того, в чём вычерчена сама модель. У ПЗУ модель в
        # метрах, и без этой поправки интерфейс считал бы лист размером
        # 1783 метра.
        units = "mm"
    else:
        unit_code = int(doc.header.get("$INSUNITS", 0) or 0)
        units = {6: "m", 4: "mm", 5: "cm"}.get(unit_code, units)
    primitives, meta = sheet_geometry(doc, box, sheet)
    if key is not None:
        if len(_SHEET_CACHE) >= _SHEET_CACHE_SIZE:
            _SHEET_CACHE.pop(next(iter(_SHEET_CACHE)))
        _SHEET_CACHE[key] = (primitives, meta, units)
    return primitives, meta, units


def sheet_csv(path, page_number: int, units: str = "mm") -> str:
    """Готовый CSV листа документа."""
    primitives, meta, units = sheet_primitives(path, page_number, units)
    if not primitives:
        return ""
    return to_csv(primitives, meta, units)
