"""Второй путь чтения чертежа — прямо из DWG, минуя конвертацию в DXF.

Зачем он нужен, если основной путь работает. Конвертер теряет целые листы: у
ПЗУ комплекта «Жуковский» из тринадцати листов два («ПЗМ», «Озел. и МАФ»)
после `dwg2dxf` пусты полностью — ни рамки, ни окон вида. В самом DWG они на
месте: 7 и 13 объектов, включая окна вида и примечания.

Обиднее всего то, что содержимое этих листов из файла НЕ пропадает: чертёж
лежит в пространстве модели, и модель конвертируется целиком. Пропадают
именно окна вида — прямоугольники, которые говорят, какой кусок модели на
каком листе показан. Без них подписи «Живая изгородь» и «Площадка для отдыха»
лежат в модели и не привязываются ни к одному листу, то есть в вывод не
попадают вовсе.

Поэтому здесь читается ровно одно: окна вида потерянных листов. Текст, слои и
геометрию по-прежнему даёт основной путь — он проверен и умеет разворачивать
блоки, чего в сыром JSON нет.

Чем платим. Вывод `dwgread -O JSON` — не совсем JSON: в 77 файлах из 94
встречается голый `nan` вместо числа, ещё в шести — числа с точкой на конце
(`70314623254719279532198071210344448.`). Обе поломки чинятся подстановкой
перед разбором. Координаты местами всё равно мусорные — попадаются величины
порядка 10¹⁵⁰, — поэтому окно с нечисловыми или бессмысленными размерами
отбрасывается, а не переносится на лист.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

_READERS = ("dwgread",)

# Поломки JSON, которые пишет LibreDWG. Чиним подстановкой, а не «ремонтом на
# лету»: файл читается один раз, зато обычным json.loads.
_NAN = re.compile(r'(?<![\w."])nan(?![\w."])')
_TRAILING_DOT = re.compile(r"(\d)\.(\s*[,\]\}])")

# Дальше этого значения координаты не бывают ни у одной площадки: у самой
# дальней в комплекте — 2,3 млн единиц. Всё, что больше, — мусор разбора.
_SANE_LIMIT = 1e9


def find_reader() -> Path | None:
    """Путь к `dwgread` или None. Порядок тот же, что у конвертера в dwg_sheets."""
    explicit = os.environ.get("PTO_DWGREAD")
    if explicit and Path(explicit).exists():
        return Path(explicit)
    for name in _READERS:
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in (ROOT / "tools", ROOT.parent / "tools"):
        for name in _READERS:
            for exe in (candidate / name, candidate / f"{name}.exe"):
                if exe.exists():
                    return exe
    return None


def read_objects(path: Path, timeout: int = 900) -> dict | None:
    """Содержимое DWG как разобранный JSON. None — если прочитать не вышло.

    Отсутствие `dwgread` — не ошибка: это дополнительный путь, и без него
    конвейер работает как прежде.
    """
    reader = find_reader()
    if reader is None:
        return None
    out_dir = Path(tempfile.mkdtemp(prefix="dwgjson_"))
    target = out_dir / "doc.json"
    try:
        subprocess.run(
            [str(reader), "-O", "JSON", "-o", str(target), str(path)],
            capture_output=True,
            timeout=timeout,
        )
        if not target.exists():
            return None
        return json.loads(_repair(target.read_text(encoding="utf-8", errors="replace")))
    except Exception:
        return None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _repair(raw: str) -> str:
    """Чинит то, чем LibreDWG портит JSON, не трогая сам текст чертежа.

    Обе подстановки применяются ТОЛЬКО к строкам файла, где нет кавычек, то
    есть к голым числам: вывод отформатирован по одному значению на строку.
    Без этой оговорки правка «числа с точкой на конце» переписывала подписи —
    `"{\\fISOCPEUR|b1;Раздел 1.} Пояснительная записка"` превращалась в
    «Раздел 1.0», и весь состав проекта выглядел расхождением с DXF.
    """
    out = []
    for line in raw.splitlines(keepends=True):
        # Всё, что внутри кавычек, — текст чертежа, его не касаемся. Числовое
        # значение всегда идёт ПОСЛЕ последней кавычки строки («"VIEWSIZE":
        # nan,») либо на строке без кавычек вовсе (элемент массива координат).
        edge = line.rfind('"') + 1
        head, tail = line[:edge], line[edge:]
        tail = _NAN.sub("null", tail)
        tail = _TRAILING_DOT.sub(r"\g<1>.0\g<2>", tail)
        out.append(head + tail)
    return "".join(out)


def _handle(value) -> int | None:
    """Последний элемент ссылки — это и есть номер объекта."""
    if isinstance(value, list) and value:
        last = value[-1]
        return last if isinstance(last, int) else None
    return value if isinstance(value, int) else None


def _number(value) -> float | None:
    """Число из JSON, если оно вообще число и не мусор разбора."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if math.isnan(value) or math.isinf(value) or abs(value) > _SANE_LIMIT:
        return None
    return float(value)


