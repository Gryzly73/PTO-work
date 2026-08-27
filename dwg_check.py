"""Сверка разбора чертежа числами: что прочитано, что потеряно, что мусор.

Зачем это здесь. Ошибки разбора мы до сих пор ловили глазами на картинке.
Ширина подписи читалась не из того поля DXF — у TEXT в группе 41 лежит сжатие
знака, а у MTEXT ширина блока, — и заметили это только потому, что подписи
наехали друг на друга на превью. Картинка не то, ради чего делается конвейер:
на выходе нужен markdown, по которому работает модель. Значит, ошибку такого
рода ловить будет нечем.

Здесь разбор проверяется сам по себе, без картинки. Проверки трёх родов:

  * **поле не доехало.** Файл читается вторым, независимым проходом, и по
    каждому виду подписей сравнивается доля: в чертеже поворот задан у трети
    надписей — а в разборе у скольких? ширина блока? сжатие знака? Треть
    против нуля и означает «атрибут прочитан не тот или не прочитан вовсе».
    Обе найденные ошибки этого рода — ширина у TEXT и поворот у MTEXT —
    выглядели здесь строкой «в чертеже у 31%, в разборе у 0%».
  * **мусор в тексте.** Недораскрытые коды `\\U+041E`, остатки разметки MTEXT,
    одинокие суррогаты, заглушки полей `####`. Всё это едет в markdown как
    есть, и модель читает его как содержание листа.
  * **числа не могут быть такими.** Высота знака нулевая или в треть листа,
    ширина блока меньше высоты знака, сжатие вне 0,3…3, подпись за пределами
    листа. Это не приговор — часть чертежей действительно битая, — но каждое
    такое число где-то дальше становится неверным переносом строки, неверной
    зоной листа или неверным порядком чтения.

Порог не в том, чтобы находок было ноль: в комплекте «Жуковский» их и не будет
ноль. Порог в том, что число находок известно и не растёт. Отсюда `--baseline`:
слепок числа находок по набору файлов, с которым сверяется следующий прогон.

Запуск:

    python dwg_check.py "1. Стадия П DWG/2 - ПЗУ/Жуковский 1_ПЗУ.dwg"
    python dwg_check.py "1. Стадия П DWG" --baseline check_baseline.json
    python dwg_check.py "1. Стадия П DWG/2 - ПЗУ" --twice

`--twice` конвертирует каждый чертёж дважды и сравнивает текст: так видно,
воспроизводим ли сам конвертер. Проверка платная, поэтому не по умолчанию.
"""
from __future__ import annotations

import json
import math
import re
import sys
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path

# Уровни находок. «Ошибка» — то, чего в исправном разборе быть не должно:
# поле, которое есть в чертеже и не доехало до разбора. «Сигнал» — то, что
# бывает и от битого исходника: мусор в подписи, невозможное число.
ERROR = "ошибка"
SIGNAL = "сигнал"


@dataclass
class Finding:
    level: str
    kind: str
    count: int
    note: str
    examples: list[str] = field(default_factory=list)


# ── что говорит о подписях сам файл ─────────────────────────────────────────
#
# Проход намеренно свой, а не через `dwg_sheets`: сверять разбор с самим собой
# бессмысленно. Здесь читается голый DXF теми же атрибутами, но напрямую.

_MTEXT_FACTOR = re.compile(r"\\W([0-9]*\.?[0-9]+)")

# Виды подписей, по которым сверяются поля. Ключ совпадает с `TextItem.source`
# — так находка сразу указывает, в какой ветке разбора искать.
_KINDS = ("mtext", "text", "attrib")


def _rotated(entity) -> bool:
    """Подпись стоит не горизонтально.

    У MTEXT угол хранится вектором `text_direction`, а не числом `rotation`:
    в комплекте «Жуковский» из 763 блоков MTEXT числа нет ни у одного.
    """
    kind = entity.dxftype()
    try:
        if kind == "MTEXT":
            direction = entity.dxf.get("text_direction", None)
            if direction is not None:
                return _turned(math.degrees(math.atan2(direction.y, direction.x)))
        return _turned(float(entity.dxf.get("rotation", 0.0) or 0.0))
    except Exception:
        return False


def _turned(angle: float) -> bool:
    """Угол не нулевой. Волосок ниже нуля — это ноль, а не поворот на 360°."""
    angle = round(angle % 360, 1) % 360
    return angle != 0.0


