"""Чтение листов из DWG: векторная истина вместо отрисованного PDF.

Зачем это отдельно от PDF-конвейера. В PDF текст чертежа приходит через
отрисовку: шрифт с битым ToUnicode отдаёт кракозябры, пунктуация теряется, а
масштаб и номер листа приходится вычитывать глазами модели из штампа. В DWG
всё это лежит данными: текст — текстом, координаты — числами, номер листа — в
имени layout'а, масштаб — в параметрах вьюпорта. Проверено на реальном
комплекте: там, где PDF отдавал «jПроизводственно\x0eскладской» и
«28 ХСА 1 25 ИОС2», DWG отдаёт «Производственно-складской» и
«28-ХСА-1/25-ИОС5.С» без единой поправки.

Что здесь есть и чего нет. Модуль отвечает на вопрос «какой текст на каком
листе и где именно» — то есть заменяет собой текстовый слой PDF. Описание
графики (что нарисовано, как идут системы) остаётся за VLM, как и было: линии
и штриховки словами не пересказать.

Как устроен DWG проектного комплекта (выведено по файлам «Жуковский»):

  * Геометрия целиком лежит в ПРОСТРАНСТВЕ МОДЕЛИ, в мировых координатах.
  * Лист — это layout, и он почти пуст: в нём только вьюпорты, окна в модель.
    Номер листа стоит в имени: «22 Фундамент Ф-1».
  * У вьюпорта есть размер на бумаге и высота вида в модели. Их отношение —
    и есть масштаб листа (594x420 мм при высоте вида 16800 → 1:40).
  * Больше половины текста лежит ВНУТРИ блоков (штампы, выноски, маркировка)
    и в размерных объектах. Брать только верхний уровень нельзя.

Использование:
    python dwg_sheets.py "чертёж.dwg"                  # обзор листов
    python dwg_sheets.py "чертёж.dwg" -o лист.md       # markdown по контракту
    python dwg_sheets.py "чертёж.dxf" --sheet 22       # один лист
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Имя конвертера DWG → DXF. Своего чтения DWG в Python нет: формат закрытый и
# двоичный, ezdxf читает только DXF. Берём внешний конвертер — тот, что найдём.
_CONVERTERS = ("dwg2dxf", "ODAFileConverter")

# Сколько миллиметров бумаги считаем одним «шагом» при поиске соседних строк.
# Текст на листе выровнен по строкам, но не идеально: подписи в штампе гуляют
# на доли миллиметра, и без допуска каждая уезжает в свою строку.
LINE_TOL_RATIO = 0.6  # доля высоты текста

# Вьюпорт с id=1 — это сама бумага, а не окно в модель. Он есть всегда и
# охватывает лист целиком; содержимое чертежа показывают остальные.
PAPER_VIEWPORT_ID = 1


@dataclass
class TextItem:
    """Кусок текста с местом в мировых координатах модели."""

    x: float
    y: float
    height: float
    text: str
    source: str  # mtext | text | attrib | dimension | leader


@dataclass
class Sheet:
    """Лист комплекта — то, что в PDF было страницей."""

    number: int | None
    name: str
    paper: str
    paper_width: float
    paper_height: float
    scales: list[float] = field(default_factory=list)
    texts: list[TextItem] = field(default_factory=list)
    # Прямоугольники модели, которые лист показывает: окна вьюпортов или сама
    # рамка. По ним лист и разбирается — что на нём есть и где именно.
    # (x0, y0, x1, y1, масштаб) — окна вьюпортов или сама рамка.
    windows: list[tuple[float, float, float, float, float]] = field(default_factory=list)

    def extent(self) -> tuple[float, float, float, float] | None:
        """Общий охват листа в координатах модели."""
        if not self.windows:
            return None
        return (
            min(w[0] for w in self.windows),
            min(w[1] for w in self.windows),
            max(w[2] for w in self.windows),
            max(w[3] for w in self.windows),
        )

    def main_window(self) -> tuple[float, float, float, float, float] | None:
        """Самое большое окно — основной вид листа."""
        if not self.windows:
            return None
        return max(self.windows, key=lambda w: (w[2] - w[0]) * (w[3] - w[1]))

    def scale(self) -> float:
        """Масштаб ОСНОВНОГО вида. 0 — неизвестен.

        Не «самый частый»: врезок на листе обычно больше, чем основных видов,
        и по частоте масштаб получался от врезки. У листа 22 комплекта КР1 это
        давало 1:20 вместо настоящего 1:40.
        """
        main = self.main_window()
        return main[4] if main else 0.0

    @property
    def title(self) -> str:
        """Имя листа без ведущего номера: «22 Фундамент Ф-1» → «Фундамент Ф-1»."""
        return re.sub(r"^\s*\d+[.\s]*", "", self.name).strip() or self.name


def find_converter() -> Path | None:
    """Путь к конвертеру DWG → DXF или None.

    Порядок: явно заданный PTO_DWG2DXF, затем PATH, затем папка tools рядом с
    репозиторием. Явное указание нужно потому, что LibreDWG распространяется
    zip-архивом и в PATH сам не прописывается.
    """
    explicit = os.environ.get("PTO_DWG2DXF")
    if explicit and Path(explicit).exists():
        return Path(explicit)
    for name in _CONVERTERS:
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in (ROOT / "tools", ROOT.parent / "tools"):
        for name in _CONVERTERS:
            for exe in (candidate / name, candidate / f"{name}.exe"):
                if exe.exists():
                    return exe
    return None


def to_dxf(path: Path, out_dir: Path | None = None) -> Path:
    """DWG → DXF. Если на вход уже DXF, возвращает его же."""
    if path.suffix.lower() == ".dxf":
        return path
    converter = find_converter()
    if converter is None:
        raise RuntimeError(
            "Не найден конвертер DWG → DXF. Поставьте LibreDWG (dwg2dxf) или "
            "ODA File Converter и укажите путь в PTO_DWG2DXF."
        )
    out_dir = out_dir or Path(tempfile.mkdtemp(prefix="dwg2dxf_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / (path.stem + ".dxf")
    # dwg2dxf молчалив в успехе и болтлив в предупреждениях: часть объектов он
    # пропускает («Unknown object»). Это не отказ — файл читаемый, но полноту
    # стоит сверять с ODA, когда есть с чем сравнить.
    result = subprocess.run(
        [str(converter), "-o", str(target), str(path)],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if not target.exists():
        raise RuntimeError(
            f"Конвертер не создал {target.name}: {result.stderr.strip()[:300]}"
        )
    return target


# Управляющие последовательности AutoCAD внутри текста. Они универсальны для
# формата, а не для конкретного чертежа: «%%d» — градус, «%%c» — диаметр.
# Без замены в вывод едет «N%%dдок» вместо «N°док» и «%%c325» вместо «∅325»,
# а диаметр — это то, что ПТО сверяет с ТЗ.
_ACAD_CODES = (
    ("%%d", "°"),
    ("%%D", "°"),
    ("%%p", "±"),
    ("%%P", "±"),
    ("%%c", "∅"),
    ("%%C", "∅"),
    ("%%%", "%"),
    ("%%u", ""),
    ("%%U", ""),
    ("%%o", ""),
    ("%%O", ""),
)


def clean_text(raw: str) -> str:
    """Убирает то, что не переживёт запись в файл.

    Конвертер оставляет в тексте одиночные суррогаты — половинки символов UTF-16,
    которые в UTF-8 не кодируются вовсе, и запись markdown падает на середине
    документа. Плюс управляющие коды: в чертеже они разделяют строки внутри
    MTEXT, а в готовом тексте выглядят мусором.
    """
    if not raw:
        return ""
    out = []
    for ch in raw:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:  # одинокий суррогат
            continue
        if code < 0x20 and ch not in (chr(10), chr(9)):
            continue
        out.append(ch)
    text = "".join(out)
    for code, repl in _ACAD_CODES:
        if code in text:
            text = text.replace(code, repl)
    return text


def _plain(entity) -> str:
    """Текст сущности без разметки MTEXT."""
    try:
        if entity.dxftype() == "MTEXT":
            return clean_text(entity.plain_text())
        return clean_text(entity.dxf.text)
    except Exception:
        return ""


def _text_height(entity) -> float:
    for attr in ("char_height", "height"):
        try:
            value = float(entity.dxf.get(attr))
            if value > 0:
                return value
        except Exception:
            continue
    return 2.5


def _collect_from(space, out: list[TextItem], depth: int = 0) -> None:
    """Собирает текст из пространства, разворачивая блоки.

    Блоки разворачиваем через virtual_entities(): у вставки свои поворот и
    масштаб, и координаты внутреннего текста без пересчёта врут. Глубину
    ограничиваем: блок в блоке в блоке встречается, бесконечная вложенность —
    нет, а зациклиться на битом файле не хочется.
    """
    if depth > 3:
        return
    for e in space:
        kind = e.dxftype()
        try:
            if kind in ("MTEXT", "TEXT"):
                text = _plain(e).strip()
                if text:
                    p = e.dxf.insert
                    out.append(
                        TextItem(p.x, p.y, _text_height(e), text, kind.lower())
                    )
            elif kind == "ATTRIB":
                text = clean_text(e.dxf.text or "").strip()
                if text:
                    p = e.dxf.insert
                    out.append(TextItem(p.x, p.y, _text_height(e), text, "attrib"))
            elif kind == "DIMENSION":
                # Размер несёт либо явный текст, либо измеренное значение.
                text = clean_text(e.dxf.get("text", "") or "").strip()
                # «<>» — место, куда AutoCAD подставляет измеренное значение.
                # Оно бывает и с приставкой: «з.с.<>» = «з.с.240».
                if text in ("", "<>") or "<>" in text:
                    try:
                        value = f"{e.get_measurement():.0f}"
                    except Exception:
                        value = ""
                    text = value if text in ("", "<>") else text.replace("<>", value)
                if text:
                    p = e.dxf.text_midpoint
                    out.append(TextItem(p.x, p.y, 2.5, text, "dimension"))
            elif kind == "MULTILEADER":
                try:
                    text = clean_text(e.get_mtext_content() or "").strip()
                except Exception:
                    text = ""
                if text:
                    p = e.dxf.get("insert", None)
                    x, y = (p.x, p.y) if p is not None else (0.0, 0.0)
                    out.append(TextItem(x, y, 2.5, text, "leader"))
            elif kind == "INSERT":
                for attrib in e.attribs:
                    text = clean_text(attrib.dxf.text or "").strip()
                    if text:
                        p = attrib.dxf.insert
                        out.append(
                            TextItem(p.x, p.y, _text_height(attrib), text, "attrib")
                        )
                _collect_from(e.virtual_entities(), out, depth + 1)
        except Exception:
            # Один битый объект не должен ронять разбор листа.
            continue


def _viewport_window(vp) -> tuple[float, float, float, float, float] | None:
    """Окно вьюпорта в координатах модели: (x0, y0, x1, y1, масштаб).

    Масштаб — во сколько раз модель крупнее бумаги: высота вида, делённая на
    высоту окна на бумаге. Для листа 1:40 это ровно 40.
    """
    try:
        if int(vp.dxf.get("id", 0)) == PAPER_VIEWPORT_ID:
            return None
        center = vp.dxf.view_center_point
        view_h = float(vp.dxf.view_height)
        paper_w = float(vp.dxf.width)
        paper_h = float(vp.dxf.height)
    except Exception:
        return None
    if view_h <= 0 or paper_h <= 0:
        return None
    view_w = view_h * (paper_w / paper_h)
    return (
        center.x - view_w / 2,
        center.y - view_h / 2,
        center.x + view_w / 2,
        center.y + view_h / 2,
        view_h / paper_h,
    )


# Форматы по ГОСТ 2.301 / ISO 216, в миллиметрах. Нужны, чтобы по размеру
# рамки в модели восстановить и формат листа, и масштаб чертежа.
_ISO_SIZES = {
    "A0": (841, 1189),
    "A1": (594, 841),
    "A2": (420, 594),
    "A3": (297, 420),
    "A4": (210, 297),
}

# Допуск на соотношение сторон листа: у ISO это корень из двух.
_ISO_RATIO = 2 ** 0.5
_RATIO_TOL = 0.03

# Рамка мельче этого — не лист, а рамочка внутри чертежа.
_MIN_FRAME_SIDE = 100.0

# Меньше стольких подписей на листе — преобладающая высота шрифта случайна.
_MIN_TEXTS_FOR_HEIGHT = 5


# Высоты шрифта по ГОСТ 2.304. Рабочие в проектной документации — 2.5 и 3.5:
# ими набирают штамп и подписи, которых на листе большинство.
_GOST_HEIGHTS = (2.5, 3.5, 5.0, 7.0, 10.0, 14.0)
_WORKHORSE_HEIGHTS = (2.5, 3.5)


def _guess_format(
    width: float, height: float, text_heights: list[float] | None = None
) -> tuple[str, float]:
    """Формат и масштаб по размеру рамки в модели: (A4, 100) для 21000x29700.

    Чертёж в модели вычерчен в натуральную величину, а рамка увеличена во
    столько раз, во сколько уменьшен масштаб. Делим сторону рамки на сторону
    стандартного листа и смотрим, где получилось круглое число: масштабы
    бывают 1:20, 1:50, 1:100, но не 1:37.

    Одной геометрии мало. Форматы ISO кратны двум, поэтому рамка 21000x29700 —
    это и A4 в 1:100, и A2 в 1:50, и различить их размером нельзя. Различает
    шрифт: при верном масштабе высоты текста ложатся на ряд ГОСТ 2.304, причём
    большинство подписей набрано рабочими 2.5 или 3.5 мм. При вдвое неверном
    масштабе они превращаются в 5 и 7 — формально допустимые, но для сплошной
    подписи неправдоподобно крупные.
    """
    short, long = sorted((width, height))
    candidates: list[tuple[str, float]] = []
    for name, (w, h) in _ISO_SIZES.items():
        scale = long / h
        if scale <= 0:
            continue
        for guess in {round(scale), round(scale / 5) * 5, round(scale / 10) * 10}:
            if guess <= 0:
                continue
            if abs(scale - guess) / guess > 0.02:
                continue
            if abs(short / w - guess) / guess > 0.05:
                continue
            candidates.append((name, float(guess)))
    if not candidates:
        return "", 0.0
    if len(candidates) == 1 or not text_heights:
        return candidates[0]

    mode = max(set(round(h, 1) for h in text_heights), key=text_heights.count)

    def rank(item: tuple[str, float]) -> tuple[int, float]:
        mm = mode / item[1]
        nearest = min(_GOST_HEIGHTS, key=lambda g: abs(g - mm))
        # сначала те, где преобладает рабочая высота, потом по близости к ряду
        return (0 if nearest in _WORKHORSE_HEIGHTS else 1, abs(nearest - mm))

    return min(candidates, key=rank)


def _frames_in_model(msp) -> list[tuple[float, float, float, float]]:
    """Рамки листов, разложенных в пространстве модели.

    Так делают, когда листы не оформлены через layout: все чертежи комплекта
    вычерчены рядом друг с другом в модели, каждый в своей рамке. Опознаём
    рамку по соотношению сторон — у любого листа ISO это корень из двух, и
    ничего другого такой формы на чертеже не рисуют. Вложенные прямоугольники
    (внутренняя рамка поля чертежа) выбрасываем: лист один.
    """
    rects: list[tuple[float, float, float, float]] = []
    for e in msp:
        if e.dxftype() != "LWPOLYLINE" or not e.closed:
            continue
        try:
            pts = [(p[0], p[1]) for p in e.get_points()]
        except Exception:
            continue
        if len(pts) < 4:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if w < _MIN_FRAME_SIDE or h < _MIN_FRAME_SIDE:
            continue
        ratio = max(w, h) / min(w, h)
        if abs(ratio - _ISO_RATIO) / _ISO_RATIO > _RATIO_TOL:
            continue
        rects.append((min(xs), min(ys), max(xs), max(ys)))
    # выбрасываем вложенные
    outer: list[tuple[float, float, float, float]] = []
    for r in sorted(rects, key=lambda r: -(r[2] - r[0]) * (r[3] - r[1])):
        if any(
            o[0] <= r[0] and o[1] <= r[1] and o[2] >= r[2] and o[3] >= r[3]
            for o in outer
        ):
            continue
        outer.append(r)
    # порядок чтения комплекта: слева направо, сверху вниз
    outer.sort(key=lambda r: (-round(r[3], -2), r[0]))
    return outer


def _sheets_from_frames(msp, model_texts: list[TextItem]) -> list[Sheet]:
    """Листы, собранные по рамкам в модели, когда layout'ы пустые."""
    sheets: list[Sheet] = []
    for i, (x0, y0, x1, y1) in enumerate(_frames_in_model(msp), start=1):
        inside = [t for t in model_texts if x0 <= t.x <= x1 and y0 <= t.y <= y1]
        # На листе с парой подписей преобладающую высоту не определить —
        # берём её по всему файлу: рамки одного комплекта одного формата.
        sample = inside if len(inside) >= _MIN_TEXTS_FOR_HEIGHT else model_texts
        fmt, scale = _guess_format(x1 - x0, y1 - y0, [t.height for t in sample])
        sheet = Sheet(
            number=i,
            name=f"лист {i} (рамка в модели)",
            paper=fmt,
            paper_width=_ISO_SIZES.get(fmt, (0, 0))[0],
            paper_height=_ISO_SIZES.get(fmt, (0, 0))[1],
            scales=[scale] if scale else [],
            texts=inside,
            windows=[(x0, y0, x1, y1, scale)],
        )
        sheets.append(sheet)
    return sheets