def _point(value) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    x, y = _number(value[0]), _number(value[1])
    return None if x is None or y is None else (x, y)


def layout_viewports(data: dict) -> dict[str, list[dict]]:
    """Окна вида по имени листа: {'6 Озел. и МАФ': [{...}, ...]}.

    Читаем через `ownerhandle` самого окна, а не через список объектов листа:
    ссылка от ребёнка к родителю у LibreDWG заполнена надёжнее.
    """
    objects = data.get("OBJECTS") or []
    # Имя листа лежит в LAYOUT, а окна ссылаются на BLOCK_HEADER этого листа.
    block_to_name: dict[int, str] = {}
    for item in objects:
        if item.get("object") != "LAYOUT":
            continue
        name = item.get("layout_name") or item.get("name")
        block = _handle(item.get("block_header"))
        if isinstance(name, str) and block is not None:
            block_to_name[block] = name

    found: dict[str, list[dict]] = {}
    for item in objects:
        if item.get("entity") != "VIEWPORT":
            continue
        owner = _handle(item.get("ownerhandle"))
        name = block_to_name.get(owner)
        if name is None:
            continue
        window = _viewport(item)
        if window is not None:
            found.setdefault(name, []).append(window)
    return found


def all_texts(data: dict) -> list[str]:
    """Все подписи файла подряд — для сверки полноты с основным путём.

    `ATTDEF` сюда не входит: это определение атрибута внутри блока, шаблон, а
    не то, что нарисовано на листе. В комплекте таких 270 штук на один файл, и
    в сверке они выглядели бы потерянным текстом.
    """
    out: list[str] = []
    for item in data.get("OBJECTS") or []:
        if item.get("entity") not in ("TEXT", "MTEXT", "ATTRIB"):
            continue
        value = item.get("text_value") or item.get("text") or ""
        if isinstance(value, str) and value.strip():
            out.append(value)
    return out


def layout_texts(data: dict) -> dict[str, list[dict]]:
    """Подписи, лежащие на самой бумаге листа, по имени листа.

    Нужны для потерянных листов: примечание «Сетка квадратов для подсчёта
    объёмов…» у листа ПЗМ нарисовано прямо на бумаге, а не в модели, и через
    окна вида его не достать — вместе с листом его теряет конвертер.

    Берём только прямых детей листа. Разворачивать вложенные блоки здесь
    нечем: в сыром JSON нет ни преобразований вставок, ни текста рамки —
    этим занимается основной путь, где блоки разворачивает ezdxf.
    """
    objects = data.get("OBJECTS") or []
    block_to_name: dict[int, str] = {}
    for item in objects:
        if item.get("object") != "LAYOUT":
            continue
        name = item.get("layout_name") or item.get("name")
        block = _handle(item.get("block_header"))
        if isinstance(name, str) and block is not None:
            block_to_name[block] = name

    found: dict[str, list[dict]] = {}
    for item in objects:
        kind = item.get("entity")
        if kind not in ("TEXT", "MTEXT", "ATTRIB"):
            continue
        name = block_to_name.get(_handle(item.get("ownerhandle")))
        if name is None:
            continue
        value = item.get("text_value") or item.get("text") or ""
        if not isinstance(value, str) or not value.strip():
            continue
        point = _point(item.get("ins_pt"))
        if point is None:
            continue
        height = _number(item.get("text_height")) or _number(item.get("height")) or 2.5
        found.setdefault(name, []).append(
            {
                "text": value,
                "kind": kind,
                "x": point[0],
                "y": point[1],
                "height": height,
                "width": _number(item.get("rect_width")) or 0.0,
                # Во сколько раз знак сжат по ширине: в штампе так подписи
                # укладывают в графу. Без множителя они рисуются шире её.
                "factor": _number(item.get("width_factor")) or 1.0,
                "rotation": _number(item.get("rotation")) or 0.0,
            }
        )
    return found


def _viewport(item: dict) -> dict | None:
    """Окно вида в тех же величинах, что читает основной путь из DXF.

    Возвращает None, если хоть одно число испорчено: лучше не показать окно,
    чем перенести подписи по мусорным координатам.
    """
    paper_center = _point(item.get("center"))
    paper_width = _number(item.get("width"))
    paper_height = _number(item.get("height"))
    view_center = _point(item.get("VIEWCTR"))
    view_height = _number(item.get("VIEWSIZE"))
    target = _point(item.get("view_target")) or (0.0, 0.0)
    twist = _number(item.get("VIEWTWIST")) or 0.0
    if None in (paper_center, paper_width, paper_height, view_center, view_height):
        return None
    if paper_width <= 0 or paper_height <= 0 or view_height <= 0:
        return None
    return {
        "paper_center": paper_center,
        "paper_size": (paper_width, paper_height),
        "view_center": view_center,
        "view_height": view_height,
        "view_target": target,
        "twist": twist,
    }