def _has_text(entity) -> bool:
    """В подписи что-то написано.

    Пустые считать нельзя: разбор их отбрасывает, и без такого же отсева
    сверка объявляла ошибкой четыре пустых повёрнутых ATTRIB из блока витража
    — надписи, которой нет.
    """
    try:
        value = entity.text if entity.dxftype() == "MTEXT" else entity.dxf.get("text", "")
    except Exception:
        return False
    return bool((value or "").strip())


def _boxed(entity) -> bool:
    """У блока задана ширина, по которой AutoCAD переносит строки."""
    if entity.dxftype() != "MTEXT":
        return False
    try:
        return float(entity.dxf.get("width", 0.0) or 0.0) > 0
    except Exception:
        return False


def _squeezed(entity) -> bool:
    """Подпись сжата по ширине: у TEXT числом, у MTEXT кодом `\\W` в разметке."""
    try:
        if entity.dxftype() == "MTEXT":
            found = _MTEXT_FACTOR.search(entity.text or "")
            return bool(found) and abs(float(found.group(1)) - 1.0) > 0.01
        return abs(float(entity.dxf.get("width", 1.0) or 1.0) - 1.0) > 0.01
    except Exception:
        return False


def _file_fields(dxf_path: Path) -> dict[str, dict[str, int]]:
    """Сколько в чертеже подписей каждого вида и с каким заданным полем.

    Скрытое в самом чертеже не считаем: разбор его на листы не берёт, и без
    этого отсева сравнение врёт. На листе «Кон-ции АББ» все 105 сжатых
    подписей лежат на отключённых слоях, и проверка объявляла ошибкой то, что
    разбор их не показывает, — а он и не должен.
    """
    import ezdxf

    from dwg_sheets import _is_hidden, hidden_layers

    doc = ezdxf.readfile(str(dxf_path))
    hidden = hidden_layers(doc)
    out = {kind: {"всего": 0, "повёрнут": 0, "ширина": 0, "сжат": 0} for kind in _KINDS}

    def walk(space, depth: int = 0) -> None:
        if depth > 3:
            return
        for entity in space:
            if _is_hidden(entity, hidden):
                continue
            if entity.dxftype() == "INSERT":
                for attrib in entity.attribs:
                    count(attrib)
                try:
                    walk(entity.virtual_entities(), depth + 1)
                except Exception:
                    pass
                continue
            count(entity)

    def count(item) -> None:
        kind = item.dxftype().lower()
        if kind not in out or _is_hidden(item, hidden) or not _has_text(item):
            return
        out[kind]["всего"] += 1
        out[kind]["повёрнут"] += _rotated(item)
        out[kind]["ширина"] += _boxed(item)
        out[kind]["сжат"] += _squeezed(item)

    # Блоки обходим только через вставки, как это делает разбор: в чертеже
    # «Кон-ции АББ» 98 блоков с повёрнутым MTEXT не вставлены никуда, и по
    # определениям блоков проверка объявляла бы потерянным то, чего на листах
    # нет и быть не должно.
    walk(doc.modelspace())
    for name in doc.layouts.names():
        if name != "Model":
            walk(doc.layouts.get(name))
    return out


def _parsed_fields(sheets) -> dict[str, dict[str, int]]:
    """То же самое, но по результату разбора."""
    out = {kind: {"всего": 0, "повёрнут": 0, "ширина": 0, "сжат": 0} for kind in _KINDS}
    for sheet in sheets:
        for item in sheet.texts:
            slot = out.get(item.source)
            if slot is None:
                continue
            slot["всего"] += 1
            slot["повёрнут"] += _turned(item.rotation)
            slot["ширина"] += (item.width or 0) > 0
            slot["сжат"] += abs((getattr(item, "factor", 1.0) or 1.0) - 1.0) > 0.01
    return out


_FIELD_NOTE = {
    "повёрнут": "поворот подписи",
    "ширина": "ширина текстового блока",
    "сжат": "сжатие знака по ширине",
}


# Сколько подписей вида должно быть в разборе, чтобы по ним вообще судить.
# Ниже этого «в разборе ноль» означает не ошибку чтения, а то, что таких
# подписей на листы и не попало: часть блоков в чертеже нигде не вставлена, а
# отключённые слои разбор отсекает.
_ENOUGH = 10

# Какая доля подписей должна нести поле, чтобы его отсутствие о чём-то
# говорило. Одна повёрнутая надпись на тысячу — не признак поломки чтения.
_NOTICEABLE = 0.02

# Какую часть подписей чертежа разбор должен увидеть, чтобы их поля вообще
# было с чем сравнивать.
_COMPARABLE = 0.25