def read_sheets(dxf_path: Path) -> list[Sheet]:
    """Листы чертежа с текстом, привязанным к каждому."""
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    model_texts: list[TextItem] = []
    _collect_from(doc.modelspace(), model_texts)

    sheets: list[Sheet] = []
    for name in doc.layouts.names():
        if name == "Model":
            continue
        layout = doc.layouts.get(name)
        d = layout.dxf_layout.dxf
        paper = str(d.get("paper_size", "") or "")
        number = None
        if m := re.match(r"\s*(\d+)", name):
            number = int(m.group(1))
        sheet = Sheet(
            number=number,
            name=name,
            paper=paper,
            paper_width=float(d.get("paper_width", 0) or 0),
            paper_height=float(d.get("paper_height", 0) or 0),
        )
        # Текст, лежащий на самом листе (рамка, штамп бывают и там).
        _collect_from(layout, sheet.texts)
        # Плюс всё, что попадает в окна вьюпортов.
        seen: set[tuple[float, float, str]] = set()
        for vp in layout.query("VIEWPORT"):
            window = _viewport_window(vp)
            if window is None:
                continue
            x0, y0, x1, y1, scale = window
            sheet.scales.append(round(scale, 2))
            sheet.windows.append((x0, y0, x1, y1, round(scale, 2)))
            for item in model_texts:
                if x0 <= item.x <= x1 and y0 <= item.y <= y1:
                    key = (round(item.x, 2), round(item.y, 2), item.text)
                    if key in seen:
                        continue  # врезки перекрываются, текст не дублируем
                    seen.add(key)
                    sheet.texts.append(item)
        sheets.append(sheet)

    # Layout'ы бывают пустыми: комплект вычерчен прямо в модели, листы стоят
    # рядом каждый в своей рамке. Тогда листы ищем по рамкам.
    if not any(s.texts for s in sheets):
        by_frames = _sheets_from_frames(doc.modelspace(), model_texts)
        if by_frames:
            return by_frames
    sheets.sort(key=lambda s: (s.number is None, s.number or 0, s.name))
    return sheets


