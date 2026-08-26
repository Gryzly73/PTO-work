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

import math
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
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
    # Поворот подписи в градусах. В штампе «Взам. инв. №» и «Инв. № подл.»
    # стоят вертикально, и без угла интерфейс нарисовал бы их поперёк рамки.
    rotation: float = 0.0
    # Ширина текстового блока в единицах чертежа (у MTEXT). По ней подпись
    # переносится по строкам; без неё длинное примечание рисуется одной
    # строкой и уезжает за рамку листа.
    width: float = 0.0
    # Каким углом подпись прижата к своей точке. Без этого выровненный по
    # центру текст рисуется от неё вправо и наезжает на соседний.
    anchor: str = "left"
    valign: str = "baseline"


@dataclass
class SheetView:
    """Окно вида: кусок модели, показанный в прямоугольнике на листе.

    Держит всё, что нужно, чтобы перевести координаты модели в координаты
    листа. Без этого перевода лист собирается в двух системах сразу: рамка и
    штамп лежат в миллиметрах бумаги (0…1783), а подписи с генплана — в
    мировых координатах площадки (2 226 000). Разница в два миллиона, и лист
    выглядит пустым: и то и другое сжимается в точку по разным углам.
    """

    world: tuple[float, float, float, float]  # окно в координатах модели
    paper_center: tuple[float, float]  # центр окна на листе, мм
    paper_size: tuple[float, float]  # размер окна на листе, мм
    scale: float  # во сколько раз модель крупнее бумаги
    twist: float  # разворот вида, радианы

    def to_paper(self, x: float, y: float) -> tuple[float, float]:
        """Точку модели — в координаты листа."""
        cx = (self.world[0] + self.world[2]) / 2
        cy = (self.world[1] + self.world[3]) / 2
        dx, dy = x - cx, y - cy
        cos_a, sin_a = math.cos(-self.twist), math.sin(-self.twist)
        rx = dx * cos_a - dy * sin_a
        ry = dx * sin_a + dy * cos_a
        return (
            self.paper_center[0] + rx / self.scale,
            self.paper_center[1] + ry / self.scale,
        )

    def holds(self, x: float, y: float) -> bool:
        """Точка модели попадает в это окно."""
        return (
            self.world[0] <= x <= self.world[2]
            and self.world[1] <= y <= self.world[3]
        )


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
    # Окна вида с полным преобразованием «модель → лист». Пусты у листов,
    # найденных по рамкам: там чертёж и рамка уже в одной системе.
    views: list[SheetView] = field(default_factory=list)
    # Имя layout'а, если лист им и является: по нему берут рамку и штамп,
    # нарисованные на самой бумаге.
    layout_name: str = ""
    # Имя блока, из которого собран лист. Заполняется у таблиц, потерявших
    # привязку: их сетку надо искать не в модели, а внутри самого блока.
    source_block: str = ""
    # Чем разбор листа оказался неполон — это идёт в паспорт, чтобы инженер
    # видел разницу между «на листе ничего нет» и «мы не смогли прочитать».
    note: str = ""
    # Лист не пережил конвертацию: в файле от него не осталось ни одного
    # объекта. Отличается от «на листе нет текста» — там есть хотя бы линии.
    lost: bool = False
    # Лист пуст и в самом чертеже: комплект вычерчен в модели, а layout'ы
    # оставлены пустыми заготовками. Не потеря — сверено прямым чтением DWG.
    blank: bool = False
    # Служебный список подписей без координат: рисовать нечего, всё легло бы в
    # одну точку. Ставится только там, где координаты и правда неизвестны.
    flat: bool = False
    # Таблицы, потерявшие вставку, но опознанные как принадлежащие этому листу:
    # (имя блока, подписи). Место на листе неизвестно, поэтому в геометрию они
    # не идут — только в текст листа. Подробности — в `attach_tables()`.
    attached: list = field(default_factory=list)
    # Заметка листа говорит о находке, а не о недостаче: тогда в паспорте она
    # идёт как «восстановлено», а не «разбор листа неполон».
    recovered: bool = False
    # Сколько подписей пришло из пространства модели через окна вьюпортов.
    # Ноль при непустом чертеже означает, что окно настроено мимо, — и это
    # надо отличать от «на листе просто нет текста»: собственный штамп в
    # layout'е есть почти всегда, и по `texts` промах не виден.
    from_model: int = 0

    def extent(self) -> tuple[float, float, float, float] | None:
        """Охват листа — в той же системе координат, что и его подписи.

        Смешивать окна вида с подписями нельзя. У листа, собранного из
        layout'а, подписи уже пересчитаны в миллиметры бумаги, а окна остались
        в мировых координатах площадки: у листа ИГР это −3 062…−1 821 против
        −624 118…−433 116. По такому охвату все 1 248 подписей оказывались «в
        северо-восточном углу», и карта листа врала.
        """
        if self.views and self.texts:
            xs = [item.x for item in self.texts]
            ys = [item.y for item in self.texts]
            return min(xs), min(ys), max(xs), max(ys)
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


# Сколько раз повторить конвертацию, если её результат не читается. Выход
# dwg2dxf НЕ детерминирован: восемь прогонов на одном и том же DWG дают восемь
# разных файлов (размер гуляет на килобайты — в секцию материалов попадает
# мусор из неинициализированной памяти), и примерно каждый восьмой оказывается
# структурно битым. Ретрай стоит секунды и убирает эту лотерею: три неудачи
# подряд — это уже не случайность, а свойство файла.
_CONVERT_ATTEMPTS = 3