def _field_findings(dxf_path: Path, sheets) -> list[Finding]:
    """Поля, которые в чертеже заданы, а до разбора не доехали.

    Сравниваем доли, а не штуки: разбор разворачивает блоки (одна надпись в
    файле — десять на листах) и отбрасывает отключённые слои, поэтому числа с
    двух сторон и не должны совпадать. А вот доля подписей с заданным полем
    от такой пересборки почти не меняется — если поле читается.
    """
    try:
        in_file = _file_fields(dxf_path)
    except Exception as error:
        return [Finding(SIGNAL, "файл не перечитан", 1, str(error)[:120])]
    parsed = _parsed_fields(sheets)
    out: list[Finding] = []
    for kind in _KINDS:
        total_file = in_file[kind]["всего"]
        total_parsed = parsed[kind]["всего"]
        if not total_file or total_parsed < max(_ENOUGH, total_file * _COMPARABLE):
            # Разбор дошёл до слишком малой части подписей этого вида, чтобы
            # судить о его полях: это блоки, нигде не вставленные, и слои,
            # отключённые в самом чертеже. Полноту текста стережёт не эта
            # проверка, а сверка с прямым чтением DWG («подписи без листа»).
            continue
        for field_name, note in _FIELD_NOTE.items():
            has, got = in_file[kind][field_name], parsed[kind][field_name]
            share_file = has / total_file
            share_parsed = got / total_parsed
            if share_file < _NOTICEABLE:
                continue
            where = (
                f"в чертеже у {share_file:.0%} подписей {kind.upper()} "
                f"({has} из {total_file}), в разборе у {share_parsed:.0%} "
                f"({got} из {total_parsed})"
            )
            if not got:
                out.append(
                    Finding(ERROR, f"{note} потеряно у {kind.upper()}", has,
                            where + " — атрибут читается не тот")
                )
            elif share_parsed < share_file / 4:
                out.append(
                    Finding(SIGNAL, f"{note} читается редко у {kind.upper()}",
                            has - got, where)
                )
    return out


# ── мусор в тексте ──────────────────────────────────────────────────────────

_JUNK = (
    ("недораскрытый код юникода", re.compile(r"\\[UM]\+")),
    ("остаток разметки MTEXT", re.compile(r"\{\\[A-Za-z]|\\[fFHWTQ][^;]{0,24};")),
    ("заглушка невычисленного поля", re.compile(r"#{3,}")),
    ("замещающий знак", re.compile("[\ufffd\u0001-\u0008]")),
)


def _junk_findings(sheets) -> list[Finding]:
    counts: dict[str, list[str]] = {}
    for sheet in sheets:
        for item in sheet.texts:
            for name, rx in _JUNK:
                if rx.search(item.text):
                    counts.setdefault(name, []).append(item.text[:60])
    return [
        Finding(SIGNAL, name, len(found), "едет в markdown как содержание листа", found[:3])
        for name, found in counts.items()
    ]


# ── числа, которых не бывает ────────────────────────────────────────────────


def _number_findings(sheets) -> list[Finding]:
    counts: dict[str, list[str]] = {}

    def note(name: str, text: str) -> None:
        counts.setdefault(name, []).append(text[:60])

    for sheet in sheets:
        box = sheet.extent()
        for item in sheet.texts:
            if item.height <= 0:
                note("высота знака нулевая", item.text)
            elif box and item.height > (box[3] - box[1]) / 3:
                note("высота знака в треть листа", item.text)
            # Ширина блока меньше высоты знака — по такой ширине не встанет ни
            # одна буква. Ровно так выглядела ошибка с группой 41.
            if 0 < item.width < item.height:
                note("ширина блока меньше высоты знака", item.text)
            factor = getattr(item, "factor", 1.0) or 1.0
            if not 0.3 <= factor <= 3.0:
                note("сжатие вне разумного", f"{factor:g} · {item.text}")
            if box and not _inside(item, box):
                note("подпись за пределами листа", item.text)
    return [
        Finding(SIGNAL, name, len(found), "искажает перенос строк и порядок чтения", found[:3])
        for name, found in counts.items()
    ]


def _inside(item, box) -> bool:
    """Подпись лежит в пределах листа с запасом в его габарит."""
    x0, y0, x1, y1 = box
    pad_x, pad_y = (x1 - x0) or 1.0, (y1 - y0) or 1.0
    return x0 - pad_x <= item.x <= x1 + pad_x and y0 - pad_y <= item.y <= y1 + pad_y