def reading_order(texts: list[TextItem]) -> list[str]:
    """Текст листа строками, сверху вниз и слева направо.

    Чертёж — не поток слов: подписи разбросаны по полю, и без склейки в строки
    вывод превращается в кашу из обрывков. Строку собираем по близости Y с
    допуском от высоты самого текста: у штампа она 2.5 мм, у заголовка 10.
    """
    if not texts:
        return []
    items = sorted(texts, key=lambda t: (-t.y, t.x))
    lines: list[list[TextItem]] = []
    for item in items:
        tol = max(item.height, 1.0) * LINE_TOL_RATIO
        if lines and abs(lines[-1][0].y - item.y) <= tol:
            lines[-1].append(item)
        else:
            lines.append([item])
    out: list[str] = []
    for line in lines:
        line.sort(key=lambda t: t.x)
        joined = " ".join(t.text.replace("\n", " ").strip() for t in line)
        joined = re.sub(r"\s{2,}", " ", joined).strip()
        if joined:
            out.append(joined)
    return out


# Код единиц чертежа ($INSUNITS) → сколько это метров. Спрашиваем у файла, а
# не выводим по размеру рамки: комплект «Жуковский» вычерчен в миллиметрах, а
# стройгенплан ПОС из того же комплекта — в метрах, и длины отличались бы в
# тысячу раз.
_UNIT_TO_M = {1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0, 9: 1e-6, 14: 0.1, 15: 10.0, 16: 1000.0}

