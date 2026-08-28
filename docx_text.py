"""Пояснительная записка из .docx — текстом и таблицами, без модели.

Зачем. Комплект приходит не только чертежами: текстовая часть каждого раздела
(«Пояснительная записка») — это .docx, и в комплекте «Жуковский» таких файлов
шесть. Раньше сервис их не принимал вовсе, то есть половина раздела просто не
доезжала до сверки с ТЗ.

Читается так же, как чертёж: **данными, а не картинкой**. .docx — это zip с
XML внутри, поэтому ни модель, ни сторонняя библиотека здесь не нужны:
достаточно `zipfile` и разбора дерева. Ошибиться в цифре при таком чтении
невозможно — она берётся из файла дословно.

Что читается:

* **абзацы** в порядке документа;
* **таблицы** — в GFM, с объединёнными ячейками (`w:gridSpan`, `w:vMerge`) и
  переносами строк внутри ячейки. Для записки это главное: расчётные таблицы
  и спецификации несут те числа, ради которых всё затевалось;
* **основная надпись из колонтитулов**. Word держит рамку листа в
  `word/header*.xml` и `footer*.xml`, и там же лежит шифр документа
  (`28-ХСА-1/25-КР1.ПЗ`), организация и вид части. По шифру записка сама
  встаёт в свой раздел при сведении комплекта (`bundle.py`) — рядом с
  чертежами того же раздела.

Чего здесь нет и не будет:

* **старый бинарный `.doc`** (OLE2) — другой формат, читается только
  внешним конвертером. Такой файл сервис отклоняет с просьбой пересохранить
  в .docx, а не пытается угадать содержимое.
* **картинки** — в записках это схемы и фотографии; их содержимое в текст не
  попадает, о чём лист прямо пишет.

Разбиение на листы. В .docx страниц нет: разбивка появляется только при
печати, и в записках комплекта «Жуковский» разрывов страниц нет ни одного.
Поэтому документ отдаётся **одним листом**, если в нём нет явных разрывов
(`w:br w:type="page"`, `w:pageBreakBefore`), и по этим разрывам — если они
есть. Выдумывать страницы там, где их нет в файле, нельзя: пропуск лучше
галлюцинации, а придуманная нумерация — та же галлюцинация, только про
структуру.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

SUFFIXES = {".docx"}
# Старый бинарный формат Word. Ловим отдельно, чтобы сказать внятное.
LEGACY_SUFFIXES = {".doc"}


def is_docx(path: Path) -> bool:
    return path.suffix.lower() in SUFFIXES


def is_legacy_doc(path: Path) -> bool:
    return path.suffix.lower() in LEGACY_SUFFIXES


@dataclass
class Block:
    """Кусок документа в порядке чтения."""

    kind: str  # "heading" | "text" | "table"
    text: str
    rows: int = 0
    columns: int = 0


@dataclass
class DocxSheet:
    """Лист записки: блоки плюс то, что о нём известно из файла."""

    number: int
    blocks: list[Block] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)

    @property
    def tables(self) -> int:
        return sum(1 for b in self.blocks if b.kind == "table")


# ── текст ────────────────────────────────────────────────────────────────────


def _runs_text(node: ET.Element) -> str:
    """Текст абзаца или ячейки: прогоны, табуляции и разрывы строк.

    Идём по дереву сами, а не собираем все `w:t` подряд: иначе теряются
    мягкие переносы внутри ячейки, а на них держится вёрстка таблиц записки.
    """
    out: list[str] = []
    for el in node.iter():
        tag = el.tag
        if tag == f"{W}t":
            out.append(el.text or "")
        elif tag == f"{W}tab":
            out.append("\t")
        elif tag == f"{W}br":
            out.append("\n")
        elif tag in (f"{W}noBreakHyphen",):
            out.append("-")
    return "".join(out)


def _is_page_break(para: ET.Element) -> bool:
    """Явный разрыв страницы: перед абзацем или внутри его прогонов."""
    props = para.find(f"{W}pPr")
    if props is not None and props.find(f"{W}pageBreakBefore") is not None:
        return True
    for br in para.iter(f"{W}br"):
        if br.get(f"{W}type") == "page":
            return True
    return False


_HEADING_STYLE_RE = re.compile(r"^(heading|заголовок)", re.I)
_NUMBERED_HEADING_RE = re.compile(r"^\d+(\.\d+)*\.?\s+\S")
_MAX_HEADING_CHARS = 120


def _is_heading(para: ET.Element, text: str) -> bool:
    props = para.find(f"{W}pPr")
    if props is not None:
        style = props.find(f"{W}pStyle")
        if style is not None and _HEADING_STYLE_RE.match(style.get(f"{W}val") or ""):
            return True
        if props.find(f"{W}outlineLvl") is not None:
            return True
    if len(text) > _MAX_HEADING_CHARS or "\n" in text:
        return False
    return bool(_NUMBERED_HEADING_RE.match(text))


# ── таблицы ──────────────────────────────────────────────────────────────────


def _cell_text(cell: ET.Element) -> str:
    parts = [_runs_text(p).strip() for p in cell.findall(f"{W}p")]
    return "\n".join(p for p in parts if p)


def _md_escape(text: str) -> str:
    """Ячейка в GFM: вертикальная черта экранируется, перенос — <br>."""
    return text.replace("|", "\\|").replace("\n", " <br> ").strip()


def _span(cell: ET.Element) -> int:
    props = cell.find(f"{W}tcPr")
    if props is None:
        return 1
    grid = props.find(f"{W}gridSpan")
    if grid is None:
        return 1
    try:
        return max(1, int(grid.get(f"{W}val") or 1))
    except ValueError:
        return 1


def _is_continued(cell: ET.Element) -> bool:
    """Ячейка — продолжение объединённой по вертикали (`w:vMerge` без val)."""
    props = cell.find(f"{W}tcPr")
    if props is None:
        return False
    merge = props.find(f"{W}vMerge")
    if merge is None:
        return False
    return (merge.get(f"{W}val") or "continue") == "continue"


def table_rows(tbl: ET.Element) -> list[list[str]]:
    """Таблица как список строк. Объединённые ячейки разворачиваются."""
    rows: list[list[str]] = []
    for tr in tbl.findall(f"{W}tr"):
        row: list[str] = []
        for tc in tr.findall(f"{W}tc"):
            text = "" if _is_continued(tc) else _cell_text(tc)
            row.append(text)
            # Объединённая по горизонтали ячейка занимает несколько колонок:
            # добиваем пустыми, чтобы столбцы не разъезжались.
            row.extend("" for _ in range(_span(tc) - 1))
        rows.append(row)
    return rows


def table_markdown(rows: list[list[str]]) -> str:
    """GFM-таблица. Пустые колонки справа убираются."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    grid = [list(r) + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(r[i].strip() for r in grid)]
    if not keep:
        return ""
    grid = [[r[i] for i in keep] for r in grid]
    head = [_md_escape(c) for c in grid[0]]
    if not any(head):
        head = [""] * len(keep)
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in keep) + "|"]
    for row in grid[1:]:
        out.append("| " + " | ".join(_md_escape(c) for c in row) + " |")
    return "\n".join(out)


