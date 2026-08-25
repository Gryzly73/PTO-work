"""Основная надпись листа: шифр, номер, наименование — одинаково из DWG и PDF.

Зачем это отдельным модулем. По одному разделу проекта приходит и графика в
DWG, и альбом в PDF, и текстовая часть отдельным PDF. Сшить их в один отчёт по
именам файлов нельзя: имена у проектировщика произвольные («10.2 Жуковский
1_(КР1)_Фундаменты_6-7.dwg»), а порядковый номер страницы в файле не совпадает
с номером листа в комплекте — в чертеже КР1 первый лист файла имеет номер 19.

Единственное, что связывает лист с комплектом надёжно, — основная надпись по
ГОСТ 21.101: обозначение документа (`28-ХСА-1/25-КР1`), номер листа, стадия.
Ими же оперирует инженер в замечаниях («лист 19 шифра КР1»), поэтому разбор
надписи нужен и сам по себе, не только ради сведения источников.

Разбор один на оба источника. Вход — плоский список подписей с координатами в
миллиметрах листа, ось Y вверх:

    [(x, y, высота_шрифта, текст), ...]

У чертежа такие координаты уже лежат в файле данными (`dwg_sheets.TextItem`), у
PDF они считаются из текстового слоя. Дальше работает одна и та же геометрия
штампа, а не два похожих алгоритма, которые расходятся при первой же правке.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PT_TO_MM = 25.4 / 72.0

# Подписи граф, по которым надпись узнаётся на листе. Ключ — нормализованный
# текст (см. `_norm`), потому что в реальных чертежах пишут и «Кол.уч.», и
# «Кол.уч», и «Кол. уч.».
_ANCHOR_WORDS = {
    "изм": "изм",
    "колуч": "колуч",
    "ндок": "ндок",
    "nдок": "ндок",
    "№док": "ндок",
    "подп": "подп",
    "подпись": "подп",
    "дата": "дата",
    "стадия": "стадия",
    "лист": "лист",
    "листов": "листов",
    "разраб": "разраб",
    "проверил": "проверил",
    "гип": "гип",
    "нконтр": "нконтр",
    "нконтроль": "нконтр",
}

# Обозначение документа: «28-ХСА-1/25-КР1», «28-ХСА-1/25-КР1.ПЗ», «2024-15-ПЗУ».
# Пробелы внутри запрещены сознательно: с ними под шаблон попадало обычное
# название листа — «Схема расположения фундаментов для блоков 1-5» тоже
# начинается с буквы и содержит цифру с дефисом, и наименование уезжало в шифр.
_CODE_RE = re.compile(r"^[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9\-./]{4,40}$", re.IGNORECASE)

# Суффикс марки текстовой части: «…-КР1.ПЗ» — тот же раздел, что «…-КР1».
_TEXT_PART_SUFFIXES = (".пз", ".тч", ".пояснительная записка")

_STAGE_VALUES = {"п", "р", "рд", "пд", "и", "э"}


def _norm(text: str) -> str:
    """Текст графы к сравнимому виду: без пробелов, точек и регистра."""
    low = text.strip().lower().replace("ё", "е")
    return re.sub(r"[\s. ]+", "", low)


@dataclass
class Item:
    """Одна подпись на листе. Координаты — мм, начало где угодно, Y вверх."""

    x: float
    y: float
    size: float
    text: str


@dataclass
class Stamp:
    """Прочитанная основная надпись. Пустая строка — «не нашли»."""

    code: str = ""          # графа 2: обозначение документа
    sheet: str = ""         # графа 6: номер листа
    sheets_total: str = ""  # графа 7: всего листов
    stage: str = ""         # графа 5: стадия
    title: str = ""         # графа 4: наименование листа
    object_name: str = ""   # графа 1: наименование стройки
    org: str = ""           # графа 9: организация
    source: str = ""        # откуда прочитано: «DWG» или «PDF»
    # Чем разбор неполон — уходит в отчёт, чтобы «нет шифра» отличалось от
    # «шифр прочитан пустым».
    note: str = ""
    fields: dict = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return bool(self.code or self.sheet or self.title)

    @property
    def section(self) -> str:
        """Раздел комплекта: шифр без суффикса текстовой части.

        `28-ХСА-1/25-КР1.ПЗ` и `28-ХСА-1/25-КР1` — один раздел КР1: первый —
        пояснительная записка, второй — чертежи. В отчёте они должны встать
        рядом, а не в два разных документа.
        """
        code = self.code.strip()
        low = code.lower()
        for suffix in _TEXT_PART_SUFFIXES:
            if low.endswith(suffix):
                return code[: -len(suffix)].rstrip(" .")
        return code

    @property
    def is_text_part(self) -> bool:
        low = self.code.strip().lower()
        return any(low.endswith(s) for s in _TEXT_PART_SUFFIXES)

    @property
    def key(self) -> tuple[str, str]:
        """Чем лист опознаётся в комплекте: (раздел, номер листа)."""
        return (self.section.upper(), self.sheet.strip())

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "section": self.section,
            "sheet": self.sheet,
            "sheetsTotal": self.sheets_total,
            "stage": self.stage,
            "title": self.title,
            "objectName": self.object_name,
            "org": self.org,
            "source": self.source,
            "note": self.note,
        }


# ── геометрия штампа ────────────────────────────────────────────────────────
#
# Все расстояния — доли от размеров надписи по ГОСТ 21.101 (185×55 мм), а не
# абсолютные миллиметры: у половины чертежей комплекта штамп нарисован не в
# масштабе 1:1, и жёсткие «7.5 мм вниз» на них не попадают никуда.


def _below(items: list[Item], anchor: Item, *, max_dy: float, max_dx: float):
    """Значение графы: ближайшая подпись под её названием.

    Ячейка подписана сверху и заполнена снизу («Стадия» → «П»), причём обе
    выровнены по одной вертикали. Поэтому ищем не «ближайший текст вообще», а
    ближайший в узкой колонке под якорем: иначе в стадию попадает фамилия из
    соседней графы, которая физически ближе по прямой.
    """
    best = None
    for item in items:
        if item is anchor:
            continue
        dy = anchor.y - item.y
        if not 0 < dy <= max_dy:
            continue
        if abs(item.x - anchor.x) > max_dx:
            continue
        if _norm(item.text) in _ANCHOR_WORDS:
            continue
        if best is None or dy < anchor.y - best.y:
            best = item
    return best


def _looks_like_code(text: str) -> bool:
    stripped = text.strip()
    if not (5 <= len(stripped) <= 40):
        return False
    if not _CODE_RE.match(stripped):
        return False
    if not any(sep in stripped for sep in "-/"):
        return False
    # Четверть знаков и больше — цифры. Без этой проверки шифром считается
    # любое слово с дефисом, набранное заглавными: «ПОЯСНИТЕЛЬНАЯ-ЗАПИСКА».
    digits = sum(1 for ch in stripped if ch.isdigit())
    return digits / len(stripped) >= 0.25


# Подписи рамки и полей листа. Стоят вплотную к надписи и лезут в графу 4:
# «Формат А2» печатается прямо под ней, «Копировал» — рядом.
_SERVICE_STARTS = (
    "формат",
    "копировал",
    "инв",
    "взам",
    "подпидата",
    "чертил",
    "масштаб",
)


def _is_service_text(text: str) -> bool:
    return _norm(text).startswith(_SERVICE_STARTS)


def _anchors(items: list[Item]) -> dict[str, list[Item]]:
    found: dict[str, list[Item]] = {}
    for item in items:
        key = _ANCHOR_WORDS.get(_norm(item.text))
        if key:
            found.setdefault(key, []).append(item)
    return found


def read(items: list[Item], *, source: str = "") -> Stamp:
    """Разобрать основную надпись из подписей листа."""
    items = [i for i in items if i.text and i.text.strip()]
    if not items:
        return Stamp(source=source, note="на листе нет подписей")

    anchors = _anchors(items)
    stamp = Stamp(source=source)

    # Стадия — самый надёжный якорь: слово встречается на листе один раз и
    # стоит в фиксированном месте надписи. От него же берём масштаб штампа:
    # расстояние «Стадия» → «Листов» по ГОСТ равно 35 мм.
    stage_anchor = _next_anchor(anchors, "стадия")
    total_anchor = _next_anchor(anchors, "листов")
    scale = 1.0
    if stage_anchor and total_anchor:
        span = abs(total_anchor.x - stage_anchor.x)
        if span > 1:
            scale = span / 35.0

    # Строка надписи по ГОСТу 8 мм, но подпись графы и её значение прижаты к
    # разным краям ячейки, и на реальных чертежах между ними выходит чуть
    # больше восьми. Берём с запасом в полторы строки: следующая графа всё
    # равно дальше.
    cell_h = 12.0 * scale
    cell_w = 9.0 * scale   # половина ячейки «Стадия»/«Лист»/«Листов»

    if stage_anchor:
        value = _below(items, stage_anchor, max_dy=cell_h, max_dx=cell_w)
        if value and _norm(value.text) in _STAGE_VALUES:
            stamp.stage = value.text.strip()
        elif value:
            stamp.stage = value.text.strip()

    sheet_anchor = _sheet_anchor(anchors, stage_anchor, total_anchor)
    if sheet_anchor:
        value = _below(items, sheet_anchor, max_dy=cell_h, max_dx=cell_w)
        if value:
            stamp.sheet = _clean_number(value.text)
    if total_anchor:
        value = _below(items, total_anchor, max_dy=cell_h, max_dx=cell_w)
        if value:
            stamp.sheets_total = _clean_number(value.text)

    stamp.code = _read_code(items, stage_anchor, sheet_anchor, scale)
    _read_titles(items, stamp, stage_anchor, scale)

    if not stamp.found:
        stamp.note = "основная надпись не распознана"
    return stamp


def _next_anchor(anchors: dict[str, list[Item]], key: str) -> Item | None:
    found = anchors.get(key) or []
    if not found:
        return None
    # Слово может встретиться и в тексте чертежа. Из нескольких берём самое
    # правое-нижнее: надпись всегда в правом нижнем углу листа.
    return max(found, key=lambda i: (i.x - i.y))


def _sheet_anchor(
    anchors: dict[str, list[Item]], stage: Item | None, total: Item | None
) -> Item | None:
    """Графа «Лист» — та, что стоит между «Стадия» и «Листов».

    Слово «Лист» есть в надписи дважды: в строке учёта изменений слева и в
    строке «Стадия | Лист | Листов» справа. Слева стоит номер изменения, а не
    номер листа, и без этой проверки в отчёт уходил бы он.
    """
    candidates = anchors.get("лист") or []
    if not candidates:
        return None
    if stage and total:
        inner = [i for i in candidates if stage.x < i.x < total.x]
        if inner:
            return min(inner, key=lambda i: abs(i.y - stage.y))
    if total:
        left = [i for i in candidates if i.x < total.x]
        if left:
            return max(left, key=lambda i: i.x)
    return max(candidates, key=lambda i: i.x)


def _clean_number(text: str) -> str:
    match = re.search(r"\d+", text)
    return match.group(0) if match else text.strip()


def _read_code(
    items: list[Item], stage: Item | None, sheet: Item | None, scale: float
) -> str:
    """Обозначение документа — графа 2, над строкой учёта изменений.

    Опознаётся не местом, а видом: шифр — самая крупная надпись в правой
    части листа, похожая на обозначение. Место у него плавает (в форме 3 он
    сверху надписи, в форме 6 текстового документа — слева от «Лист»), а
    крупный шрифт графы 2 обязателен по ГОСТ и на практике соблюдается.
    """
    anchor = stage or sheet
    if anchor is None:
        # Листа без основной надписи в комплекте не бывает, а вот текст,
        # похожий на шифр, посреди чертежа есть всегда — марка бетона, ГОСТ,
        # ссылка на другой документ. Без якоря надписи шифр не выдумываем.
        return ""
    candidates = [i for i in items if _looks_like_code(i.text)]
    if anchor is not None:
        # Ограничиваем зоной надписи: обозначение того же вида встречается в
        # штампах врезок и в ссылках на другие документы посреди чертежа.
        width = 185.0 * scale
        height = 55.0 * scale
        candidates = [
            i
            for i in candidates
            if anchor.x - width * 1.1 <= i.x <= anchor.x + width * 0.6
            and anchor.y - height <= i.y <= anchor.y + height
        ]
    if not candidates:
        return ""
    best = max(candidates, key=lambda i: (round(i.size, 1), len(i.text)))
    return best.text.strip()


def _read_titles(
    items: list[Item], stamp: Stamp, stage: Item | None, scale: float
) -> None:
    """Графы 1, 4 и 9: стройка, наименование листа, организация.

    Все три стоят одной колонкой справа. Разделить их по расстояниям из ГОСТа
    не выходит: у половины чертежей комплекта надпись растянута, а графа 1
    занимает то одну строку, то две («…комплекс «Цифровой коворкинг»» плюс
    «Универсальное индустриальное здание Block 1-7»).

    Что держится на всех проверенных листах — порядок снизу: последняя строка
    колонки и есть наименование листа, рядом с ней организация, всё, что выше,
    относится к стройке. Отсюда и разбор: сначала находим нижний уровень, а не
    отсчитываем миллиметры сверху.
    """
    if stage is None:
        return
    width = 185.0 * scale
    height = 55.0 * scale
    column = [
        i
        for i in items
        if stage.x - width * 0.55 <= i.x <= stage.x + width * 0.6
        and stage.y - height * 0.75 <= i.y <= stage.y + height * 0.55
        and len(i.text.strip()) >= 6
        and _norm(i.text) not in _ANCHOR_WORDS
        and not _looks_like_code(i.text)
        and not _is_service_text(i.text)
    ]
    if not column:
        return

    org_marks = ("ооо", "оао", "зао", "ао ", "ип ", "пао", "оао")
    org = next(
        (i for i in sorted(column, key=lambda i: i.y) if any(m in i.text.lower() for m in org_marks)),
        None,
    )
    if org is not None:
        stamp.org = org.text.strip()
        column = [i for i in column if i is not org]
    if not column:
        return

    # Нижний уровень колонки = графа 4. «Уровень», а не «строка»: наименование
    # листа часто разбито на две-три подписи на одной высоте, и брать надо
    # самую содержательную из них, а не первую попавшуюся.
    bottom = min(i.y for i in column)
    line_h = max(2.0 * scale, 4.0 * scale)
    bottom_level = [i for i in column if i.y - bottom <= line_h]
    stamp.title = max(bottom_level, key=lambda i: len(i.text)).text.strip()

    above = [i for i in column if i not in bottom_level]
    if above:
        stamp.object_name = max(above, key=lambda i: len(i.text)).text.strip()


# ── источники ───────────────────────────────────────────────────────────────


def from_dwg_sheet(sheet) -> Stamp:
    """Надпись листа чертежа. Координаты уже в миллиметрах бумаги."""
    items = [
        Item(x=t.x, y=t.y, size=getattr(t, "height", 0.0) or 0.0, text=t.text)
        for t in getattr(sheet, "texts", [])
    ]
    stamp = read(items, source="DWG")
    if not stamp.title and getattr(sheet, "title", ""):
        # Имя листа из перечня («19 Схема фундаментов блоки 1-5») — запасной
        # вариант, когда графа 4 не прочиталась.
        stamp.title = sheet.title
    if not stamp.sheet and getattr(sheet, "number", None) is not None:
        stamp.sheet = str(sheet.number)
    return stamp


def from_pdf_page(page, *, glyph_map: dict | None = None) -> Stamp:
    """Надпись страницы PDF из текстового слоя.

    У страницы координаты в пунктах и ось Y вниз — переводим в миллиметры и
    переворачиваем, чтобы дальше работала та же геометрия, что у чертежа.
    Растровый лист (скан без текстового слоя) вернёт пустую надпись: там
    реквизиты достаёт модель, а не этот разбор.
    """
    height_pt = page.rect.height
    items: list[Item] = []
    try:
        data = page.get_text("dict")
    except Exception:
        return Stamp(source="PDF", note="страница не отдала текстовый слой")
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(s.get("text", "") for s in spans)
            if glyph_map:
                from deglyph import decode

                text = decode(text, glyph_map)
            if not text.strip():
                continue
            x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
            size = max((s.get("size", 0.0) for s in spans), default=0.0)
            items.append(
                Item(
                    x=(x0 + x1) / 2 * PT_TO_MM,
                    y=(height_pt - (y0 + y1) / 2) * PT_TO_MM,
                    size=size * PT_TO_MM,
                    text=text,
                )
            )
    if not items:
        return Stamp(source="PDF", note="у страницы нет текстового слоя")
    return read(items, source="PDF")


def markdown(stamp: Stamp) -> str:
    """Надпись строками паспорта листа."""
    rows = [
        ("обозначение", stamp.code),
        ("раздел", stamp.section if stamp.section != stamp.code else ""),
        ("лист", stamp.sheet + (f" из {stamp.sheets_total}" if stamp.sheets_total else "")),
        ("стадия", stamp.stage),
        ("наименование листа", stamp.title),
        ("объект", stamp.object_name),
        ("организация", stamp.org),
    ]
    lines = [f"- {name}: {value}" for name, value in rows if value.strip()]
    if stamp.note:
        lines.append(f"- основная надпись: {stamp.note}")
    return "\n".join(lines)