def to_dxf(path: Path, out_dir: Path | None = None) -> Path:
    """DWG → DXF, готовый к чтению. Если на вход уже DXF, конвертация не нужна.

    В обоих случаях файл проходит через `merge_split_text()`: разрезанные
    строки чинятся и в том DXF, который прислали готовым.
    """
    if path.suffix.lower() == ".dxf":
        return merge_split_text(path)
    converter = find_converter()
    if converter is None:
        raise RuntimeError(
            "Не найден конвертер DWG → DXF. Поставьте LibreDWG (dwg2dxf) или "
            "ODA File Converter и укажите путь в PTO_DWG2DXF."
        )
    out_dir = out_dir or Path(tempfile.mkdtemp(prefix="dwg2dxf_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / (path.stem + ".dxf")
    last_error = ""
    for attempt in range(1, _CONVERT_ATTEMPTS + 1):
        # dwg2dxf молчалив в успехе и болтлив в предупреждениях: часть объектов
        # он пропускает («Unknown object»). Это не отказ — файл читаемый, но
        # полноту стоит сверять с ODA, когда есть с чем сравнить.
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
        try:
            _check_readable(target)
        except Exception as error:
            # Битый выход — не повод сдаваться: следующая попытка даёт другой
            # файл. Мусор удаляем, иначе merge_split_text подберёт его по кэшу.
            last_error = f"{type(error).__name__}: {error}"
            merged = target.with_suffix(".merged.dxf")
            for junk in (target, merged):
                junk.unlink(missing_ok=True)
            if attempt == _CONVERT_ATTEMPTS:
                raise RuntimeError(
                    f"Конвертер {_CONVERT_ATTEMPTS} раза подряд выдал нечитаемый "
                    f"DXF для {path.name}: {last_error}"
                ) from error
            continue
        return merge_split_text(target)
    raise RuntimeError(f"Не удалось получить читаемый DXF для {path.name}")


def _check_readable(dxf_path: Path) -> None:
    """Быстрая проверка структуры DXF: коды групп идут через строку и числовые.

    Полное чтение через ezdxf здесь не годится: разбор повторится сразу после
    конвертации, а на ПОС это 42 МБ и десятки секунд. Скан по строкам ловит
    ровно тот класс поломок, которым портит файл конвертер, — сбитую пару
    «код / значение», на которой ezdxf и падает с DXFStructureError.
    """
    with dxf_path.open("rb") as handle:
        for number, line in enumerate(handle):
            if number % 2:
                continue  # нечётная строка — значение, оно любое
            code = line.strip()
            if not code.isdigit() and not (
                code[:1] in (b"-", b"+") and code[1:].isdigit()
            ):
                raise ValueError(
                    f"строка {number + 1}: код группы не число ({code[:40]!r})"
                )


# Сущности, у которых длинный текст разложен по нескольким тегам: 3 —
# продолжения, 1 — последний кусок. У ATTRIB и TEXT ezdxf выбрасывает всё,
# кроме последнего куска, и от подписи остаётся хвост.
#
# MTEXT ezdxf склеивает сам, но склеивает УЖЕ ДЕКОДИРОВАННЫЕ куски, а режет их
# конвертер по границе 255 БАЙТ — прямо посередине кириллической буквы. Её
# половинки при декодировании теряются, и в тексте появляется «инженрно-
# технического» вместо «инженерно-технического». Склейка на байтах собирает
# букву обратно, поэтому MTEXT здесь тоже.
_SPLIT_TEXT_ENTITIES = (b"ATTRIB", b"ATTDEF", b"TEXT", b"MTEXT")


def merge_split_text(dxf_path: Path) -> Path:
    """Склеивает разрезанные строки в DXF и возвращает путь к исправленному.

    Зачем. В штампе комплекта «Жуковский» название объекта — 175 символов, а в
    DXF строка режется по границе 255 БАЙТ: начало уходит в тег 3, остаток — в
    тег 1. ezdxf отдаёт для ATTRIB только тег 1, и в вывод попадало «ковская
    область, городской округ Жуковский» вместо полного названия — то есть
    ровно та строка, по которой ПТО опознаёт объект.

    Режется по байтам, а не по символам, поэтому кириллическая буква на стыке
    разрывается пополам и превращается в пару одиноких суррогатов. Склейка
    делается на байтах — тогда буква собирается обратно сама.

    Файл переписывается рядом (суффикс `.merged.dxf`); если склеивать нечего,
    возвращается исходный путь.
    """
    if dxf_path.name.endswith(".merged.dxf"):
        return dxf_path  # уже склеенный — второй раз не переписываем
    ready = dxf_path.with_suffix(".merged.dxf")
    if ready.exists() and ready.stat().st_mtime >= dxf_path.stat().st_mtime:
        return ready  # склейка ПОС на 42 МБ стоит секунды, повторять её незачем

    data = dxf_path.read_bytes()
    if b"\n  3\n" not in data and b"\r\n  3\r\n" not in data:
        return dxf_path

    lines = data.splitlines(keepends=True)
    out: list[bytes] = []
    entity = b""
    pending: list[bytes] = []
    merged = 0
    index = 0
    while index + 1 < len(lines):
        code_line, value_line = lines[index], lines[index + 1]
        code = code_line.strip()
        if code == b"0":
            entity = value_line.strip()
            pending.clear()
        if code == b"3" and entity in _SPLIT_TEXT_ENTITIES:
            pending.append(value_line.rstrip(b"\r\n"))
            index += 2
            continue
        if pending:
            if code == b"1":
                value = b"".join(pending) + value_line.rstrip(b"\r\n")
                ending = value_line[len(value_line.rstrip(b"\r\n")):]
                out.append(code_line)
                out.append(value + ending)
                merged += 1
                pending.clear()
                index += 2
                continue
            # Тег 3 был не продолжением строки — возвращаем как было.
            for chunk in pending:
                out.append(b"  3\n")
                out.append(chunk + b"\n")
            pending.clear()
        out.append(code_line)
        out.append(value_line)
        index += 2
    out.extend(lines[index:])
    if not merged:
        return dxf_path

    ready.write_bytes(b"".join(out))
    return ready


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


# Юникод, записанный escape-последовательностью AutoCAD: «\U+041E» — это «О»,
# «\M+1041E» — то же самое через кодовую страницу. Без раскрытия в вывод едет
# «1-1 (\U+041E\U+043F\U+0430…)» вместо «1-1 (Опалубка)»: на фундаментах КР1
# таких мест 36, на стройгенплане ПОС — 434.
_UNI_ESCAPE = re.compile(r"\\U\+([0-9A-Fa-f]{4})|\\M\+[0-9A-Fa-f]([0-9A-Fa-f]{4})")


def _unescape_unicode(text: str) -> str:
    if r"\U+" not in text and r"\M+" not in text:
        return text

    def repl(match: re.Match) -> str:
        code = match.group(1) or match.group(2)
        try:
            return chr(int(code, 16))
        except ValueError:
            return match.group(0)

    return _UNI_ESCAPE.sub(repl, text)


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
    text = _unescape_unicode("".join(out))
    for code, repl in _ACAD_CODES:
        if code in text:
            text = text.replace(code, repl)
    return text


# Заглушка невычисленного поля AutoCAD: две решётки и больше, ничего кроме.
_FIELD_STUB = re.compile(r"#{2,}")


def _plain(entity) -> str:
    """Текст сущности без разметки MTEXT.

    «####» отбрасываем: так AutoCAD показывает поле, значение которого не
    вычислено, а конвертер поля не переносит вовсе («copy process ignored
    FIELD»). В штампе ПЗУ таких заглушек 172 штуки — в выводе они выглядели бы
    строкой «Разраб. Терещенко #### #### ####».
    """
    try:
        if entity.dxftype() == "MTEXT":
            text = clean_text(entity.plain_text())
        else:
            text = clean_text(entity.dxf.text)
    except Exception:
        return ""
    return "" if _FIELD_STUB.fullmatch(text.strip()) else text


def _plain_direct(value: str) -> str:
    """То же для строки, прочитанной прямо из DWG.

    Через ezdxf её не пропустить — сущности там нет, есть только строка, а
    разметка MTEXT в ней та же самая: `{\\fISOCPEUR|b1;Раздел 1.0}` без
    очистки попал бы в вывод целиком.
    """
    from ezdxf.tools.text import plain_mtext

    try:
        text = clean_text(plain_mtext(value or ""))
    except Exception:
        text = clean_text(value or "")
    return "" if _FIELD_STUB.fullmatch(text.strip()) else text


# Выравнивание текста в DXF. Точка вставки у выровненной подписи лежит НЕ в
# группе 10 (insert), а в группе 11 (align_point) — это правило формата, и без
# него подписи уезжают: на листе фундаментов таких 336 штук, часть из них
# сходится в одну точку и наезжает друг на друга.
_HALIGN = {0: "left", 1: "center", 2: "right", 3: "left", 4: "center", 5: "left"}
_VALIGN = {0: "baseline", 1: "bottom", 2: "middle", 3: "top"}

# У MTEXT точка одна, но attachment_point говорит, каким углом текст к ней
# прижат: 1 — левый верхний, 5 — центр, 9 — правый нижний.
_MTEXT_ANCHOR = {
    1: ("left", "top"), 2: ("center", "top"), 3: ("right", "top"),
    4: ("left", "middle"), 5: ("center", "middle"), 6: ("right", "middle"),
    7: ("left", "bottom"), 8: ("center", "bottom"), 9: ("right", "bottom"),
}


# Межстрочный интервал MTEXT по умолчанию: полтора кегля с небольшим — так
# AutoCAD расставляет строки, и по нему же считается отступ пустых строк.
_LINE_SPACING = 1.5


def _leading_blanks(entity) -> int:
    """Сколько пустых строк стоит в начале блока MTEXT."""
    try:
        plain = entity.plain_text()
    except Exception:
        return 0
    if not plain:
        return 0
    stripped = plain.lstrip(chr(13) + chr(10))
    return plain[: len(plain) - len(stripped)].count(chr(10))


def _placement(entity) -> tuple[float, float, str, str]:
    """Где на самом деле стоит подпись: (x, y, привязка по X, привязка по Y)."""
    kind = entity.dxftype()
    try:
        if kind == "MTEXT":
            point = entity.dxf.insert
            anchor, valign = _MTEXT_ANCHOR.get(
                int(entity.dxf.get("attachment_point", 1) or 1), ("left", "top")
            )
            # Пустые строки в начале блока — это вертикальный отступ, которым
            # чертёжник опускает подпись. На титульном листе ПЗ у блока
            # «Генеральный директор» их 31: без поправки текст встаёт наверх и
            # ложится поверх блока «Заказчик», у которого та же точка вставки.
            # Сам текст очищается, поэтому отступ переносим в координату.
            shift = _leading_blanks(entity) * _text_height(entity) * _LINE_SPACING
            return point.x, point.y - shift, anchor, valign
        halign = int(entity.dxf.get("halign", 0) or 0)
        valign = int(entity.dxf.get("valign", 0) or 0)
        point = entity.dxf.insert
        if halign or valign:
            aligned = entity.dxf.get("align_point", None)
            if aligned is not None:
                point = aligned
        return (
            point.x,
            point.y,
            _HALIGN.get(halign, "left"),
            _VALIGN.get(valign, "baseline"),
        )
    except Exception:
        try:
            point = entity.dxf.insert
            return point.x, point.y, "left", "baseline"
        except Exception:
            return 0.0, 0.0, "left", "baseline"


def _rotation(entity) -> float:
    """Угол поворота подписи в градусах. 0, если его нет."""
    for attr in ("rotation", "text_direction", "char_height"):
        if attr != "rotation":
            continue
        try:
            return float(entity.dxf.get("rotation", 0.0) or 0.0) % 360
        except Exception:
            return 0.0
    return 0.0


def _text_width(entity) -> float:
    """Ширина текстового блока MTEXT в единицах чертежа. 0 — не задана."""
    try:
        return float(entity.dxf.get("width", 0.0) or 0.0)
    except Exception:
        return 0.0


def _text_height(entity) -> float:
    for attr in ("char_height", "height"):
        try:
            value = float(entity.dxf.get(attr))
            if value > 0:
                return value
        except Exception:
            continue
    return 2.5


# Слои, выключенные или замороженные в самом чертеже. Их содержимое на печать
# не идёт, и в расшифровку ему тоже не место.
#
# Без этого на листе ПЗУ «2 СПОЗУ» ложилось поверх наименования листа: в штампе
# два атрибута в одной точке, и один из них — на слое
# «ШТАМП_название-текущего-листа», который в файле отключён. В комплекте таких
# слоёв 45 из 450, так что мусор был не единичный.
_HIDDEN_LAYERS: dict[int, frozenset] = {}


def hidden_layers(doc) -> frozenset:
    """Имена слоёв, которые в чертеже не показываются. Считается раз на документ."""
    key = id(doc)
    cached = _HIDDEN_LAYERS.get(key)
    if cached is None:
        names = set()
        try:
            for layer in doc.layers:
                try:
                    if layer.is_off() or layer.is_frozen():
                        names.add(layer.dxf.name)
                except Exception:
                    continue
        except Exception:
            pass
        cached = frozenset(names)
        if len(_HIDDEN_LAYERS) > 8:
            _HIDDEN_LAYERS.clear()
        _HIDDEN_LAYERS[key] = cached
    return cached


def _is_hidden(entity, hidden: frozenset) -> bool:
    """Объект не показывается: невидим сам или лежит на выключенном слое.

    Флаг невидимости приходится проверять отдельно от слоёв. В блоке «Штамп
    КРП Меридиан» лежат заготовки на все случаи — четыре фамилии в одной
    точке, помеченные невидимыми, и одна настоящая рядом. Без проверки флага
    все пять рисуются друг на друге, и графа «Разраб.» превращается в кляксу.
    """
    try:
        if int(entity.dxf.get("invisible", 0) or 0):
            return True
    except Exception:
        pass
    if not hidden:
        return False
    try:
        return entity.dxf.layer in hidden
    except Exception:
        return False


def _collect_from(
    space,
    out: list[TextItem],
    depth: int = 0,
    hidden: frozenset = frozenset(),
    skipped: list | None = None,
) -> None:
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
        if _is_hidden(e, hidden):
            # Текст с невидимого слоя не выводим, но помним: иначе сверка с
            # исходным DWG сочтёт его потерей и вернёт обратно.
            if skipped is not None:
                try:
                    value = _plain(e).strip()
                    if value:
                        x, y, anchor, valign = _placement(e)
                        skipped.append(
                            TextItem(x, y, _text_height(e), value, "hidden",
                                     _rotation(e), _text_width(e), anchor, valign)
                        )
                except Exception:
                    pass
            continue
        try:
            if kind in ("MTEXT", "TEXT"):
                text = _plain(e).strip()
                if text:
                    x, y, anchor, valign = _placement(e)
                    out.append(
                        TextItem(
                            x,
                            y,
                            _text_height(e),
                            text,
                            kind.lower(),
                            _rotation(e),
                            _text_width(e),
                            anchor,
                            valign,
                        )
                    )
            elif kind == "ATTRIB":
                text = _plain(e).strip()
                if text:
                    x, y, anchor, valign = _placement(e)
                    out.append(
                        TextItem(
                            x,
                            y,
                            _text_height(e),
                            text,
                            "attrib",
                            _rotation(e),
                            _text_width(e),
                            anchor,
                            valign,
                        )
                    )
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
                    if _is_hidden(attrib, hidden):
                        # Атрибут на отключённом слое на печать не идёт.
                        if skipped is not None:
                            try:
                                value = _plain(attrib).strip()
                                if value:
                                    x, y, anchor, valign = _placement(attrib)
                                    skipped.append(
                                        TextItem(x, y, _text_height(attrib), value,
                                                 "hidden", _rotation(attrib),
                                                 _text_width(attrib), anchor, valign)
                                    )
                            except Exception:
                                pass
                        continue
                    text = _plain(attrib).strip()
                    if text:
                        x, y, anchor, valign = _placement(attrib)
                        out.append(
                            TextItem(
                                x,
                                y,
                                _text_height(attrib),
                                text,
                                "attrib",
                                _rotation(attrib),
                                _text_width(attrib),
                                anchor,
                                valign,
                            )
                        )
                _collect_from(e.virtual_entities(), out, depth + 1, hidden, skipped)
        except Exception:
            # Один битый объект не должен ронять разбор листа.
            continue


def _viewport_window(vp):
    """Окно вьюпорта: (x0, y0, x1, y1, масштаб, преобразование в лист).

    Масштаб — во сколько раз модель крупнее бумаги: высота вида, делённая на
    высоту окна на бумаге. Для листа 1:40 это ровно 40.

    Где на самом деле центр вида. Поле `view_center_point` задано НЕ в мировых
    координатах, а в системе координат самого вида, и у площадочных чертежей
    это разные вещи: у ПЗУ комплекта «Жуковский» центр по этому полю выходит
    около x=20 000, а сам генплан вычерчен около x=2 226 000 — вид уезжал на
    два миллиона единиц, и в лист не попадало НИ ОДНОЙ подписи из модели.
    Правильная точка — «цель вида» (`view_target_point`, она в мировых) плюс
    смещение центра, повёрнутое на угол разворота вида. После поправки на
    листы ПЗУ легло 3, 28, 26 и 33 подписи; там, где всё работало и раньше
    (КР1), числа не изменились.
    """
    try:
        if int(vp.dxf.get("id", 0)) == PAPER_VIEWPORT_ID:
            return None
        center = vp.dxf.view_center_point
        target = vp.dxf.get("view_target_point", None)
        twist = math.radians(float(vp.dxf.get("view_twist_angle", 0.0) or 0.0))
        view_h = float(vp.dxf.view_height)
        paper_w = float(vp.dxf.width)
        paper_h = float(vp.dxf.height)
    except Exception:
        return None
    if view_h <= 0 or paper_h <= 0:
        return None
    cos_a, sin_a = math.cos(twist), math.sin(twist)
    shift_x = center.x * cos_a - center.y * sin_a
    shift_y = center.x * sin_a + center.y * cos_a
    if target is not None:
        center_x = target.x + shift_x
        center_y = target.y + shift_y
    else:
        center_x, center_y = center.x, center.y
    view_w = view_h * (paper_w / paper_h)
    world = (
        center_x - view_w / 2,
        center_y - view_h / 2,
        center_x + view_w / 2,
        center_y + view_h / 2,
    )
    paper_center = vp.dxf.get("center", None)
    view = SheetView(
        world=world,
        paper_center=(paper_center.x, paper_center.y) if paper_center else (0.0, 0.0),
        paper_size=(paper_w, paper_h),
        scale=view_h / paper_h,
        twist=twist,
    )
    return (*world, view_h / paper_h, view)


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


# Анонимные блоки AutoCAD: «*T17», «*U258». В них он держит развёрнутое
# содержимое таблиц и динамических вставок. Обычно на такой блок ссылается
# сущность-таблица, и мы добираемся до текста через неё.
_ANON_BLOCK_PREFIXES = ("*T", "*U", "*X", "*D")

# Блоки, которые не бывают потерянной вставкой: пространства читаются
# напрямую, а «_None» AutoCAD держит как пустышку.
_SERVICE_BLOCKS = {"*model_space", "*paper_space", "_none"}


# Слова, которыми чертёжник делит длинную таблицу между листами. Порядок
# здесь — это и есть порядок листов: «(начало)» на первом, «(окончание)» на
# последнем.
_TABLE_PARTS = ("начало", "продолжение", "окончание")

# Корни этих слов и их порядок. По корню, а не по слову целиком: в чертежах
# встречаются «окочание», «продолж.» и прочие сокращения.
_PART_ROOTS = (("нача", 0), ("продолж", 1), ("оконч", 2), ("окоч", 2), ("оконьч", 2))


def _part_order(word: str) -> int | None:
    """Какая это часть таблицы: начало, продолжение, окончание. None — не часть."""
    low = " ".join((word or "").split()).lower()
    for root, order in _PART_ROOTS:
        if low.startswith(root):
            return order
    return None

# Строки, по которым видно, что подпись — не заголовок, а шапка таблицы.
_TABLE_HEADS = ("обозначение", "наименование", "примечание", "номер тома")


def _table_caption(texts: list) -> tuple[str, int] | None:
    """Заголовок таблицы: (имя без части, порядок части).

    Читаем верхнюю подпись блока. У комплекта «Жуковский» это «Содержание
    (начало)», «Состав проектной документации (окончание)» и так далее — то
    есть имя документа плюс указание, какая это часть.
    """
    ordered = sorted(texts, key=lambda item: -item.y)
    for item in ordered[:3]:
        text = " ".join((item.text or "").split())
        if not text or text.lower() in _TABLE_HEADS:
            continue
        # Слово в скобках пишут как придётся: у ТБЭ в чертеже стоит
        # «Содержание (окочание)» — с опечаткой. Ловим по корню, иначе такая
        # таблица не садится на лист из-за одной пропущенной буквы.
        match = re.match(r"^(.*?)\s*\(\s*([^)]{3,20}?)\s*\)\s*$", text)
        if match:
            part = _part_order(match.group(2))
            if part is not None:
                return match.group(1).strip(), part
        return text, 0
    return None


def attach_tables(sheets: list[Sheet], orphans: list[tuple[str, list]]):
    """Сажает потерянные таблицы на их листы. Возвращает то, что не село.

    Вставку таблицы конвертер теряет вместе с местом на листе, и содержимое
    приходилось отдавать отдельным листом в конце. Инженер при этом открывал
    лист «Содержание» и видел пустую рамку со штампом, а само содержание —
    где-то в конце документа. Это и читалось как «таблицы пропали».

    Место на листе восстановить неоткуда, а вот ЛИСТ — вполне: у таблицы есть
    заголовок («Содержание (продолжение)»), а у листа — основная надпись с
    наименованием и номером. Совпало наименование — значит это листы того
    документа; часть «продолжение» садится на второй лист по порядку номеров.

    Привязку делаем только там, где она однозначна: имя таблицы совпало с
    наименованием листа, а листов документа хватает на все её части.
    """
    if not orphans:
        return []
    import stamp as stamp_module

    # Листы, у которых прочитана основная надпись, разложенные по документам.
    documents: dict[str, list[tuple[int, Sheet, object]]] = {}
    for sheet in sheets:
        if sheet.source_block or not sheet.texts:
            continue
        try:
            mark = stamp_module.from_dwg_sheet(sheet)
        except Exception:
            continue
        if not mark.code:
            continue
        number = int(mark.sheet) if str(mark.sheet).isdigit() else 0
        documents.setdefault(mark.code, []).append((number, sheet, mark))
    for group in documents.values():
        group.sort(key=lambda row: row[0])

    # Таблицы одного документа — в порядке частей, а при равенстве в порядке
    # блоков: имена анонимных блоков конвертер выдаёт по порядку создания.
    by_name: dict[str, list[tuple[int, str, list]]] = {}
    unnamed: list[tuple[str, list]] = []
    for index, (name, texts) in enumerate(orphans):
        caption = _table_caption(texts)
        if caption is None:
            unnamed.append((name, texts))
            continue
        title, part = caption
        by_name.setdefault(_fold(title), []).append((part * 1000 + index, name, texts))

    # Когда настоящий лист в документе ОДИН, гадать не о чем: всё, что
    # потеряло вставку, принадлежит ему. Так на лист ИГР возвращаются штамп и
    # четыре геологических разреза — 1600 подписей, которые иначе висели бы
    # десятком отдельных листов.
    single = [rows[0][1] for rows in documents.values() if len(rows) == 1]
    if len(documents) == 1 and len(single) == 1:
        target_sheet = single[0]
        for name, texts in orphans:
            target_sheet.attached.append((name, texts))
        target_sheet.recovered = True
        target_sheet.note = (
            "содержимое листа собрано заново: конвертер потерял привязку "
            "блоков, а лист в документе один"
        )
        return []

    left: list[tuple[str, list]] = []
    for folded, group in by_name.items():
        group.sort(key=lambda row: row[0])
        target = None
        for rows in documents.values():
            # Наименование документа стоит в надписи первого листа: дальше
            # чертёжник пишет в этой графе «Сод2», «СП2» и подобное.
            if rows and _fold(rows[0][2].title) == folded:
                target = rows
                break
        if target is None:
            left += [(name, texts) for _, name, texts in group]
            continue
        if len(target) < len(group):
            # Частей больше, чем листов документа: сажаем по порядку сколько
            # есть, остаток отдаём отдельными листами. Лучше вернуть на место
            # две таблицы из трёх, чем ни одной.
            left += [(name, texts) for _, name, texts in group[len(target):]]
            group = group[: len(target)]
        for (_, name, texts), (_number, sheet, _mark) in zip(group, target):
            sheet.attached.append((name, texts))
            sheet.recovered = True
            sheet.note = (
                "таблица листа: конвертер потерял её привязку к листу, лист "
                "определён по заголовку таблицы и основной надписи"
            )
    return left + unnamed


def orphan_blocks(doc) -> list[tuple[str, list]]:
    """Блоки с текстом, на которые в файле никто не ссылается.

    Зачем это нужно. В файле ТБЭ состав проекта — это таблица AutoCAD; её
    содержимое конвертер перенёс в анонимные блоки «*T17» и «*T18», а саму
    вставку потерял. Формально блоки в файле есть, фактически их не видно
    ниоткуда — и лист «Состав проекта» приезжал пустым, хотя в исходнике на
    нём полсотни строк с шифрами разделов.

    Такой блок нельзя поставить на лист: без вставки неизвестно ни место, ни
    масштаб. Но потерять его тем более нельзя, поэтому содержимое отдаётся
    отдельно — как приложение к документу.
    """
    used: set[str] = set()
    spaces = [doc.modelspace()] + [
        layout for layout in doc.layouts if layout.name != "Model"
    ]
    spaces += [doc.blocks.get(block.name) for block in doc.blocks]
    for space in spaces:
        try:
            items = list(space)
        except Exception:
            continue
        for entity in items:
            if entity.dxftype() == "INSERT":
                try:
                    used.add(entity.dxf.name)
                except Exception:
                    continue

    hidden = hidden_layers(doc)
    found: list[tuple[str, list]] = []
    seen_content: set[str] = set()
    for block in doc.blocks:
        name = block.name
        if name in used:
            continue
        # Вставку конвертер теряет не только у анонимных блоков с таблицами, но
        # и у обычных: в файле ИГР без вставки остались «Штамп КРП» и
        # «ГП-СООРУЖЕНИЯ-БЛОК 4» — 910 подписей, включая фамилии из штампа и
        # номера скважин. Служебные блоки пространств пропускаем: их
        # содержимое читается напрямую.
        if name.startswith("*") and not name.startswith(_ANON_BLOCK_PREFIXES):
            continue
        if name.lower() in _SERVICE_BLOCKS:
            continue
        texts: list[TextItem] = []
        try:
            _collect_from(doc.blocks.get(name), texts, hidden=hidden)
        except Exception:
            continue
        if len(texts) < 5:
            continue  # пара подписей — это не потерянная таблица
        # Копии одной таблицы AutoCAD держит под разными именами: «*T17» и
        # «*T22» у ТБЭ совпадают знак в знак. Второй экземпляр не нужен.
        fingerprint = "|".join(sorted(t.text for t in texts))[:2000]
        if fingerprint in seen_content:
            continue
        seen_content.add(fingerprint)
        found.append((name, texts))
    return found


def _hidden_sheet(hidden_texts: list) -> "Sheet | None":
    """Лист с текстом, который в чертеже скрыт. None — если скрывать нечего."""
    if not hidden_texts:
        return None
    return Sheet(
        number=None,
        name="Текст с отключённых слоёв",
        paper="",
        paper_width=0.0,
        paper_height=0.0,
        texts=list(hidden_texts),
        flat=True,
        note=(
            f"подписей на отключённых слоях: {len(hidden_texts)}. В чертеже они "
            "скрыты и на печать не идут — приведены, чтобы ничего не потерялось"
        ),
    )


def read_sheets(dxf_path: Path, source: Path | None = None) -> list[Sheet]:
    """Листы чертежа с текстом, привязанным к каждому.

    `source` — исходный DWG, если он есть. По нему восстанавливаются листы,
    которые не пережили конвертацию: см. `recover_lost_sheets()`.
    """
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    model_texts: list[TextItem] = []
    hidden = hidden_layers(doc)
    # Текст с отключённых слоёв: в вывод не идёт, но нужен сверке со вторым
    # путём чтения — иначе он вернётся обратно как «потерянный».
    hidden_texts: list[TextItem] = []
    _collect_from(doc.modelspace(), model_texts, hidden=hidden, skipped=hidden_texts)

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
            layout_name=name,
            paper=paper,
            paper_width=float(d.get("paper_width", 0) or 0),
            paper_height=float(d.get("paper_height", 0) or 0),
        )
        # Текст, лежащий на самом листе (рамка, штамп бывают и там).
        _collect_from(layout, sheet.texts, hidden=hidden, skipped=hidden_texts)
        # Лист, у которого в файле нет вообще ничего, — это не «пустой лист
        # комплекта», а потеря: у ПЗУ так выглядят «ПЗМ» и «Озел. и МАФ» —
        # в DXF от них остались только границы блока, без рамки и без окна
        # вида, хотя в самом DWG листы не пустые. Молчать об этом нельзя:
        # инженер должен знать, что смотреть его надо в исходнике.
        if not any(True for _ in layout):
            sheet.lost = True
            sheet.note = (
                "после конвертации DWG → DXF лист пуст: в файле не осталось "
                "ни рамки, ни окна вида — смотрите исходный чертёж"
            )
        # Плюс всё, что попадает в окна вьюпортов.
        seen: set[tuple[float, float, str]] = set()
        for vp in layout.query("VIEWPORT"):
            window = _viewport_window(vp)
            if window is None:
                continue
            x0, y0, x1, y1, scale, view = window
            sheet.scales.append(round(scale, 2))
            sheet.windows.append((x0, y0, x1, y1, round(scale, 2)))
            sheet.views.append(view)
            for item in model_texts:
                if not view.holds(item.x, item.y):
                    continue
                key = (round(item.x, 2), round(item.y, 2), item.text)
                if key in seen:
                    continue  # врезки перекрываются, текст не дублируем
                seen.add(key)
                # Подпись из модели переносим в координаты листа — туда, где
                # инженер её и видит. Высоту делим на масштаб вида по той же
                # причине: 2.5 мм на бумаге при 1:500 — это 1250 единиц в
                # модели, и без пересчёта надпись накрыла бы весь лист.
                paper_x, paper_y = view.to_paper(item.x, item.y)
                sheet.texts.append(
                    replace(
                        item,
                        x=paper_x,
                        y=paper_y,
                        height=item.height / view.scale if view.scale else item.height,
                        width=item.width / view.scale if view.scale else item.width,
                    )
                )
                sheet.from_model += 1
        sheets.append(sheet)

    # Пустой layout — это потеря конвертера только тогда, когда рядом есть
    # непустые: у ПЗУ из тринадцати листов пусты два, и в DWG они не пусты
    # (сверено `dwgread`). Когда пусты ВСЕ, это заготовки: комплект вычерчен в
    # модели, а layout'ы никто не наполнял — у «Сводного плана по сетям» оба
    # листа пусты и в самом DWG. Называть это потерей было бы враньём.
    if sheets and all(s.lost for s in sheets):
        for sheet in sheets:
            sheet.lost = False
            sheet.blank = True
            sheet.note = (
                "лист пуст и в самом чертеже: содержимое вычерчено в "
                "пространстве модели — смотрите его отдельным листом"
            )

    # Второй путь: читаем исходный DWG и дополняем им разбор. Оттуда берутся
    # окна вида потерянных листов и сверяется полнота текста.
    direct_texts: list[str] = []
    if source is not None:
        direct_texts = enrich_from_dwg(source, sheets, model_texts)

    # Layout'ы бывают пустыми: комплект вычерчен прямо в модели, листы стоят
    # рядом каждый в своей рамке. Тогда листы ищем по рамкам.
    #
    # Сравниваем два разбиения по существу, а не по признаку «layout'ы совсем
    # пусты»: у ИОС5 единственный layout цепляет пару подписей из модели, и по
    # такому признаку разбиение по рамкам не включалось бы — пять листов
    # комплекта схлопывались в один почти пустой.
    #
    # Решает ТОЛЬКО количество пойманного текста, но не количество листов. По
    # числу листов разбиение по рамкам однажды выигрывало вчистую, теряя всё
    # содержимое: у «КР1 Пристройка» layout ловит 364 подписи из 374, а рамки
    # не ловят ни одной — они там очерчивают не листы, а мелкие блоки 140×100.
    # Файл превращался в 11 пустых листов вместо одного полного.
    caught_by_layouts = sum(len(s.texts) for s in sheets)
    if caught_by_layouts < len(model_texts):
        by_frames = _sheets_from_frames(doc.modelspace(), model_texts)
        caught_by_frames = sum(len(s.texts) for s in by_frames)
        if by_frames and caught_by_frames > caught_by_layouts:
            # Скрытый текст отдаём и здесь: раньше ранний выход уносил его с
            # собой, и 19 643 подписи ПОС не попадали никуда.
            extra = _hidden_sheet(hidden_texts)
            return by_frames + ([extra] if extra else [])

    # Окно вьюпорта бывает настроено мимо чертежа: в «КР1 Планы» вид смотрит на
    # x −52 816..169 632, а сам чертёж вычерчен около x 2 900 000, и по окну не
    # находится ни одной подписи. Пока лист в файле один, сомнений нет — весь
    # текст модели относится к нему. Когда листов несколько, раздавать один и
    # тот же текст всем неправильно, поэтому лист остаётся пустым, но об этом
    # прямо сказано в паспорте.
    caught = sum(item.from_model for item in sheets)
    if model_texts and not caught:
        if len(sheets) == 1:
            sheets[0].texts.extend(model_texts)
            sheets[0].note = (
                "окно вида в файле указывает мимо чертежа — взят весь текст "
                "пространства модели"
            )
        else:
            # Листов несколько, и раздать им один и тот же текст нельзя. Но и
            # потерять его нельзя: на стройгенплане ПОС в модели 33 825
            # подписей, а через окна на листы не попадает ни одна. Отдаём их
            # отдельным листом в конце — с честной пометкой, что разложить по
            # листам комплекта не удалось.
            for sheet in sheets:
                sheet.note = (
                    "окно вида в файле указывает мимо чертежа — виден только "
                    "собственный текст листа"
                )
            sheets.append(
                Sheet(
                    number=None,
                    name="Текст пространства модели",
                    paper="",
                    paper_width=0.0,
                    paper_height=0.0,
                    texts=list(model_texts),
                    # Лист служебный: это подписи, которые не легли ни на один
                    # лист комплекта. У стройгенплана ПОС их 33 825, и попытка
                    # отдать их геометрией даёт CSV на девять мегабайт —
                    # интерфейс такое не открывает. Содержимое читается текстом.
                    flat=True,
                    note=(
                        "лист собран из подписей, которые не привязались ни к "
                        "одному листу комплекта"
                    ),
                )
            )
            extra = _hidden_sheet(hidden_texts)
            if extra:
                sheets.append(extra)
            return sheets

    # Остаток: подписи модели, не попавшие НИ В ОДНО окно. Раньше их подбирал
    # только случай выше — «не попало вообще ничего», — а частичный промах
    # проходил молча: у ПЗУ так терялись шесть подписей («Граница участка
    # строительства», «Площадка для отдыха…»), потому что окна листов их не
    # накрывают. Терять текст, который в файле есть, нельзя ни в каком объёме.
    if model_texts and caught:
        placed = {item.text for sheet in sheets for item in sheet.texts}
        orphans = [item for item in model_texts if item.text not in placed]
        if orphans:
            xs = [item.x for item in orphans]
            ys = [item.y for item in orphans]
            sheets.append(
                Sheet(
                    number=None,
                    name="Текст вне листов комплекта",
                    paper="",
                    paper_width=0.0,
                    paper_height=0.0,
                    texts=list(orphans),
                    windows=[(min(xs), min(ys), max(xs), max(ys), 1.0)],
                    # Лист служебный: подписи собраны со всей модели и лежат
                    # где попало. У «Фундаментов 6-7» их охват — 227 814 ×
                    # 234 126 единиц, то есть точки на километровом поле;
                    # рисовать такое незачем, читается оно текстом.
                    flat=True,
                    note=(
                        "подписи есть в пространстве модели, но не попадают ни "
                        "в одно окно вида — на каком они листе, из файла не "
                        "видно"
                    ),
                )
            )
    sheets.sort(key=lambda s: (s.number is None, s.number or 0, s.name))

    # Потерянные при конвертации таблицы. Сначала пробуем посадить каждую на
    # свой лист — по заголовку и штампу; что не село, идёт отдельными листами
    # в конце, как раньше.
    # Блоки, которым лист найти не удалось. Каждый отдельным листом — это
    # десятки лишних листов на документ (по комплекту набегало 181), поэтому
    # одиночный блок остаётся листом, а когда их много, они собираются в один
    # лист с подзаголовками: содержимое всё равно всё, а листать нечего.
    left = attach_tables(sheets, orphan_blocks(doc))
    if len(left) == 1:
        name, texts = left[0]
        xs = [t.x for t in texts]
        ys = [t.y for t in texts]
        sheets.append(
            Sheet(
                number=None,
                name=f"Без привязки ({name})",
                source_block=name,
                paper="",
                paper_width=0.0,
                paper_height=0.0,
                texts=list(texts),
                windows=[(min(xs), min(ys), max(xs), max(ys), 1.0)],
                note=(
                    "содержимое есть в файле, но конвертер потерял его привязку "
                    "к листу — место неизвестно, приведено полностью"
                ),
            )
        )
    elif left:
        merged: list[TextItem] = []
        for name, texts in left:
            merged.extend(texts)
        xs = [t.x for t in merged] or [0.0]
        ys = [t.y for t in merged] or [0.0]
        sheets.append(
            Sheet(
                number=None,
                name="Содержимое без привязки к листам",
                paper="",
                paper_width=0.0,
                paper_height=0.0,
                texts=merged,
                windows=[(min(xs), min(ys), max(xs), max(ys), 1.0)],
                flat=True,
                note=(
                    f"блоков, потерявших привязку: {len(left)} "
                    f"({', '.join(name for name, _ in left[:6])}"
                    f"{' и другие' if len(left) > 6 else ''}). Место на листах "
                    "неизвестно, содержимое приведено полностью"
                ),
            )
        )

    # Сверка со вторым путём — последней, когда все листы уже собраны, включая
    # восстановленные таблицы. То, что есть в исходном DWG и не нашлось ни на
    # одном листе, отдаём отдельным листом: истина в DWG, промолчать о
    # расхождении нельзя.
    # Текст с отключённых слоёв — отдельным листом. На печать он не идёт, и
    # смешивать его с содержимым листа нельзя: у ПЗУ так «2 СПОЗУ» ложилось
    # поверх наименования. Но и выбросить нельзя — на слое Defpoints у «Плана
    # кровли» лежат примечания на полторы тысячи знаков.
    extra = _hidden_sheet(hidden_texts)
    if extra:
        sheets.append(extra)

    missing = (
        _missing_texts(sheets, direct_texts, hidden_texts) if direct_texts else []
    )
    if missing:
        sheets.append(
            Sheet(
                number=None,
                name="Текст из исходного DWG, не найденный на листах",
                paper="",
                paper_width=0.0,
                paper_height=0.0,
                texts=[
                    TextItem(0.0, 0.0, 2.5, value, "direct") for value in missing
                ],
                flat=True,
                note=(
                    f"подписей найдено в исходном DWG: {len(missing)}; на листах "
                    "комплекта их нет. Место на листе из исходника не "
                    "восстанавливается — часть таких подписей может лежать в "
                    "блоках, которые нигде не вставлены"
                ),
            )
        )
    return sheets


# Больше этого исходный DWG читать вторым путём не станем: JSON от dwgread
# выходит примерно в семь раз объёмнее файла и целиком держится в памяти. В
# комплекте «Жуковский» самый крупный чертёж — 7,3 МБ, так что запас двойной.
_DIRECT_LIMIT = 32 * 1024 * 1024


def enrich_from_dwg(
    source: Path, sheets: list[Sheet], model_texts: list[TextItem]
) -> list[str]:
    """Дополнить разбор чтением исходного DWG. Возвращает все подписи файла.

    Делает две вещи за одно чтение файла — JSON тяжёлый, второй раз его брать
    незачем:

    1. **Восстанавливает потерянные листы.** Конвертер иногда теряет лист
       целиком: от него не остаётся ни рамки, ни окон вида. Содержимое при
       этом никуда не девается — чертёж лежит в модели, а модель
       конвертируется полностью. Пропадает привязка, те самые окна вида. Их и
       берём из DWG, а текст остаётся из DXF: он там полный, и блоки в нём уже
       развёрнуты.
    2. **Отдаёт все подписи файла** — для сверки полноты. Саму сверку делает
       вызывающий, и делает её последней: листы, восстановленные из потерянных
       таблиц, добавляются в самом конце разбора, а до того их текст выглядел
       бы недостающим.

    Ошибка второго пути не должна ронять разбор: нет `dwgread`, не прочитался
    JSON, файл слишком велик — работаем как раньше, на одном DXF.
    """
    try:
        if source.stat().st_size > _DIRECT_LIMIT:
            return []
        import dwg_direct
    except Exception:
        return []
    data = dwg_direct.read_objects(source)
    if not data:
        return []
    try:
        by_layout = dwg_direct.layout_viewports(data)
        paper_texts = dwg_direct.layout_texts(data)
        every_text = dwg_direct.all_texts(data)
    except Exception:
        return []

    _recover_lost(sheets, model_texts, by_layout, paper_texts)
    return every_text


def _missing_texts(
    sheets: list[Sheet], every_text: list[str], hidden_texts: list | None = None
) -> list[str]:
    """Подписи из DWG, которых нет ни на одном листе.

    Сравниваем по голому тексту, без разметки и регистра: одна и та же надпись
    в DWG и в DXF отличается обёрткой MTEXT, а не содержанием. Считаем с
    кратностью — блок, вставленный на десять листов, даёт в DWG одну подпись, а
    у нас десять, и это не расхождение.
    """
    have: dict[str, int] = {}
    # Подписи с отключённых слоёв считаем известными: они в файле есть, но на
    # печать не идут, и возвращать их «потерей» нельзя.
    for item in hidden_texts or []:
        key = _fold(_plain_direct(getattr(item, "text", str(item))))
        if key:
            have[key] = have.get(key, 0) + 1
    for sheet in sheets:
        # Таблицы, посаженные на лист (`attach_tables`), в `texts` не входят,
        # но в вывод листа попадают — иначе вся таблица числилась бы потерей.
        carried = [item for _name, texts in getattr(sheet, "attached", []) for item in texts]
        for item in list(sheet.texts) + carried:
            key = _fold(item.text)
            if key:
                have[key] = have.get(key, 0) + 1
    # Одна и та же надпись бывает нарезана по-разному: в DWG «Раздел 1.0
    # Пояснительная записка» — один MTEXT, а в таблице DXF это две соседние
    # ячейки. Поэтому кроме точного совпадения проверяем вхождение в сплошной
    # текст всех листов — иначе половина состава проекта числилась бы потерей.
    joined = " | ".join(sorted(have))
    missing: list[str] = []
    for value in every_text:
        text = _plain_direct(value).strip()
        key = _fold(text)
        if not key:
            continue
        if have.get(key):
            have[key] -= 1
        elif key not in joined:
            missing.append(text)
    return missing


def _fold(text: str) -> str:
    """Подпись в виде, в котором её сравнивают: без лишних пробелов и регистра."""
    return " ".join((text or "").split()).strip().lower()


def _recover_lost(
    sheets: list[Sheet],
    model_texts: list[TextItem],
    by_layout: dict,
    paper_texts: dict,
) -> int:
    """Собрать потерянные листы заново по данным из DWG."""
    fixed = 0
    for sheet in sheets:
        if not sheet.lost:
            continue
        key = sheet.layout_name or sheet.name
        # Подписи с самой бумаги листа — их окнами вида не достать.
        for item in paper_texts.get(key, []):
            text = clean_text(_plain_direct(item["text"])).strip()
            if text:
                sheet.texts.append(
                    TextItem(
                        item["x"],
                        item["y"],
                        item["height"],
                        text,
                        item["kind"].lower(),
                        math.degrees(item["rotation"]),
                        item["width"],
                    )
                )
        windows = by_layout.get(key)
        if not windows:
            if sheet.texts:
                sheet.lost = False
                sheet.note = (
                    "лист восстановлен из исходного DWG: конвертер потерял его "
                    "целиком, окон вида в исходнике нет — взяты подписи с бумаги"
                )
                fixed += 1
            continue
        views = [_view_from_direct(w) for w in windows]
        views = [v for v in views if v is not None]
        if not views:
            continue
        seen: set[tuple[float, float, str]] = set()
        for view in views:
            sheet.views.append(view)
            sheet.windows.append((*view.world, round(view.scale, 2)))
            sheet.scales.append(round(view.scale, 2))
            for item in model_texts:
                if not view.holds(item.x, item.y):
                    continue
                key = (round(item.x, 2), round(item.y, 2), item.text)
                if key in seen:
                    continue
                seen.add(key)
                paper_x, paper_y = view.to_paper(item.x, item.y)
                sheet.texts.append(
                    replace(
                        item,
                        x=paper_x,
                        y=paper_y,
                        height=item.height / view.scale if view.scale else item.height,
                        width=item.width / view.scale if view.scale else item.width,
                    )
                )
                sheet.from_model += 1
        if not sheet.texts:
            # Окна нашлись, а подписей в них нет — лист графический. Пометку о
            # потере всё равно снимаем: лист восстановлен, просто без текста.
            sheet.lost = False
            sheet.note = (
                "лист восстановлен из исходного DWG: конвертер потерял его "
                "рамку и окна вида, подписей в окнах нет"
            )
            fixed += 1
            continue
        sheet.lost = False
        sheet.note = (
            "лист восстановлен из исходного DWG: конвертер потерял его рамку "
            "и окна вида, содержимое взято по окнам из исходника"
        )
        fixed += 1
    return fixed


def _view_from_direct(window: dict) -> "SheetView | None":
    """Окно вида из DWG — в тот же вид, что строит `_viewport_window` по DXF.

    Формулы те же, включая поправку центра: `VIEWCTR` задан в системе координат
    вида, а не в мировой, поэтому к «цели вида» прибавляется смещение центра,
    повёрнутое на угол разворота. Разница между источниками одна — в DWG угол
    хранится в радианах, в DXF в градусах.
    """
    try:
        paper_w, paper_h = window["paper_size"]
        center_x, center_y = window["view_center"]
        target_x, target_y = window["view_target"]
        view_h = float(window["view_height"])
        twist = float(window["twist"])
    except Exception:
        return None
    if view_h <= 0 or paper_h <= 0 or paper_w <= 0:
        return None
    cos_a, sin_a = math.cos(twist), math.sin(twist)
    world_x = target_x + center_x * cos_a - center_y * sin_a
    world_y = target_y + center_x * sin_a + center_y * cos_a
    view_w = view_h * (paper_w / paper_h)
    return SheetView(
        world=(
            world_x - view_w / 2,
            world_y - view_h / 2,
            world_x + view_w / 2,
            world_y + view_h / 2,
        ),
        paper_center=window["paper_center"],
        paper_size=(paper_w, paper_h),
        scale=view_h / paper_h,
        twist=twist,
    )


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
    joined_lines: list[str] = []
    for line in lines:
        line.sort(key=lambda t: t.x)
        joined = " ".join(t.text.replace(chr(10), " ").strip() for t in line)
        joined = re.sub(r"\s{2,}", " ", joined).strip()
        if joined:
            joined_lines.append(joined)

    # На плане одна и та же метка стоит у каждого объекта: на стройгенплане
    # ПОС отдельные подписи повторяются сотнями. Для чтения это шум, поэтому
    # частые повторы схлопываем — тем же приёмом, каким normalize_pdf_text()
    # разбирается со спамом меток в текстовом слое PDF.
    counts = Counter(joined_lines)
    shown: set[str] = set()
    out: list[str] = []
    for line in joined_lines:
        repeats = counts[line]
        if repeats < REPEAT_LIMIT:
            out.append(line)
            continue
        if line in shown:
            continue
        shown.add(line)
        out.append(f"{line} (×{repeats} на листе — схлопнуто)")
    return out


# Сколько одинаковых строк на листе считать спамом меток, а не текстом.
REPEAT_LIMIT = 4


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


def xref_names(doc) -> set[str]:
    """Имена блоков, которые на деле — внешние ссылки на другие файлы.

    В комплекте «Жуковский» весь ПЗУ собран из xref: в самом файле лежат рамка
    и штамп, а генплан, топосъёмка и сети — в отдельных DWG папки `_Ссылки`,
    которой в присланном комплекте нет. Без пометки лист выглядит так, будто на
    нём почти ничего не начерчено, — а на самом деле начерченное не приложено.
    """
    names = set()
    for block in doc.blocks:
        try:
            flags = int(block.block.dxf.get("flags", 0))
        except Exception:
            continue
        if flags & 4:  # бит внешней ссылки
            names.add(block.name)
    return names


# Слова основной надписи листа. Штамп есть почти на каждом листе, и в карте
# он должен называться штампом, а не «текстом в юго-восточном углу».
_STAMP_WORDS = ("изм.", "кол.уч", "подпись и дата", "инв. n°", "инв. №", "взам")


def _text_zone(item, box) -> str:
    """Зона листа, в которой лежит подпись."""
    return _zone((item.x, item.y, item.x, item.y), box)


def sheet_extras(msp, sheet: Sheet):
    """Таблицы листа и число вставленных OLE-объектов.

    OLE — это вставленный кусок Excel или картинка. В комплекте «Жуковский»
    так вставлен состав проекта: внутри лежит растр, а не данные, поэтому в
    текст листа он не попадает. Молчать об этом нельзя — иначе лист выглядит
    пустым, хотя на нём напечатана таблица.
    """
    try:
        from dwg_tables import sheet_tables

        # У листа, собранного из потерянного блока, и сетка, и подписи лежат
        # внутри этого блока, а не в пространстве модели.
        space = msp
        if sheet.source_block:
            try:
                space = msp.doc.blocks.get(sheet.source_block)
            except Exception:
                space = msp
        tables = sheet_tables(space, sheet, sheet.texts)
    except Exception:
        tables = []
    # Таблицы, вставку которых потерял конвертер, а лист опознан по заголовку
    # и надписи (`attach_tables`). Сетка и подписи у них лежат внутри своего
    # блока, поэтому строим их из него же, а не из модели.
    for block_name, texts in getattr(sheet, "attached", []):
        try:
            from dwg_tables import sheet_tables as _tables

            # Окна и виды исходного листа сюда тащить нельзя: по ним
            # считаются границы, а подписи блока лежат в своих координатах —
            # сетка тогда не находится, и таблица выходит плоским текстом.
            # Границы листа считаются по окнам, поэтому подсовываем окно по
            # габаритам самих подписей блока — так же, как это делалось, пока
            # такая таблица была отдельным листом. Без него границ нет и сетка
            # не ищется вовсе.
            xs = [item.x for item in texts]
            ys = [item.y for item in texts]
            # С запасом: крайние линии сетки стоят ШИРЕ подписей (у «Содержания»
            # текст лежит в 30..170, а рамка таблицы — в 0..185), и по границам
            # ровно по тексту они отсекались, оставляя таблицу без колонок.
            pad_x = (max(xs) - min(xs)) * 0.25 + 1.0
            pad_y = (max(ys) - min(ys)) * 0.1 + 1.0
            carrier = replace(
                sheet,
                texts=list(texts),
                windows=[
                    (
                        min(xs) - pad_x,
                        min(ys) - pad_y,
                        max(xs) + pad_x,
                        max(ys) + pad_y,
                        1.0,
                    )
                ],
                views=[],
                source_block=block_name,
                attached=[],
            )
            block = msp.doc.blocks.get(block_name)
            found = _tables(block, carrier, carrier.texts, trusted=True)
        except Exception:
            found = []
        if found:
            tables += found
        else:
            # Сетку восстановить не вышло — отдаём хотя бы подписи по порядку
            # чтения, иначе содержимое таблицы пропало бы совсем.
            body = "\n".join(reading_order(list(texts)))
            if body.strip():
                tables.append(((0.0, 0.0, 0.0, 0.0), body))
    box = sheet.extent()
    ole = 0
    # У листа, собранного из потерянного блока, своей области на чертеже нет —
    # считать по ней вставки бессмысленно.
    if box is not None and not sheet.source_block:
        for entity in msp:
            if entity.dxftype() != "OLE2FRAME":
                continue
            try:
                point = entity.dxf.get("insert", None)
                if point is None or (
                    box[0] <= point.x <= box[2] and box[1] <= point.y <= box[3]
                ):
                    ole += 1
            except Exception:
                ole += 1
    return tables, ole


# Чем оказался лист, когда текста на нём не нашлось. Исхода «просто пусто» быть
# не должно: либо мы не выбили текст, либо текста на листе и нет — и тогда это
# графика, и это надо сказать прямо, а не отдать пустую страницу.
CONTENT_TEXT = "text"  # подписи есть — обычный случай
CONTENT_DRAWING = "drawing"  # текста нет, но лист вычерчен: чертёж или схема
CONTENT_LOST = "lost"  # нет ни текста, ни линий — до нас лист не дошёл
CONTENT_BLANK = "blank"  # лист пуст и в самом чертеже: заготовка layout'а

# Заглушка на месте содержимого. Стоит вместо пустоты, чтобы нумерация листов
# и структура отчёта не рвались, а читатель видел причину.
MOCK_DRAWING = (
    "**[mock] Лист графический: подписей в файле нет.**\n\n"
    "_На листе {shapes} и ни одной текстовой подписи — расшифровывать нечего. "
    "Содержимое такого листа разбирается отдельно, по чертежу._"
)
MOCK_BLANK = (
    "**[mock] Лист пуст и в самом чертеже.**\n\n"
    "_Это заготовка: в файле лист есть, но на нём ничего не вычерчено — "
    "комплект нарисован в пространстве модели. Его содержимое отдаётся "
    "отдельным листом в конце документа._"
)
MOCK_LOST = (
    "**[mock] Лист не прочитан: потеря при конвертации DWG → DXF.**\n\n"
    "_В файле от этого листа не осталось ни рамки, ни линий, ни подписей. "
    "В исходном чертеже содержимое может быть на месте: у листов ПЗУ так и "
    "было — открывайте DWG в чертёжной программе._"
)


def sheet_content(sheet: Sheet, layers=None, flow=None) -> str:
    """Чем оказался лист: текстом, графикой или потерей конвертера.

    Потерей считаем только то, что при чтении оказалось пустым по-настоящему
    (`Sheet.lost`). Судить по «нет ни слоёв, ни окон» нельзя: у файла опорных
    точек ПОС в модели 112 вставок, а окно вида одно и нечитаемое — лист
    графический, а выглядел бы потерянным.
    """
    if flow:
        return CONTENT_TEXT
    if any((item.text or "").strip() for item in sheet.texts):
        return CONTENT_TEXT
    if sheet.lost:
        return CONTENT_LOST
    return CONTENT_BLANK if sheet.blank else CONTENT_DRAWING


def _mock_block(sheet: Sheet, layers=None) -> str:
    """Заглушка вместо содержимого — с описанием того, что на листе всё же есть."""
    content = sheet_content(sheet, layers)
    if content == CONTENT_LOST:
        return MOCK_LOST
    if content == CONTENT_BLANK:
        return MOCK_BLANK
    if layers:
        entities = sum(st.entities for st in layers)
        length = sum(st.length_m for st in layers)
        shapes = f"{entities} объектов черчения в {len(layers)} слоях"
        if length >= 0.1:
            shapes += f", общей длиной {length:.0f} м"
    else:
        shapes = "есть вычерченное содержимое"
    return MOCK_DRAWING.format(shapes=shapes)


def sheet_flow(sheet: Sheet, tables=None) -> list[str]:
    """Содержимое листа в порядке чертежа: подписи сверху вниз, таблицы на месте.

    Таблица врезается туда, где она нарисована, а не выносится отдельным
    списком в конец: инженер читает лист так же, как смотрит на него.
    """
    tables = tables or []
    lines: list[tuple[float, str]] = []
    covered = []
    number = 0
    for box, markdown in tables:
        covered.append(box)
        grid = [ln for ln in markdown.splitlines() if ln.startswith("|")]
        if len(grid) < 2 or grid[0].count("|") - 1 < 2:
            # Сетки нет — это просто подписи, лежавшие рядом. Заголовок
            # «Таблица» над одной строкой обманывает: на листе ИГР таких
            # заголовков набиралось десять, и ни один не вёл к таблице.
            lines.append((box[3], markdown))
            continue
        number += 1
        lines.append((box[3], chr(10).join([f"**Таблица {number}**", "", markdown])))
    outside = [
        item
        for item in sheet.texts
        if not any(
            b[0] <= item.x <= b[2] and b[1] <= item.y <= b[3] for b in covered
        )
    ]
    for text in _text_by_zone(sheet, outside):
        lines.append((None, text))
    # Таблицы расставляем по своей высоте, текст идёт своим порядком чтения:
    # смешивать их по одному ключу нельзя — у строк текста высоты уже нет.
    ordered: list[str] = []
    table_lines = sorted([ln for ln in lines if ln[0] is not None], key=lambda ln: -ln[0])
    text_lines = [ln[1] for ln in lines if ln[0] is None]
    ordered.extend(text_lines)
    for _, block in table_lines:
        ordered.insert(0, block)
    return ordered


# С какого числа подписей простыню текста стоит делить на части. Ниже этого
# порога заголовки только мешают: на титульном листе их три штуки.
_ZONE_SPLIT_FROM = 40

# Порядок обхода листа: сверху вниз, слева направо — как читают чертёж.
_ZONE_ORDER = (
    "северо-запад", "север", "северо-восток",
    "запад", "центр", "восток",
    "юго-запад", "юг", "юго-восток",
    "по всему листу",
)


def _text_by_zone(sheet: Sheet, items: list[TextItem]) -> list[str]:
    """Текст листа, разбитый по углам и середине.

    На чертеже подписи разбросаны, и сплошная лента строк («СКВ 89 155.80»,
    «10 лоб, МПа») не читается вовсе: непонятно, что относится к разрезу, что
    к условным обозначениям, а что к штампу. Разбиваем по зонам листа и
    подписываем каждую — тогда видно, из какого места чертежа строки.
    """
    if len(items) < _ZONE_SPLIT_FROM:
        return reading_order(items)
    box = sheet.extent()
    if box is None:
        return reading_order(items)
    groups: dict[str, list[TextItem]] = {}
    for item in items:
        groups.setdefault(_text_zone(item, box), []).append(item)
    if len(groups) < 2:
        return reading_order(items)
    out: list[str] = []
    known = [z for z in _ZONE_ORDER if z in groups]
    for zone in known + [z for z in groups if z not in _ZONE_ORDER]:
        body = reading_order(groups[zone])
        if not body:
            continue
        out.append(f"**{zone.capitalize()} листа**")
        out.extend(body)
    return out


def sheet_map(sheet: Sheet, layers=None, tables=None, ole=0) -> str:
    """Карта листа: что за блок, где он и какого объёма.

    Пишется для модели: строки однообразны, содержимое не пересказывается —
    оно идёт ниже дословно. Нужна, чтобы вопрос «что в правом нижнем углу»
    не приходилось решать по потоку подписей.
    """
    box = sheet.extent()
    rows = ["| Блок | Где на листе | Объём |", "|---|---|---|"]
    if box is None:
        return ""
    stamp = [t for t in sheet.texts if any(w in t.text.lower() for w in _STAMP_WORDS)]
    if stamp:
        zone = _text_zone(stamp[0], box)
        rows.append(f"| штамп | {zone} | {len(stamp)} стр. |")
    by_zone: dict[str, int] = {}
    for item in sheet.texts:
        if item in stamp:
            continue
        by_zone[_text_zone(item, box)] = by_zone.get(_text_zone(item, box), 0) + 1
    for zone, count in sorted(by_zone.items(), key=lambda kv: -kv[1]):
        rows.append(f"| подписи | {zone} | {count} шт. |")
    index = 0
    for table_box, markdown in tables or []:
        grid_rows = [ln for ln in markdown.splitlines() if ln.startswith("|")]
        columns = grid_rows[0].count("|") - 1 if grid_rows else 0
        # Блок, у которого сетка не нашлась, таблицей называть нельзя: в карте
        # он выглядел строкой «таблица 7 · 0×0», и таких строк набиралось
        # больше, чем настоящих таблиц.
        if len(grid_rows) < 2 or columns < 2:
            continue
        index += 1
        rows.append(
            f"| таблица {index} | {_zone(table_box, box)} | "
            f"{max(len(grid_rows) - 1, 0)}×{columns} |"
        )
    if ole:
        rows.append(f"| вставленный объект | — | {ole} шт. |")
    return "\n".join(rows) if len(rows) > 2 else ""


def sheet_markdown(
    sheet: Sheet,
    file_name: str,
    layers: list[LayerStat] | None = None,
    blocks: Counter | None = None,
    xrefs: set[str] | None = None,
    tables: list | None = None,
    ole: int = 0,
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
    content = sheet_content(sheet, layers)
    if content != CONTENT_TEXT:
        # Признак для тех, кто читает паспорт машиной: лист без текста — это
        # не пустой лист, а либо графика, либо потеря.
        parts[-1:] = [
            "- содержимое: "
            + (
                "только графика, текста нет (mock)"
                if content == CONTENT_DRAWING
                else "лист пуст и в самом чертеже — заготовка (mock)"
                if content == CONTENT_BLANK
                else "лист потерян при конвертации DWG → DXF (mock)"
            ),
            "",
        ]
    if sheet.note:
        label = "восстановлено" if sheet.recovered else "разбор листа неполон"
        parts[-1:] = [f"- {label}: {sheet.note}", ""]
    # Карта листа идёт в PASS-A, а не отдельной секцией PASS-0: сервис
    # считает PASS-0 служебным паспортом и в интерфейс его не выводит, а карта
    # нужна как раз на экране и в промпте.
    map_md = sheet_map(sheet, layers, tables, ole)
    if map_md:
        parts += [
            "### PASS-A Карта листа",
            "",
            "_Что где лежит на листе. Содержимое не пересказывается — оно ниже "
            "дословно._",
            "",
            map_md,
            "",
        ]
    if ole:
        parts += [
            f"_На листе вставленных объектов (OLE): {ole}. Внутри такой вставки "
            "лежит картинка, а не векторные данные, и её содержимое в текст "
            "листа не попадает._",
            "",
        ]
    if layers:
        parts += [
            "**Состав листа (из геометрии)**",
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
        outside = [n for n in blocks if xrefs and n in xrefs]
        if outside:
            listed = ", ".join(f"`{n}`" for n in outside[:10])
            parts += [
                "_Часть изображённого на листе — внешние ссылки на другие "
                f"файлы ({len(outside)} шт.: {listed}). Этих файлов в "
                "комплекте нет, поэтому в состав по слоям вошло только то, "
                "что вычерчено в самом чертеже._",
                "",
            ]
    parts += [
        "### PASS-B Текст листа (из DWG)",
        "",
        "_Текст взят из чертежа как данные: модель не вызывалась._",
        "",
    ]
    # Пустой раздел означал бы «текста нет», не отличая графический лист от
    # нашей недоработки. Отдаём заглушку с причиной — читать пустую страницу
    # и гадать инженеру не приходится.
    flow = sheet_flow(sheet, tables)
    parts.extend(flow or [_mock_block(sheet, layers)])
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
    xrefs = xref_names(doc)
    for sheet in sheets:
        layers, blocks = analyse_sheet(msp, sheet, unit_m)
        tables, ole = sheet_extras(msp, sheet)
        chunks.append(
            sheet_markdown(sheet, path.name, layers, blocks, xrefs, tables, ole)
        )
    return head + "\n" + "\n\n".join(chunks)


# ── Готовое применение: лист чертежа для сервиса ────────────────────────────
#
# Сервис считает по листу за вызов и открывает файл заново. Конвертация DWG и
# разбор DXF на 17 МБ стоят секунды, поэтому результат запоминаем на документ —
# тем же приёмом, что deglyph.map_for_doc и pdf_tables.frames_for_doc.

_DOC_CACHE: dict[tuple[str, float], tuple[Path, list]] = {}


def sheets_for(path: Path) -> tuple[Path, list]:
    """(путь к DXF, листы) для документа, с кэшем на процесс."""
    key = (str(path.resolve()), path.stat().st_mtime)
    cached = _DOC_CACHE.get(key)
    if cached is None:
        dxf = to_dxf(path)
        # Путь к исходнику передаём дальше: по нему дочитываются листы,
        # потерянные конвертацией.
        cached = (dxf, read_sheets(dxf, path))
        _DOC_CACHE[key] = cached
    return cached


def sheet_count(path: Path) -> int:
    """Сколько листов в чертеже — аналог числа страниц у PDF."""
    return len(sheets_for(path)[1])


def page_markdown(path: Path, page_number: int) -> tuple[str, str]:
    """Лист по порядковому номеру: (markdown, тип листа).

    Нумерация СКВОЗНАЯ по файлу, как страницы PDF: интерфейс листает 1..N.
    Собственный номер листа в комплекте («22 Фундамент Ф-1») не теряется — он
    уходит в паспорт, потому что именно на него ссылаются в замечаниях.
    """
    import ezdxf

    dxf, sheets = sheets_for(path)
    if not 1 <= page_number <= len(sheets):
        raise IndexError(f"в чертеже {len(sheets)} листов, запрошен {page_number}")
    sheet = sheets[page_number - 1]
    doc = ezdxf.readfile(str(dxf))
    unit_code = int(doc.header.get("$INSUNITS", 0) or 0)
    unit_m = _UNIT_TO_M.get(unit_code, 0.001)
    msp = doc.modelspace()
    layers, blocks = analyse_sheet(msp, sheet, unit_m)
    tables, ole = sheet_extras(msp, sheet)
    body = sheet_markdown(
        sheet, path.name, layers, blocks, xref_names(doc), tables, ole
    )
    # Сервис нумерует листы сам, поэтому свой заголовок убираем.
    head, sep, rest = body.partition(chr(10))
    if head.startswith("## Страница"):
        body = rest.lstrip()
    kind = "plan" if any(st.length_m > 0 for st in layers) else "text"
    return body, kind


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