# ── чтение документа ─────────────────────────────────────────────────────────


def _body(path: Path) -> ET.Element:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(f"{W}body")
    if body is None:
        raise ValueError("в document.xml нет тела документа")
    return body


def read_sheets(path: Path) -> list[DocxSheet]:
    """Листы записки. Один, если явных разрывов страницы в файле нет."""
    body = _body(path)
    sheets: list[DocxSheet] = [DocxSheet(number=1)]
    for element in body:
        if element.tag == f"{W}p":
            if _is_page_break(element) and sheets[-1].blocks:
                sheets.append(DocxSheet(number=len(sheets) + 1))
            text = _runs_text(element).strip()
            if not text:
                continue
            kind = "heading" if _is_heading(element, text) else "text"
            sheets[-1].blocks.append(Block(kind=kind, text=text))
        elif element.tag == f"{W}tbl":
            rows = table_rows(element)
            markdown = table_markdown(rows)
            if not markdown:
                continue
            sheets[-1].blocks.append(
                Block(
                    kind="table",
                    text=markdown,
                    rows=len(rows),
                    columns=max((len(r) for r in rows), default=0),
                )
            )
    return [s for s in sheets if s.blocks] or [DocxSheet(number=1)]


def sheet_count(path: Path) -> int:
    return len(read_sheets(path))


def image_count(path: Path) -> int:
    """Сколько картинок в файле: их содержимое в текст не попадает."""
    try:
        with zipfile.ZipFile(path) as archive:
            return sum(
                1
                for name in archive.namelist()
                if name.startswith("word/media/")
                and not name.endswith("/")
            )
    except (OSError, zipfile.BadZipFile):
        return 0


# ── основная надпись из колонтитулов ─────────────────────────────────────────

_PART_RE = re.compile(r"word/(?:header|footer)\d*\.xml$")

# Шифр документа: «28-ХСА-1/25-КР1.ПЗ». В колонтитуле Word он разорван на
# прогоны («28-ХСА-1/25-КР 1 .ПЗ»), поэтому ищем по строке без пробелов.
_CODE_RE = re.compile(r"\d{2,}-[А-ЯA-Zа-яa-z]{2,}-[\w./-]*\d[\w./-]*")
_ORG_RE = re.compile(r"(?:ООО|АО|ЗАО|ПАО|ИП)\s*«[^»]{2,60}»|(?:ООО|АО|ЗАО|ПАО|ИП)\s*\"[^\"]{2,60}\"")
_TEXT_PART_WORDS = ("текстовая часть", "пояснительная записка")


