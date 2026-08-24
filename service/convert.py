"""Перевод вывода конвейера в страницу в том виде, в каком её ждёт фронтенд.

Конвейер пишет page_NNNN.md секциями «### PASS-0 / PASS-A / PASS-B» — этот
контракт ломать нельзя, на нём держатся compare_to_etalon.py, build_ios2_md.py
и остальные инструменты. Поэтому здесь только читаем: режем секции теми же
функциями, что и build_ios2_md.py, и собираем из них один markdown на лист.

Шапка страницы намеренно содержит строку «**Файл:**». Фронтенд в
storage.ts:listDocuments() перегенерирует markdown заглушкой, если её нет,
и молча затирает результат прогона. С шапкой этот блок не срабатывает.
"""
from __future__ import annotations

import re
from pathlib import Path

import fitz  # PyMuPDF

from build_ios2_md import extract_pass, is_garbled_pdf_text, normalize_pdf_text

from service import config

# sheet_aware.SheetKind → PageKind фронтенда (types.ts)
KIND_MAP = {
    "plan": "drawing",
    "scheme": "drawing",
    "table": "table",
    "text": "text",
    "mixed": "mixed",
}

KIND_TITLE = {
    "drawing": "чертёж",
    "table": "таблица",
    "text": "текст",
    "mixed": "смешанный",
}

# Версия формата страницы. Растёт, когда меняется состав markdown — по ней
# сервис понимает, что кэш листа собран старым кодом, и пересобирает его.
PAGE_SCHEMA = 4

# Выводить ли PASS-B в markdown интерфейса. По умолчанию нет: на чертеже это
# до 100 тыс. символов на лист. Данные остаются в поле fragments и в файле
# прогона. Включается PTO_INCLUDE_FRAGMENTS=1.
INCLUDE_FRAGMENTS = config.INCLUDE_FRAGMENTS