# ── целостность комплекта ───────────────────────────────────────────────────


def _cut_findings(sheets) -> list[Finding]:
    """Подписи, обрезанные конвертером DWG → DXF.

    Прямое чтение DWG видит «12-12 (Опалубка) Ф-3.1, Ф-3.2, Ф-3.3», а через
    DXF до нас доезжает «12-12 (». Такая подпись не теряется — целую сверка
    кладёт отдельным листом, — но на самом листе стоит обрубок, и модель
    читает как содержание чертежа именно его.

    Узнаём по началу строки: обрубок — это начало целой подписи. Сравниваем
    только с теми, что сверка уже признала ненайденными: там и лежат целые.
    """
    whole = [
        item.text
        for sheet in sheets
        if getattr(sheet, "flat", False) and "не найденный" in sheet.name
        for item in sheet.texts
    ]
    if not whole:
        return []
    # По отсортированному списку целых подпись ищется двоичным поиском: на
    # стройгенплане ПОС иначе выходит перебор двенадцати тысяч подписей на
    # восемьсот, и сверка одного листа стоит секунды.
    whole.sort()
    cut: list[str] = []
    for sheet in sheets:
        if getattr(sheet, "flat", False):
            continue
        for item in sheet.texts:
            head = " ".join(item.text.split())
            # Обрубок в один-два знака ни о чём не говорит: «1» — начало
            # половины подписей чертежа.
            if len(head) < 4:
                continue
            at = bisect_left(whole, head)
            if at < len(whole) and whole[at].startswith(head) and len(whole[at]) > len(head):
                cut.append(head)
    return (
        [Finding(SIGNAL, "подпись обрезана конвертером", len(cut),
                 "на листе стоит начало надписи, целая — только в исходном DWG",
                 cut[:3])]
        if cut
        else []
    )


def _stable_findings(path: Path) -> list[Finding]:
    """Даёт ли конвертер DWG → DXF один и тот же текст дважды.

    Не даёт. На листе фундаментов КР1 три блока MTEXT из 1894 каждый раз
    выходят с разным хвостом: «2-2 (Опалубка)» то целое, то обрубленное до
    «2-2 (», то с чужими байтами на конце. Это переполнение буфера в самом
    dwg2dxf на длинных MTEXT с кодами `\\U+`, и означает оно, что markdown
    одного и того же чертежа от прогона к прогону разный.

    Проверка платная — второй проход конвертера, — поэтому включается флагом.
    """
    import ezdxf

    from dwg_sheets import to_dxf

    def texts(dxf: Path) -> list[str]:
        doc = ezdxf.readfile(str(dxf))
        spaces = [doc.modelspace()]
        spaces += [doc.layouts.get(n) for n in doc.layouts.names() if n != "Model"]
        spaces += list(doc.blocks)
        out = []
        for space in spaces:
            for entity in space:
                if entity.dxftype() == "MTEXT":
                    out.append(entity.text)
                elif entity.dxftype() in ("TEXT", "ATTRIB"):
                    out.append(entity.dxf.get("text", "") or "")
        return sorted(out)

    if path.suffix.lower() != ".dwg":
        return []
    try:
        first, second = set(texts(to_dxf(path))), set(texts(to_dxf(path)))
    except Exception as error:
        return [Finding(SIGNAL, "повторная конвертация не удалась", 1, str(error)[:120])]
    drift = sorted(first ^ second)
    return (
        [Finding(ERROR, "конвертация невоспроизводима", len(drift),
                 "две конвертации одного файла дали разный текст — "
                 "markdown чертежа будет разным от прогона к прогону",
                 [t[:60] for t in drift[:3]])]
        if drift
        else []
    )


def _sheet_findings(sheets) -> list[Finding]:
    """Листы, которые уйдут модели без опознавательных знаков."""
    import stamp as stamp_mod

    lost, nameless, empty, orphan = [], [], [], 0
    for sheet in sheets:
        if getattr(sheet, "flat", False):
            # Служебные листы разбора: потери и текст с отключённых слоёв.
            if "не найденный" in sheet.name:
                orphan = len(sheet.texts)
            continue
        if getattr(sheet, "lost", False):
            lost.append(sheet.name)
        elif not sheet.texts:
            empty.append(sheet.name)
        if not stamp_mod.from_dwg_sheet(sheet).code:
            nameless.append(sheet.name)
    out = []
    if orphan:
        out.append(
            Finding(SIGNAL, "подписи без листа", orphan,
                    "есть в исходном DWG, места на листе для них не нашлось")
        )
    if lost:
        out.append(
            Finding(SIGNAL, "лист потерян конвертером", len(lost),
                    "в DXF от листа ничего не осталось", lost[:3])
        )
    if empty:
        out.append(
            Finding(SIGNAL, "лист без подписей", len(empty),
                    "модель получит паспорт и пустой раздел", empty[:3])
        )
    if nameless:
        out.append(
            Finding(SIGNAL, "штамп не прочитан", len(nameless),
                    "лист уйдёт модели без шифра — привязать его к комплекту нечем",
                    nameless[:3])
        )
    return out