def _headers_text(path: Path) -> str:
    parts: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for name in sorted(archive.namelist()):
                if not _PART_RE.match(name):
                    continue
                try:
                    root = ET.fromstring(archive.read(name))
                except ET.ParseError:
                    continue
                text = " ".join(t.text or "" for t in root.iter(f"{W}t"))
                if text.strip():
                    parts.append(text)
    except (OSError, zipfile.BadZipFile):
        return ""
    return " ".join(parts)


def _trim_code(code: str) -> str:
    """Обрезает шифр по концу вида документа.

    В колонтитуле рядом с шифром лежит остаток поля («…КР1.ПЗ-ВК.ПЗ»), и
    жадный шаблон утаскивает его целиком. Режем по первому известному
    окончанию текстовой части — дальше идёт уже не шифр.
    """
    low = code.lower()
    best = len(code)
    for suffix in (".пз", ".тч", ".с", ".сп", ".гч"):
        found = low.find(suffix)
        if found > 0:
            best = min(best, found + len(suffix))
    return code[:best].rstrip(" .-")


def read_stamp(path: Path):
    """Основная надпись записки из колонтитулов. Пустая — если не нашлась."""
    import stamp as stamp_mod

    raw = _headers_text(path)
    found = stamp_mod.Stamp(source="DOCX")
    if not raw.strip():
        found.note = "колонтитулы пусты — основная надпись не прочитана"
        return found

    squeezed = re.sub(r"\s+", "", raw)
    for candidate in _CODE_RE.findall(squeezed):
        code = _trim_code(candidate)
        if stamp_mod._looks_like_code(code):
            found.code = code
            break

    org = _ORG_RE.search(raw)
    if org:
        # Word рвёт название на прогоны, и внутри кавычек появляются пробелы:
        # «ООО « КУРСКРЕГИОНПРОЕКТ »». Для сведения источников это уже другая
        # организация, поэтому склеиваем.
        name = re.sub(r"\s+", " ", org.group()).strip()
        name = re.sub(r"([«\"])\s+", r"\1", name)
        found.org = re.sub(r"\s+([»\"])", r"\1", name)

    low = raw.lower()
    for word in _TEXT_PART_WORDS:
        if word in low:
            found.title = "Текстовая часть"
            break
    if not found.code:
        found.note = "шифр в колонтитулах не найден"
    return found


# ── лист в контракте конвейера ───────────────────────────────────────────────


def sheet_markdown(
    sheet: DocxSheet, path: Path, *, total: int, images: int = 0
) -> tuple[str, str]:
    """Лист записки в том же контракте, что страница PDF и лист чертежа.

    Возвращает `(markdown, kind)`. Тип листа — `table`, если таблицы занимают
    больше половины его объёма, иначе `text`: по нему интерфейс выбирает
    подпись, а конвейер — ничего, модель здесь не участвует.
    """
    found = read_stamp(path)
    table_chars = sum(len(b.text) for b in sheet.blocks if b.kind == "table")
    kind = "table" if table_chars * 2 > max(sheet.chars, 1) else "text"

    passport = [
        f"- kind: `{kind}`",
        f"- источник: `{path.name}` (DOCX)",
        f"- обозначение: {found.code}" if found.code else "",
        f"- наименование листа: {found.title}" if found.title else "",
        f"- организация: {found.org}" if found.org else "",
        f"- листов в файле: {total}"
        + (" (разрывов страниц в файле нет — записка отдана одним листом)"
           if total == 1 else ""),
        f"- знаков текста: {sheet.chars}",
        f"- таблиц: {sheet.tables}",
        f"- основная надпись: {found.note}" if found.note else "",
    ]
    parts = [
        "### PASS-0 Паспорт листа",
        "",
        *[line for line in passport if line],
        "",
    ]
    if images:
        parts += [
            f"_В документе изображений: {images}. Это схемы и фотографии; их "
            "содержимое в текст не попадает._",
            "",
        ]
    parts += [
        "### PASS-B Текст записки (из DOCX)",
        "",
        "_Текст и таблицы взяты из файла как данные: модель не вызывалась._",
        "",
    ]
    for block in sheet.blocks:
        if block.kind == "heading":
            parts += [f"### {block.text}", ""]
        elif block.kind == "table":
            parts += [block.text, ""]
        else:
            parts += [block.text, ""]
    return "\n".join(parts).rstrip() + "\n", kind


def page_markdown(path: Path, page_number: int) -> tuple[str, str]:
    """Лист записки по номеру — аналог `dwg_sheets.page_markdown`."""
    sheets = read_sheets(path)
    if not 1 <= page_number <= len(sheets):
        raise IndexError(f"в записке {len(sheets)} листов, запрошен {page_number}")
    return sheet_markdown(
        sheets[page_number - 1], path, total=len(sheets), images=image_count(path)
    )


def main() -> int:
    import argparse
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path")
    ap.add_argument("--page", type=int, default=1)
    args = ap.parse_args()
    body, kind = page_markdown(Path(args.path), args.page)
    print(f"[{kind}]")
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