def _count_fragments(text: str) -> int:
    """Сколько кусков листа вошло в извлечение («--- r1c1 ---», зоны)."""
    return len(re.findall(r"(?m)^\s*---\s+\S+\s+---\s*$", text)) or 1


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение после числа: 1 фрагменту, 2 фрагментам, 5 фрагментам.

    Строка попадает на глаза инженеру в интерфейсе, поэтому «по 1 фрагментам»
    здесь недопустимо.
    """
    if count % 10 == 1 and count % 100 != 11:
        return one
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many

# Паспорт печатается двумя способами: passport_markdown() даёт «- kind: `plan`»,
# context_pack() внутри промпта — «Тип листа (эвристика): plan».
# Заголовок, которым конвейер помечает лист, чей текст взят из PDF, а не
# прочитан моделью (hf_api_bench: текстовый лист с исправным слоем).
_LAYER_SOURCE_RE = re.compile(
    r"^###\s*PASS-A\s+Текст листа \(из текстового слоя PDF\)", re.M
)

_PASSPORT_KIND_RE = re.compile(
    r"^\s*[-*]?\s*kind:\s*`?([a-z]+)`?|Тип листа \(эвристика\):\s*([a-z]+)",
    re.I | re.M,
)


def kind_from_passport_md(pass_0: str) -> str | None:
    """Достаёт тип листа из PASS-0, если паспорт строился."""
    found = _PASSPORT_KIND_RE.search(pass_0 or "")
    if not found:
        return None
    raw = (found.group(1) or found.group(2) or "").strip().lower()
    return KIND_MAP.get(raw)


def kind_from_page(pdf_path: Path, page_number: int) -> str:
    """Запасной путь: классификация листа без обращения к модели."""
    try:
        from sheet_aware import build_passport

        with fitz.open(pdf_path) as doc:
            passport = build_passport(doc[page_number - 1], page_number)
        return KIND_MAP.get(passport.kind, "mixed")
    except Exception:
        return "mixed"


def page_layer_text(pdf_path: Path, page_number: int) -> str:
    """Текстовый слой листа, с починкой сломанного ToUnicode.

    У CAD-PDF слой часто отдаёт кракозябры («ǜодерǱаǸие» вместо «Содержание»).
    Раньше такие листы считались нечитаемыми и текст терялся совсем; конвейер
    умеет снимать эту порчу подстановкой, выведенной по самому документу
    (deglyph) — на ИОС2 это 95% символов и семь листов из сорока девяти.
    Если починить не удалось, отдаём пустую строку, как и раньше: битый слой
    инженеру и модели вреден.
    """
    try:
        from hf_api_bench import layer_text_for_markdown

        with fitz.open(pdf_path) as doc:
            text = layer_text_for_markdown(doc[page_number - 1])
    except Exception:
        try:
            with fitz.open(pdf_path) as doc:
                text = normalize_pdf_text(
                    doc[page_number - 1].get_text(sort=True) or ""
                )
        except Exception:
            return ""
    if not text.strip() or is_garbled_pdf_text(text):
        return ""
    return text.strip()


# Строки, которые дедупликация не трогает: разделители фрагментов
# («--- r1c1 ---»), таблицы, блоки кода и заголовки — на них держится разметка.
_KEEP_PREFIXES = ("---", "|", "```", "#", "===")


def dedupe_fragment_lines(text: str, *, min_len: int = 8) -> tuple[str, int]:
    """Убирает повторы строк между фрагментами PASS-B.

    Соседние тайлы перекрываются, поэтому одна и та же подпись приходит по
    много раз, а к ней добавляются служебные шапки промпта («1) ПРОСТРАНСТВО…»)
    — по одной на каждый из 16 фрагментов. На реальном листе это давало до 11
    копий одной строки и 80% объёма страницы.

    Дедупликация идёт по всему листу, а не по соседним строкам: штатный
    dedupe_lines() схлопывает только идущие подряд. Сырой вывод при этом не
    трогается — он остаётся в pages/page_NNNN.md.

    Возвращает (текст, сколько строк убрано).
    """
    seen: set[str] = set()
    out: list[str] = []
    removed = 0

    for line in text.splitlines():
        stripped = line.strip()
        key = re.sub(r"\s+", " ", stripped)
        if len(key) < min_len or stripped.startswith(_KEEP_PREFIXES):
            out.append(line)
            continue
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        out.append(line)

    result = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return result, removed


def build_page_markdown(
    *,
    page_number: int,
    file_name: str,
    kind: str,
    pass_0: str = "",
    pass_a: str = "",
    pass_b: str = "",
    layer_text: str = "",
    note: str = "",
    fragments_summary: str = "",
    layer_is_source: bool = False,
) -> str:
    parts = [
        f"# Лист {page_number}",
        "",
        f"**Файл:** `{file_name}`",
        "",
        f"**Тип листа:** {KIND_TITLE.get(kind, kind)}",
        "",
    ]
    if note:
        parts += [f"_{note}_", ""]
    if pass_a.strip():
        parts += ["## Описание листа", "", pass_a.strip(), ""]
    if pass_b.strip():
        clean_b, removed = dedupe_fragment_lines(pass_b)
        parts += ["## Извлечение по фрагментам", ""]
        if removed:
            parts += [
                f"_Убрано повторов между фрагментами: {removed} строк. "
                f"Полный вывод — в файле прогона._",
                "",
            ]
        parts += [clean_b, ""]
    elif fragments_summary:
        parts += ["## Извлечение по фрагментам", "", fragments_summary, ""]
    # Текст из PDF не печатаем, когда описание листа само собрано из него:
    # иначе один и тот же текст идёт на экран двумя блоками подряд.
    if layer_text.strip() and not layer_is_source:
        parts += ["## Текст с листа (из PDF, дословно)", "", layer_text.strip(), ""]
    # Паспорт (kind, размер листа, причины классификации) — служебная
    # диагностика конвейера. Инженеру она не нужна и читается как третий
    # пересказ тех же меток, поэтому уходит отдельным полем, а не в markdown.
    if not any(s.strip() for s in (pass_a, pass_b, layer_text)):
        parts += ["_С листа пока ничего не извлечено._", ""]
    return "\n".join(parts).rstrip() + "\n"


def page_to_frontend(
    *,
    page_number: int,
    file_name: str,
    raw_page_md: str,
    pdf_path: Path,
    kind_hint: str | None = None,
    include_fragments: bool = INCLUDE_FRAGMENTS,
) -> dict:
    """Собирает объект DocumentPage: {pageNumber, kind, markdown, extractedText}.

    PASS-B (извлечение по фрагментам) в markdown интерфейса по умолчанию не
    попадает: на чертеже это 16 подробных описаний, до 100 тыс. символов на
    лист — читать невозможно, а браузеру такой markdown тяжело рисовать.
    Данные не теряются: они лежат в поле fragments, в файле прогона и
    отдаются маршрутом /jobs/{id}/pages/{n}/raw.
    """
    pass_0, pass_a, pass_b = extract_pass(raw_page_md)
    layer_is_source = bool(_LAYER_SOURCE_RE.search(raw_page_md))
    kind = (
        kind_hint
        or kind_from_passport_md(pass_0)
        or kind_from_page(pdf_path, page_number)
    )
    layer_text = page_layer_text(pdf_path, page_number)

    fragments, removed = ("", 0)
    if pass_b.strip():
        fragments, removed = dedupe_fragment_lines(pass_b)

    summary = ""
    if fragments and not include_fragments:
        count = _count_fragments(fragments)
        summary = (
            f"_Извлечено {len(fragments):,} символов по {count} "
            f"{_plural(count, 'фрагменту', 'фрагментам', 'фрагментам')} листа. "
            f"В интерфейс не выводится из-за объёма — смотрите файл прогона "
            f"или `GET /jobs/{{id}}/pages/{page_number}/raw`._"
        ).replace(",", " ")

    markdown = build_page_markdown(
        page_number=page_number,
        file_name=file_name,
        kind=kind,
        pass_0=pass_0,
        pass_a=pass_a,
        pass_b=fragments if include_fragments else "",
        layer_text=layer_text,
        fragments_summary=summary,
        layer_is_source=layer_is_source,
    )
    return {
        "pageNumber": page_number,
        "kind": kind,
        "markdown": markdown,
        "extractedText": layer_text,
        # Паспорт листа: служебная классификация конвейера. Полем, а не в
        # markdown — на экране он был третьим пересказом тех же меток.
        "passport": pass_0.strip(),
        # Текст листа взят из PDF дословно, модель его не читала.
        "textFromLayer": layer_is_source,
        # Полное извлечение по фрагментам — для клиентов, которым нужна
        # каждая марка (сверка с ТЗ), а не читаемость.
        "fragments": fragments,
        "fragmentsRemovedLines": removed,
        "schema": PAGE_SCHEMA,
    }