# ── сверка целиком ──────────────────────────────────────────────────────────


def check(path: Path, twice: bool = False) -> list[Finding]:
    """Все находки по чертежу, самые тяжёлые первыми."""
    from dwg_sheets import sheets_for

    dxf, sheets = sheets_for(path)
    findings = (
        _field_findings(dxf, sheets)
        + _junk_findings(sheets)
        + _number_findings(sheets)
        + _cut_findings(sheets)
        + _sheet_findings(sheets)
        + (_stable_findings(path) if twice else [])
    )
    return sorted(findings, key=lambda f: (f.level != ERROR, -f.count))


def _printable(text: str) -> str:
    """Пример находки, который переживёт запись в файл.

    Сверка ищет в том числе одинокие суррогаты — половинки символов UTF-16,
    которые в UTF-8 не кодируются. Показать такой пример «как есть» значит
    уронить саму сверку на первом же битом чертеже.
    """
    return "".join("�" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)


def report(path: Path, findings: list[Finding]) -> str:
    lines = [f"## {_printable(path.name)}"]
    if not findings:
        return "\n".join(lines + ["", "разбор чист", ""])
    lines += ["", "| Уровень | Находка | Сколько | Чем плохо |", "|---|---|---:|---|"]
    for f in findings:
        lines.append(f"| {f.level} | {f.kind} | {f.count} | {f.note} |")
    for f in findings:
        for sample in f.examples:
            lines.append(f"  - {f.kind}: `{_printable(sample)}`")
    return "\n".join(lines + [""])


def _targets(argv: list[str]) -> list[Path]:
    out: list[Path] = []
    for arg in argv:
        p = Path(arg)
        out += sorted(p.rglob("*.dwg")) if p.is_dir() else [p]
    return out


def main(argv: list[str]) -> int:
    baseline_path = None
    if "--baseline" in argv:
        index = argv.index("--baseline")
        baseline_path = Path(argv[index + 1])
        argv = argv[:index] + argv[index + 2 :]
    twice = "--twice" in argv
    argv = [a for a in argv if a != "--twice"]
    targets = _targets(argv)
    if not targets:
        print(__doc__)
        return 2

    totals: dict[str, int] = {}
    errors = 0
    for path in targets:
        try:
            findings = check(path, twice)
        except Exception as error:
            findings = [Finding(ERROR, "разбор упал", 1, f"{type(error).__name__}: {error}")]
        print(report(path, findings))
        for f in findings:
            totals[f.kind] = totals.get(f.kind, 0) + f.count
            errors += f.count if f.level == ERROR else 0

    print(f"## Итого по {len(targets)} чертежам\n")
    for kind, count in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"- {kind}: {count}")

    if baseline_path:
        # В слепке лежит и число чертежей: сравнивать находки по десяти файлам
        # со слепком по девяноста бессмысленно, а выглядит это как «стало
        # лучше». Расхождение называем прямо и не сравниваем.
        snapshot = {"чертежей": len(targets), "находки": totals}
        if baseline_path.exists():
            was = json.loads(baseline_path.read_text(encoding="utf-8"))
            before = was.get("находки", was)
            if was.get("чертежей") not in (None, len(targets)):
                print(
                    f"\nСлепок `{baseline_path.name}` снят по {was['чертежей']} "
                    f"чертежам, а проверено {len(targets)} — не сравниваю."
                )
            elif worse := {
                k: (before.get(k, 0), v) for k, v in totals.items() if v > before.get(k, 0)
            }:
                print("\n**Стало хуже, чем в слепке:**\n")
                for kind, (was_count, now) in worse.items():
                    print(f"- {kind}: было {was_count}, стало {now}")
                return 1
            else:
                print(f"\nНе хуже слепка `{baseline_path.name}`.")
        else:
            baseline_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\nСлепок записан: `{baseline_path}`.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