# Слой, покрывающий столько от листа по обеим осям, описываем как «по всему
# листу»: точнее сказать нечего, а перечислять все стороны света бессмысленно.
_WHOLE_SHEET = 0.7

# Автоматические имена блоков AutoCAD: «*U258», «A$C0df934f8». Инженеру они не
# говорят ничего, в сводку оборудования не идут.
_ANON_BLOCK_RE = re.compile(r"^(?:\*[A-Za-z]\d+|A\$C[0-9A-Fa-f]+)$")


@dataclass
class LayerStat:
    """Что даёт один слой листа: сколько объектов, какой длины, где лежит."""

    name: str
    entities: int
    length_m: float
    zone: str


def _points_of(e) -> list[tuple[float, float]]:
    """Опорные точки объекта — для длины и габаритов."""
    kind = e.dxftype()
    try:
        if kind == "LINE":
            return [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
        if kind == "LWPOLYLINE":
            return [(p[0], p[1]) for p in e.get_points()]
        if kind == "POLYLINE":
            return [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
        if kind in ("CIRCLE", "ARC"):
            c, r = e.dxf.center, float(e.dxf.radius)
            return [(c.x - r, c.y - r), (c.x + r, c.y + r)]
        if kind in ("TEXT", "MTEXT", "INSERT", "ATTRIB"):
            p = e.dxf.insert
            return [(p.x, p.y)]
    except Exception:
        return []
    return []


def _length_of(e) -> float:
    """Длина объекта в единицах чертежа. Не линия — ноль."""
    kind = e.dxftype()
    try:
        if kind == "CIRCLE":
            return 2 * 3.141592653589793 * float(e.dxf.radius)
        if kind == "ARC":
            start, end = float(e.dxf.start_angle), float(e.dxf.end_angle)
            sweep = (end - start) % 360
            return 3.141592653589793 * float(e.dxf.radius) * sweep / 180
        pts = _points_of(e) if kind in ("LINE", "LWPOLYLINE", "POLYLINE") else []
        return sum(
            ((pts[i + 1][0] - pts[i][0]) ** 2 + (pts[i + 1][1] - pts[i][1]) ** 2) ** 0.5
            for i in range(len(pts) - 1)
        )
    except Exception:
        return 0.0


def _zone(box: tuple[float, float, float, float], window: tuple) -> str:
    """Где объект лежит на листе, словами. Тот же словарь, что у описаний VLM."""
    wx0, wy0, wx1, wy1 = window[:4]
    ww, wh = max(wx1 - wx0, 1e-9), max(wy1 - wy0, 1e-9)
    if (box[2] - box[0]) / ww >= _WHOLE_SHEET and (box[3] - box[1]) / wh >= _WHOLE_SHEET:
        return "по всему листу"
    cx = ((box[0] + box[2]) / 2 - wx0) / ww
    cy = ((box[1] + box[3]) / 2 - wy0) / wh
    vertical = "юг" if cy < 1 / 3 else ("север" if cy > 2 / 3 else "")
    horizontal = "запад" if cx < 1 / 3 else ("восток" if cx > 2 / 3 else "")
    if vertical and horizontal:
        return f"{vertical}о-{horizontal}" if vertical == "север" else f"{vertical}о-{horizontal}"
    return vertical or horizontal or "центр"


def _geometry_entities(space, depth: int = 0):
    """Объекты пространства с развёрнутыми блоками.

    Без разворота считается только верхний уровень, а у чертежа половина
    геометрии лежит внутри вставок: арматура, узлы, оборудование. На листе 22
    комплекта КР1 это давало 40 объектов слоя ARMATURA вместо сотен.
    """
    for e in space:
        yield e
        if e.dxftype() == "INSERT" and depth < 2:
            try:
                yield from _geometry_entities(e.virtual_entities(), depth + 1)
            except Exception:
                continue


def _texts_box(sheet: Sheet) -> tuple[float, float, float, float] | None:
    """Габариты подписей листа — запасное окно, когда вьюпорт его не дал."""
    if not sheet.texts:
        return None
    xs = [t.x for t in sheet.texts]
    ys = [t.y for t in sheet.texts]
    pad = max(max(xs) - min(xs), max(ys) - min(ys)) * 0.05 + 1.0
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def analyse_sheet(msp, sheet: Sheet, unit_m: float) -> tuple[list[LayerStat], Counter]:
    """Разбирает геометрию листа: слои с длинами и вставленное оборудование."""
    # Сначала окно основного вида. Если в нём не нашлось ничего — габариты
    # подписей: у стройгенплана ПОС вьюпорт указывает мимо (чертёж вычерчен в
    # метрах, а окно посчитано как для миллиметров), и по нему лист пуст.
    main = sheet.main_window()
    boxes = [b for b in (main[:4] if main else None, _texts_box(sheet)) if b]
    for attempt, box in enumerate(boxes):
        stats, blocks = _analyse_box(msp, box, unit_m)
        if stats:
            return stats, blocks
    return [], Counter()


def _analyse_box(msp, box, unit_m: float) -> tuple[list[LayerStat], Counter]:
    """Разбор геометрии в заданном прямоугольнике модели."""
    x0, y0, x1, y1 = box
    window = (x0, y0, x1, y1, 0.0)
    per_layer: dict[str, list] = {}
    blocks: Counter = Counter()
    for e in _geometry_entities(msp):
        pts = _points_of(e)
        if not pts:
            continue
        bx0 = min(p[0] for p in pts)
        bx1 = max(p[0] for p in pts)
        by0 = min(p[1] for p in pts)
        by1 = max(p[1] for p in pts)
        # объект относится к листу, если его центр в окне
        if not (x0 <= (bx0 + bx1) / 2 <= x1 and y0 <= (by0 + by1) / 2 <= y1):
            continue
        if e.dxftype() == "INSERT":
            name = str(e.dxf.name)
            if not _ANON_BLOCK_RE.match(name):
                blocks[name] += 1
        layer = str(getattr(e.dxf, "layer", "0"))
        slot = per_layer.setdefault(layer, [0, 0.0, [1e18, 1e18, -1e18, -1e18]])
        slot[0] += 1
        slot[1] += _length_of(e)
        box = slot[2]
        box[0], box[1] = min(box[0], bx0), min(box[1], by0)
        box[2], box[3] = max(box[2], bx1), max(box[3], by1)

    stats = [
        LayerStat(name, cnt, round(length * unit_m, 1), _zone(tuple(box), window))
        for name, (cnt, length, box) in per_layer.items()
    ]
    stats.sort(key=lambda s: (-s.length_m, -s.entities))
    return stats, blocks


def sheet_markdown(
    sheet: Sheet,
    file_name: str,
    layers: list[LayerStat] | None = None,
    blocks: Counter | None = None,
) -> str:
    """Лист в том же контракте, что и страница PDF: ## Страница N / ### PASS-*."""
    number = sheet.number if sheet.number is not None else 0
    scale = f"1:{sheet.scale():g}" if sheet.scale() else ""
    parts = [
        f"## Страница {number}",
        "",
        "### PASS-0 Паспорт листа",
        "",
        f"- источник: `{file_name}` (DWG)",
        f"- название листа: {sheet.title}",
        f"- формат: {sheet.paper or '—'}"
        + (
            f" ({sheet.paper_width:g}×{sheet.paper_height:g} мм)"
            if sheet.paper_width
            else ""
        ),
        f"- масштаб: {scale or '—'}",
        f"- текстовых объектов: {len(sheet.texts)}",
        "",
    ]
    if layers:
        parts += [
            "### PASS-A Состав листа (из геометрии)",
            "",
            "_Собрано из данных чертежа: принадлежность линий к слоям, их "
            "длины и габариты. Модель не вызывалась._",
            "",
            "| Слой | Объектов | Длина, м | Где на листе |",
            "|---|---:|---:|---|",
        ]
        for st in layers[:20]:
            shown = f"{st.length_m:g}" if st.length_m >= 0.1 else "—"
            parts.append(f"| {st.name} | {st.entities} | {shown} | {st.zone} |")
        parts.append("")
    if blocks:
        named = ", ".join(f"{n} ×{c}" for n, c in blocks.most_common(15))
        parts += ["**Вставленные элементы:** " + named, ""]
    parts += [
        "### PASS-B Текст листа (из DWG)",
        "",
        "_Текст взят из чертежа как данные: модель не вызывалась._",
        "",
    ]
    parts.extend(reading_order(sheet.texts))
    return "\n".join(parts).rstrip() + "\n"


def build_markdown(path: Path, sheets: list[Sheet], dxf_path: Path) -> str:
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()
    unit_code = int(doc.header.get("$INSUNITS", 0) or 0)
    unit_m = _UNIT_TO_M.get(unit_code, 0.001)
    unit_name = "м" if unit_m == 1.0 else ("мм" if unit_m == 0.001 else str(unit_m))
    head = (
        f"# {path.stem}\n\n"
        f"Источник — DWG (векторные данные, не отрисовка). Листов: {len(sheets)}. "
        f"Единицы чертежа: {unit_name}.\n"
    )
    chunks = []
    for sheet in sheets:
        layers, blocks = analyse_sheet(msp, sheet, unit_m)
        chunks.append(sheet_markdown(sheet, path.name, layers, blocks))
    return head + "\n" + "\n\n".join(chunks)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", help="DWG или DXF")
    ap.add_argument("-o", "--out", help="куда записать markdown")
    ap.add_argument("--sheet", type=int, help="только лист с этим номером")
    ap.add_argument("--keep-dxf", help="куда положить промежуточный DXF")
    args = ap.parse_args()

    src = Path(args.file)
    dxf = to_dxf(src, Path(args.keep_dxf) if args.keep_dxf else None)
    sheets = read_sheets(dxf)
    if args.sheet is not None:
        sheets = [s for s in sheets if s.number == args.sheet]
        if not sheets:
            print(f"Листа {args.sheet} в файле нет")
            return 1

    if args.out:
        Path(args.out).write_text(build_markdown(src, sheets, dxf), encoding="utf-8")
        print(f"записано: {args.out} ({len(sheets)} листов)")
        return 0

    print(f"{src.name}: листов {len(sheets)}")
    for s in sheets:
        scale = f"1:{s.scale():g}" if s.scale() else "—"
        print(
            f"  {str(s.number or '?'):>3}  {s.title[:44]:<44} "
            f"{s.paper_width:g}×{s.paper_height:g} мм  {scale:>7}  "
            f"текстов {len(s.texts)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
