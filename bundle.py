"""Один отчёт из нескольких источников: раздел, собранный из DWG и PDF.

По разделу проекта приходит не один файл. Графика лежит в десятке DWG
(«Планы», «Разрезы», «Фундаменты_1-5», «Фундаменты_6-7»), текстовая часть —
отдельным PDF, а сверх того заказчик часто присылает весь альбом ещё раз в
PDF, отпечатанный из тех же чертежей. Инженеру нужен один документ по разделу,
а не десять несвязанных прогонов, в которых лист «22» встречается трижды.

Что делает этот модуль:

* собирает листы всех источников и раскладывает их по разделам — по шифру из
  основной надписи (`stamp.py`), а не по именам файлов;
* внутри раздела ставит листы в порядке комплекта, по номеру из надписи, а не
  по порядку страниц в файле;
* находит листы, пришедшие из двух источников сразу, и сводит их в один.

**Основа — DWG, но выбора между источниками нет.** Когда один лист есть и в
чертеже, и в PDF, за основу берётся версия из DWG: там текст лежит данными и
читается дословно, тогда как в PDF он приходит через отрисовку и
восстанавливается — по глифам, а на сканах вообще моделью. Но версия из PDF
не отбрасывается: из неё добираются строки, таблицы и описание листа, которых
в чертеже нет (`fuse.py`). Чертёж приходит без внешних ссылок, и у листов,
собранных из xref, в нём нет вообще ничего, кроме рамки со штампом, — тогда
как в альбоме этот лист отпечатан целиком.

Расхождения реквизитов при этом никуда не деваются: несовпадение наименований
у одного и того же листа обычно значит, что альбом печатали с другой редакции
чертежа, и это как раз то, что проверяющий обязан заметить.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import stamp as stamp_mod

VECTOR_SUFFIXES = {".dwg", ".dxf"}


@dataclass
class Variant:
    """Один лист, как он пришёл из одного конкретного файла."""

    source_name: str      # имя файла-источника
    source_kind: str      # «DWG» или «PDF»
    index: int            # порядковый номер листа в файле, 1..N
    stamp: stamp_mod.Stamp
    job_id: str = ""      # прогон, в котором лист посчитан
    run_dir: str = ""     # где лежат готовые страницы прогона
    source_path: str = "" # сам файл: по нему досчитывается то, чего нет в прогоне

    @property
    def is_vector(self) -> bool:
        return self.source_kind == "DWG"

    def body_path(self) -> Path | None:
        """Файл с готовым содержимым листа, если прогон уже сделан."""
        if not self.run_dir:
            return None
        path = Path(self.run_dir) / "pages" / f"page_{self.index:04d}.md"
        return path if path.exists() else None

    def body(self) -> str:
        path = self.body_path()
        if path is None:
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def objects(self) -> list[dict]:
        """Карта «строка текста → объекты чертежа», если она посчитана.

        Есть только у листов чертежа: у страницы PDF объектов нет, там адрес
        места — сама страница. Кладётся рядом с листом при разборе
        (`service.pipeline._save_objects`).
        """
        if not self.run_dir or not self.is_vector:
            return []
        path = Path(self.run_dir) / "pages" / f"page_{self.index:04d}.objects.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("lines", [])
        except (OSError, ValueError):
            pass
        # Прогон сделан до появления карты — досчитываем и кладём рядом, чтобы
        # второй раз не считать. Разбор листа стоит секунды, а без карты
        # подсветка просто молча не работает, и понять это по интерфейсу нельзя.
        if not self.source_path:
            return []
        try:
            from dwg_sheets import sheet_objects

            lines = sheet_objects(Path(self.source_path), self.index)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"lines": lines}, ensure_ascii=False), encoding="utf-8"
            )
            return lines
        except Exception:
            return []


@dataclass
class SheetEntry:
    """Лист комплекта — один, даже если пришёл из нескольких файлов."""

    number: str
    variants: list[Variant] = field(default_factory=list)

    @property
    def main(self) -> Variant:
        """Источник истины: чертёж, если он есть, иначе PDF.

        При равных условиях берём тот вариант, у которого прочиталась
        основная надпись: пустой паспорт означает, что лист разобран хуже.
        """
        return min(
            self.variants,
            key=lambda v: (0 if v.is_vector else 1, 0 if v.stamp.found else 1, v.index),
        )

    @property
    def others(self) -> list[Variant]:
        main = self.main
        return [v for v in self.variants if v is not main]

    @property
    def title(self) -> str:
        for variant in [self.main] + self.others:
            if variant.stamp.title:
                return variant.stamp.title
        return ""

    @property
    def sheet_id(self) -> str:
        """Чем лист адресуется снаружи: шифр документа плюс его номер.

        Косую черту шифра («28-ХСА-1/25-КР1») меняем на подчёркивание: id
        живёт в пути URL, а косая там делит сегменты. Номер листа сам по себе
        не годится — лист «19» есть и в чертежах, и в пояснительной записке.
        """
        main = self.main
        code = main.stamp.section or main.stamp.code
        if code and self.number:
            return f"{_slug(code)}:{self.number}"
        # Без шифра или без номера лист адресуется тем единственным, что у него
        # есть, — файлом и страницей в нём. Так адрес остаётся однозначным,
        # даже когда в комплекте два таких листа: обложка и титул шифра не
        # несут, и по «без-шифра:1» они оба отзывались бы одинаково.
        return f"{_slug(Path(main.source_name).stem)}#{main.index}"

    def match(self) -> dict | None:
        """Счётная сверка источников листа. None — сверять не с чем."""
        import fuse as fuse_mod

        vector = next((v for v in self.variants if v.is_vector), None)
        raster = next((v for v in self.variants if not v.is_vector), None)
        if vector is None or raster is None:
            return None
        first, second = vector.body(), raster.body()
        if not (first.strip() and second.strip()):
            return None
        return fuse_mod.compare(first, second)

    def verified_by(self) -> list[str]:
        """Какие источники подтвердили лист: у каких есть готовое содержимое."""
        return sorted({v.source_kind for v in self.variants if v.body().strip()})

    def fused_body(self) -> str:
        """Содержимое листа, сведённое из всех его источников.

        Раньше здесь выбирался один вариант — из чертежа, — а версия из PDF
        отбрасывалась целиком вместе со всем, чего в чертеже не было: у листов,
        собранных из внешних ссылок, это всё содержимое. Теперь основа —
        чертёж, а из PDF добираются недостающие строки, таблицы и описание
        листа (см. `fuse.py`).
        """
        import fuse as fuse_mod

        main = self.main
        body = _strip_page_heading(main.body())
        if not main.is_vector:
            # Основа не векторная — сводить не с чем: два прочтения одного
            # растра не дополняют друг друга, а повторяют.
            return body
        for other in self.others:
            if other.is_vector:
                continue
            second = _strip_page_heading(other.body())
            if second.strip():
                body = fuse_mod.fuse(body, second, raster_name=other.source_name)
        found = self.conflicts()
        if found:
            # Расхождение показываем прямо в листе, а не только сводкой по
            # документу: читающий лист должен видеть, что источники описывают
            # его по-разному, не уходя в другой раздел отчёта.
            body = body.rstrip() + "\n\n" + "\n".join(
                [f"##### {fuse_mod.SRC_DIFF} Расхождения между источниками", ""]
                + [
                    f"- {label}: в чертеже «{left}», в «{who}» — «{right}»"
                    for label, left, right, who in found
                ]
                + [""]
            )
        return body

    def conflicts(self) -> list[tuple[str, str, str, str]]:
        """Расхождения между источниками: (поле, у главного, у другого, чей).

        Сравниваем только то, что инженер сверяет глазами: наименование листа
        и стадию. Разница в числе текстовых объектов или в порядке блоков —
        следствие способа чтения, а не разных редакций документа.
        """
        main = self.main
        found: list[tuple[str, str, str, str]] = []
        for other in self.others:
            for label, left, right in (
                ("наименование листа", main.stamp.title, other.stamp.title),
                ("стадия", main.stamp.stage, other.stamp.stage),
            ):
                if left and right and _norm_title(left) != _norm_title(right):
                    found.append((label, left, right, other.source_name))
        return found


@dataclass
class Document:
    """Документ раздела: полный шифр и его собственная нумерация листов.

    Раздел состоит из нескольких документов, и лист «19» в каждом свой:
    у `28-ХСА-1/25-КР1` это девятнадцатый лист чертежей, у
    `28-ХСА-1/25-КР1.ПЗ` — девятнадцатая страница пояснительной записки.
    Сводить их по номеру нельзя, поэтому листы живут внутри документа, а не
    прямо в разделе.
    """

    code: str
    sheets: list[SheetEntry] = field(default_factory=list)
    # Листы, у которых номер не прочитался: они не выпадают из отчёта, а идут
    # в конец с пометкой. Молча терять лист нельзя — по нему пишут замечания.
    unnumbered: list[Variant] = field(default_factory=list)

    @property
    def is_text_part(self) -> bool:
        low = self.code.lower()
        return any(low.endswith(s) for s in stamp_mod._TEXT_PART_SUFFIXES)

    @property
    def kind(self) -> str:
        return "текстовая часть" if self.is_text_part else "графическая часть"

    @property
    def sources(self) -> list[str]:
        names: list[str] = []
        for variant in self.variants:
            if variant.source_name not in names:
                names.append(variant.source_name)
        return names

    @property
    def variants(self) -> list[Variant]:
        out: list[Variant] = []
        for entry in self.sheets:
            out += entry.variants
        return out + self.unnumbered


@dataclass
class Section:
    """Раздел комплекта: шифр без марки документа и всё, что к нему относится."""

    code: str
    object_name: str = ""
    org: str = ""
    stage: str = ""
    documents: list[Document] = field(default_factory=list)

    @property
    def sheet_count(self) -> int:
        return sum(len(d.sheets) for d in self.documents)

    @property
    def unnumbered_count(self) -> int:
        return sum(len(d.unnumbered) for d in self.documents)

    @property
    def total_count(self) -> int:
        """Все листы раздела, включая те, у которых не прочитан номер.

        В сводной таблице считаем именно их: раздел из одного листа без
        основной надписи иначе показывался бы пустым, и лист терялся из виду
        ровно в том случае, когда разобраться в нём важнее всего.
        """
        return self.sheet_count + self.unnumbered_count

    @property
    def sources(self) -> list[str]:
        names: list[str] = []
        for document in self.documents:
            for name in document.sources:
                if name not in names:
                    names.append(name)
        return names


def _slug(text: str) -> str:
    """Кусок id, годный для пути URL: без косых, пробелов и точек в конце."""
    out = re.sub(r"[\/\s]+", "_", (text or "").strip())
    return re.sub(r"[?#%]+", "", out).strip("_.") or "без-имени"


def _norm_title(text: str) -> str:
    """Наименование к сравнимому виду: регистр, пробелы и кавычки не в счёт."""
    low = text.strip().lower().replace("ё", "е")
    low = re.sub(r"[«»\"'`]+", "", low)
    return re.sub(r"\s+", " ", low).strip(" .")


def _sheet_sort_key(number: str) -> tuple[int, float, str]:
    """Порядок листов: числовые по возрастанию, буквенные — следом."""
    match = re.match(r"^(\d+)", number.strip())
    if match:
        return (0, float(match.group(1)), number)
    return (1, 0.0, number)


# ── чтение источников ───────────────────────────────────────────────────────


def read_source(
    path: Path,
    *,
    name: str = "",
    run_dir: str = "",
    job_id: str = "",
    limit: int | None = None,
) -> list[Variant]:
    """Основные надписи всех листов одного файла.

    Модель не вызывается: и в чертеже, и в PDF с текстовым слоем реквизиты
    лежат данными. У сканированного PDF надпись прочитается пустой — лист
    попадёт в отчёт без номера, а не пропадёт.

    `name` — как файл называть в отчёте. Сервис хранит загруженные документы
    под случайным именем (`3c3246bf-….pdf`), и без исходного имени отчёт
    получается нечитаемым.
    """
    display = name or path.name
    suffix = path.suffix.lower()
    if suffix in VECTOR_SUFFIXES:
        return _read_vector(
            path, name=display, run_dir=run_dir, job_id=job_id, limit=limit
        )
    return _read_pdf(path, name=display, run_dir=run_dir, job_id=job_id, limit=limit)


def _read_vector(
    path: Path, *, name: str, run_dir: str, job_id: str, limit: int | None
) -> list[Variant]:
    from dwg_sheets import sheets_for

    _, sheets = sheets_for(path)
    out: list[Variant] = []
    for index, sheet in enumerate(sheets[: limit or len(sheets)], start=1):
        out.append(
            Variant(
                source_name=name,
                source_kind="DWG",
                index=index,
                source_path=str(path),
                stamp=stamp_mod.from_dwg_sheet(sheet),
                job_id=job_id,
                run_dir=run_dir,
            )
        )
    return out


def _read_pdf(
    path: Path, *, name: str, run_dir: str, job_id: str, limit: int | None
) -> list[Variant]:
    import fitz

    out: list[Variant] = []
    with fitz.open(path) as doc:
        try:
            from deglyph import map_for_doc

            glyph_map = map_for_doc(doc, quiet=True)
        except Exception:
            glyph_map = {}
        total = doc.page_count if limit is None else min(limit, doc.page_count)
        for index in range(1, total + 1):
            out.append(
                Variant(
                    source_name=name,
                    source_kind="PDF",
                    index=index,
                    source_path=str(path),
                    stamp=_pdf_stamp(doc[index - 1], glyph_map, run_dir, index),
                    job_id=job_id,
                    run_dir=run_dir,
                )
            )
    return out


# Обозначение документа в тексте разобранного листа: «28-ХСА-1/25-КР1»,
# «28-ХСА-1/25-ПЗ.С». Пробелов внутри быть не может — иначе под шаблон
# попадает обычное название листа (см. `stamp._looks_like_code`).
_CODE_IN_TEXT = re.compile(r"\b\d{2,}-[А-ЯA-Z]{2,}-[\w./-]*\d[\w./-]*", re.UNICODE)

# «- лист: 3» и «- лист: 3 из 12» из паспорта листа.
_SHEET_IN_TEXT = re.compile(r"(?m)^-\s*лист:\s*(\d+)")


def _pdf_stamp(page, glyph_map: dict, run_dir: str, index: int) -> stamp_mod.Stamp:
    """Основная надпись страницы PDF, а если её негде прочитать — из разбора.

    У печатного альбома текстового слоя нет вовсе: у ПЗ комплекта «Жуковский»
    во всех восьми страницах ноль знаков текста. Читать надпись не из чего, и
    лист остаётся без шифра — то есть не связывается со своим двойником из
    чертежа, и сведение источников не происходит именно там, где оно нужнее
    всего.

    Поэтому у страниц без слоя шифр берётся из готового разбора листа: к тому
    времени по картинке уже прошлась модель, и обозначение стоит в тексте.
    Разбор для этого не запускается — берём только то, что уже посчитано.
    """
    found = stamp_mod.from_pdf_page(page, glyph_map=glyph_map)
    if found.code or not run_dir:
        return found
    body = Variant("", "PDF", index, found, run_dir=run_dir).body()
    code, number = code_from_body(body)
    if not code:
        return found
    found.code = code
    if number:
        found.sheet = number
    found.note = (
        "шифр взят из разобранного листа: текстового слоя у страницы нет"
    )
    return found


def code_from_body(body: str) -> tuple[str, str]:
    """Обозначение документа и номер листа из готового разбора листа.

    Кандидат на шифр проверяем тем же правилом, каким он опознаётся в самой
    надписи (`stamp._looks_like_code`): два похожих шаблона разъедутся при
    первой же правке одного из них, и в отчёт поедет «ГОСТ 14098-2014».
    """
    code = next(
        (
            match.group(0)
            for match in _CODE_IN_TEXT.finditer(body or "")
            if stamp_mod._looks_like_code(match.group(0))
        ),
        "",
    )
    if not code:
        return "", ""
    number = _SHEET_IN_TEXT.search(body or "")
    return code, number.group(1) if number else ""


# ── сведение ────────────────────────────────────────────────────────────────


def build(variants: list[Variant]) -> list[Section]:
    """Разложить листы всех источников по разделам и документам комплекта."""
    by_code: dict[str, Section] = {}
    for variant in variants:
        section_code = variant.stamp.section or "(без шифра)"
        section = by_code.get(section_code)
        if section is None:
            section = Section(code=section_code)
            by_code[section_code] = section
        _fill_section_head(section, variant)

        document_code = variant.stamp.code.strip() or section_code
        document = next(
            (d for d in section.documents if d.code == document_code), None
        )
        if document is None:
            document = Document(code=document_code)
            section.documents.append(document)

        number = variant.stamp.sheet.strip()
        if not number:
            document.unnumbered.append(variant)
            continue
        entry = next((e for e in document.sheets if e.number == number), None)
        if entry is None:
            entry = SheetEntry(number=number)
            document.sheets.append(entry)
        entry.variants.append(variant)

    for section in by_code.values():
        for document in section.documents:
            document.sheets.sort(key=lambda e: _sheet_sort_key(e.number))
        # Графическая часть впереди текстовой: так собран и сам комплект.
        section.documents.sort(key=lambda d: (d.is_text_part, d.code))
    # Разделы по порядку: сначала те, где шифр прочитан, «(без шифра)» в конец.
    return sorted(
        by_code.values(), key=lambda s: (s.code.startswith("("), s.code)
    )


def _fill_section_head(section: Section, variant: Variant) -> None:
    stamp = variant.stamp
    # Шапку раздела заполняем по первому источнику, который её знает, но
    # чертёж имеет приоритет: в PDF-альбоме объект бывает написан сокращённо.
    prefer = variant.is_vector
    for attr, value in (
        ("object_name", stamp.object_name),
        ("org", stamp.org),
        ("stage", stamp.stage),
    ):
        if value and (prefer or not getattr(section, attr)):
            if prefer and getattr(section, attr) and not variant.is_vector:
                continue
            setattr(section, attr, value)


# ── отчёт ───────────────────────────────────────────────────────────────────


def _sources_line(entry: SheetEntry) -> str:
    """Откуда взят лист. Источников бывает несколько — называем все."""
    listed = [
        f"{v.source_kind} «{v.source_name}», лист {v.index}"
        for v in [entry.main] + entry.others
    ]
    if len(listed) == 1:
        return "Источник: " + listed[0]
    return "Сведён из источников: " + "; ".join(listed)


def _strip_page_heading(body: str) -> str:
    """Убирает «## Страница N» из готового листа: нумерация здесь своя."""
    lines = body.splitlines()
    if lines and lines[0].startswith("## Страница"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def contents_markdown(document: Document) -> str:
    """Состав документа: лист, наименование, откуда взят."""
    rows = [
        "| Лист | Наименование | Основа | Добрано из |",
        "|---:|---|---|---|",
    ]
    for entry in document.sheets:
        main = entry.main
        others = ", ".join(f"{v.source_name} (лист {v.index})" for v in entry.others)
        rows.append(
            f"| {entry.number} | {entry.title or '—'} | "
            f"{main.source_kind}: {main.source_name} (лист {main.index}) | "
            f"{others or '—'} |"
        )
    for variant in document.unnumbered:
        rows.append(
            f"| — | {variant.stamp.title or 'номер листа не прочитан'} | "
            f"{variant.source_kind}: {variant.source_name} "
            f"(лист {variant.index}) | — |"
        )
    return "\n".join(rows)


def duplicates_markdown(document: Document) -> str:
    """Листы, пришедшие сразу из нескольких файлов. Пусто — таких нет."""
    rows = ["| Лист | Источники |", "|---:|---|"]
    found = 0
    for entry in document.sheets:
        if len(entry.variants) < 2:
            continue
        found += 1
        listed = ", ".join(
            f"{v.source_kind} {v.source_name} (лист {v.index})" for v in entry.variants
        )
        rows.append(f"| {entry.number} | {listed} |")
    return "\n".join(rows) if found else ""


def conflicts_markdown(document: Document) -> str:
    """Расхождения между источниками. Пусто — значит источники сошлись."""
    rows = [
        "| Лист | Что различается | Принято (истина) | В другом источнике |",
        "|---:|---|---|---|",
    ]
    found = 0
    for entry in document.sheets:
        for label, left, right, source in entry.conflicts():
            found += 1
            rows.append(f"| {entry.number} | {label} | {left} | {right} — {source} |")
    return "\n".join(rows) if found else ""


def document_markdown(document: Document, *, with_bodies: bool = True) -> list[str]:
    """Документ раздела: состав, расхождения и сами листы."""
    parts = [f"## {document.code} — {document.kind}", ""]
    facts = [f"листов {len(document.sheets)}"]
    if document.unnumbered:
        facts.append(f"без номера {len(document.unnumbered)}")
    facts.append("источники: " + ", ".join(document.sources))
    parts += ["_" + ", ".join(facts) + "._", "", contents_markdown(document), ""]

    duplicates = duplicates_markdown(document)
    if duplicates:
        parts += [
            "### Листы из нескольких источников",
            "",
            "_Один лист прислан дважды. В отчёт идёт не одна из версий, а обе "
            "сразу: основа из чертежа, недостающее добрано из PDF._",
            "",
            duplicates,
            "",
        ]
    conflicts = conflicts_markdown(document)
    if conflicts:
        parts += [
            "### Расхождения между источниками",
            "",
            "_Один и тот же лист описан в источниках по-разному. Обычно это "
            "значит, что альбом печатали с другой редакции чертежа._",
            "",
            conflicts,
            "",
        ]

    if not with_bodies:
        return parts

    parts += ["### Листы", ""]
    unprocessed = "_Лист ещё не обработан конвейером._"
    for entry in document.sheets:
        main = entry.main
        parts += [
            f"#### Лист {entry.number}. {entry.title or 'без наименования'}",
            "",
            f"_{_sources_line(entry)}._",
            "",
        ]
        body = entry.fused_body()
        parts += [body if body else unprocessed, ""]
    for variant in document.unnumbered:
        parts += [
            f"#### Лист без номера ({variant.source_name}, лист {variant.index})",
            "",
        ]
        body = variant.body()
        parts += [_strip_page_heading(body) if body else unprocessed, ""]
    return parts


def section_markdown(section: Section, *, with_bodies: bool = True) -> str:
    """Раздел одним документом: шапка, потом каждый документ раздела."""
    parts = [f"# Раздел {section.code}", ""]
    if section.object_name:
        parts += [f"**Объект:** {section.object_name}", ""]
    facts = []
    if section.stage:
        facts.append(f"стадия {section.stage}")
    facts += [f"документов {len(section.documents)}", f"листов {section.sheet_count}"]
    if section.unnumbered_count:
        facts.append(f"без номера {section.unnumbered_count}")
    facts.append(f"файлов {len(section.sources)}")
    parts += ["_" + ", ".join(facts) + "._", ""]
    if section.org:
        parts += [f"Проектная организация: {section.org}", ""]
    parts += [
        "Отчёт сведён из нескольких файлов. Где лист есть и в чертеже, и в PDF, "
        "источники сложены: основа из DWG — там текст лежит данными, — а из "
        "PDF добраны строки, таблицы и описание листа, которых в чертеже нет. "
        "У каждого такого листа есть счётная сверка источников.",
        "",
    ]
    for document in section.documents:
        parts += document_markdown(document, with_bodies=with_bodies)
    return "\n".join(parts).rstrip() + "\n"


def report_markdown(sections: list[Section], *, with_bodies: bool = True) -> str:
    """Все разделы одним файлом."""
    if not sections:
        return "# Сводный отчёт\n\n_Источников нет._\n"
    if len(sections) == 1:
        return section_markdown(sections[0], with_bodies=with_bodies)
    parts = [
        "# Сводный отчёт по комплекту",
        "",
        f"_Разделов: {len(sections)}._",
        "",
        "| Раздел | Документов | Листов | Файлов |",
        "|---|---:|---:|---:|",
    ]
    for section in sections:
        parts.append(
            f"| {section.code} | {len(section.documents)} | "
            f"{section.total_count} | {len(section.sources)} |"
        )
    parts.append("")
    for section in sections:
        parts += [section_markdown(section, with_bodies=with_bodies), ""]
    return "\n".join(parts).rstrip() + "\n"


def _variant_dict(variant: Variant) -> dict:
    return {
        "source": variant.source_name,
        "kind": variant.source_kind,
        "index": variant.index,
        "jobId": variant.job_id,
    }


def sections_dict(sections: list[Section]) -> list[dict]:
    """То же для интерфейса: состав разделов без текстов листов."""
    return [
        {
            "code": section.code,
            "objectName": section.object_name,
            "org": section.org,
            "stage": section.stage,
            "sources": section.sources,
            "documents": [
                {
                    "code": document.code,
                    "kind": document.kind,
                    "sources": document.sources,
                    "sheets": [
                        {
                            "sheetId": entry.sheet_id,
                            "number": entry.number,
                            "title": entry.title,
                            "main": _variant_dict(entry.main),
                            "alsoIn": [_variant_dict(v) for v in entry.others],
                            "conflicts": [
                                {
                                    "field": label,
                                    "accepted": left,
                                    "other": right,
                                    "source": source,
                                }
                                for label, left, right, source in entry.conflicts()
                            ],
                        }
                        for entry in document.sheets
                    ],
                    "unnumbered": [
                        {**_variant_dict(v), "title": v.stamp.title}
                        for v in document.unnumbered
                    ],
                }
                for document in section.documents
            ],
        }
        for section in sections
    ]



# ── сведённый лист для интерфейса ───────────────────────────────────────────


def sheet_dict(entry: SheetEntry, document: Document, *, with_body: bool = False) -> dict:
    """Лист комплекта полями: откуда взят, чем подтверждён, из чего состоит.

    Содержимое отдаём по запросу: в списке листов раздела оно не нужно, а у
    стройгенплана один лист весит сотни килобайт.
    """
    import fuse as fuse_mod

    main = entry.main
    out = {
        "sheetId": entry.sheet_id,
        "number": entry.number,
        "title": entry.title,
        "code": document.code,
        "kind": document.kind,
        "primarySource": main.source_kind,
        "variants": [_variant_dict(v) for v in [main] + entry.others],
        "verifiedBy": entry.verified_by(),
        "match": entry.match(),
        "conflicts": [
            {"field": label, "primary": left, "other": right, "source": who}
            for label, left, right, who in entry.conflicts()
        ],
        "ready": bool(main.body().strip()),
    }
    if with_body:
        body = entry.fused_body()
        out["markdown"] = body
        out["blocks"] = fuse_mod.blocks(body, base=main.source_kind)
        # Связь строки текста с объектом на чертеже. Номер тот же, что в
        # колонке `id` геометрии листа, — по нему интерфейс подсвечивает
        # подпись, когда инженер ткнул в строку.
        out["objects"] = main.objects()
    return out


def sheets_dict(sections: list[Section], *, with_bodies: bool = False) -> list[dict]:
    """Все листы комплекта подряд, в порядке разделов и документов.

    Листы, у которых не прочитался номер, идут наравне с остальными, в конце
    своего документа. Молча терять лист нельзя — по нему пишут замечания, а
    без номера чаще всего оказываются обложка и титул, которые инженер как раз
    и открывает первыми.
    """
    out: list[dict] = []
    for section in sections:
        for document in section.documents:
            for entry in document.sheets:
                out.append(sheet_dict(entry, document, with_body=with_bodies))
            for variant in document.unnumbered:
                out.append(
                    sheet_dict(
                        SheetEntry(number="", variants=[variant]),
                        document,
                        with_body=with_bodies,
                    )
                )
    return out


def find_sheet(sections: list[Section], sheet_id: str) -> tuple[SheetEntry, Document] | None:
    """Лист по его id. None — такого листа в комплекте нет."""
    for section in sections:
        for document in section.documents:
            for entry in document.sheets:
                if entry.sheet_id == sheet_id:
                    return entry, document
            for variant in document.unnumbered:
                entry = SheetEntry(number="", variants=[variant])
                if entry.sheet_id == sheet_id:
                    return entry, document
    return None
