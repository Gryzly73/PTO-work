"""Сведение одного листа из двух источников: чертежа и PDF.

Зачем. Один и тот же лист приходит дважды — в DWG и в альбоме PDF, — и это
не дубль, а два неполных чтения одного документа. Полнота у них разная, и
разная не случайно:

* **В чертеже текст лежит данными.** Он точен до знака, у него есть
  координаты, а таблицы собираются из настоящей сетки линий. Но чертёж
  приходит без внешних ссылок: весь ПЗУ комплекта «Жуковский» собран из
  xref-файлов, которых заказчик не присылает, и в разборе от листа остаются
  рамка со штампом — при том что в альбоме этот лист отпечатан целиком.
* **В PDF лист отпечатан целиком**, включая содержимое внешних ссылок. Зато
  текст там восстанавливается по отрисовке, а на сканах — моделью, то есть с
  ошибками узнавания. И только PDF даёт словесное описание начерченного:
  из геометрии чертежа его взять неоткуда.

`bundle.py` до сих пор выбирал: лист есть в обоих — в отчёт шла версия из
DWG, версия из PDF отбрасывалась целиком, и вместе с ней всё, чего в чертеже
не было. Здесь источники не выбираются, а складываются: основа — чертёж,
из PDF добираются строки и таблицы, которых в чертеже нет, и его описание
листа. Плюс счётная сверка: сколько подписей чертежа не нашлось в PDF и
наоборот — это то, что проверяющий и обязан заметить.

Модель здесь не вызывается. Сложение идёт по нормализованным токенам — тем
же приёмом, каким `merge_vlm_ocr.py` добирает OCR к выводу VLM: строка
попадает в добор, если несёт хоть один токен, которого нет в чертеже.
"""
from __future__ import annotations

import re

# Токен для сравнения источников: слово от двух букв или число. Знаки
# препинания и одиночные буквы выброшены — в PDF они появляются и исчезают от
# качества отрисовки, и по ним источники расходились бы всегда.
_TOKEN = re.compile(
    r"[0-9]+(?:[.,][0-9]+)?|[A-Za-zА-Яа-яЁё]{2,}(?:[-/][A-Za-zА-Яа-яЁё0-9]+)*"
)

# Заголовок раздела листа: «### PASS-B Текст листа (из DWG)».
_SECTION = re.compile(r"(?m)^###\s+(PASS-\S+)(.*)$")

# Сколько строк добора показывать. Больше — это уже не добор, а второй лист
# в отчёте: у листа-скана модель выдаёт сотни строк, и половина из них —
# пересказ того, что и так есть в чертеже.
_MAX_EXTRA = 120

# Строка короче этого — не факт, а обрывок отрисовки.
_MIN_LINE = 3

# ── метки источника ─────────────────────────────────────────────────────────
#
# Заголовки блоков подписаны по-русски — для инженера. Интерфейсу русский
# заголовок разбирать нельзя: он меняется от правки к правке. Поэтому в начале
# заголовка стоит метка, и это тот же приём, на котором уже держится разбор
# листа: все инструменты бэкенда режут markdown по «### PASS-0 / PASS-A /
# PASS-B». Метка — часть контракта, менять её нельзя.
SRC_DWG = "SRC-DWG"    # из чертежа, данными: точно до знака
SRC_PDF = "SRC-PDF"    # прочитано с картинки, чертежом не подтверждено
SRC_BOTH = "SRC-BOTH"  # счётный итог сверки двух источников
SRC_DIFF = "SRC-DIFF"  # источники описывают лист по-разному

MARKERS = (SRC_DWG, SRC_PDF, SRC_BOTH, SRC_DIFF)

# Сколько своих слов должно быть в таблице, чтобы переносить её из PDF.
# Меньше — это пересборка того, что и так есть в чертеже.
_TABLE_OWN = 3


def tokens(text: str) -> set[str]:
    """Слова и числа строки в виде, в котором источники сравниваются."""
    return {found.upper() for found in _TOKEN.findall(text or "")}


def sections(body: str) -> dict[str, str]:
    """Разделы листа по их заголовкам: PASS-0, PASS-A, PASS-B…

    Ключ — только имя прохода, без описания: у чертежа это «PASS-B Текст
    листа (из DWG)», у PDF — «PASS-B Тайлы / текст», и по полному заголовку
    они бы не сошлись.
    """
    out: dict[str, str] = {}
    marks = list(_SECTION.finditer(body or ""))
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        out.setdefault(mark.group(1), body[mark.end() : end].strip())
    return out


def _tables(body: str) -> list[list[str]]:
    """Таблицы markdown, каждая своим списком строк."""
    found: list[list[str]] = []
    current: list[str] = []
    for line in (body or "").splitlines():
        if line.lstrip().startswith("|"):
            current.append(line.rstrip())
            continue
        if current:
            found.append(current)
            current = []
    if current:
        found.append(current)
    # Две строки — это заголовок и разделитель, данных в такой таблице нет.
    # Пустая сетка «| | |» тоже не таблица: у листа-текста модель иногда
    # рисует её на пустом месте, и в отчёт уходил бы каркас без единой ячейки.
    return [
        table
        for table in found
        if len(table) > 2 and len(tokens("\n".join(table[2:]))) >= 3
    ]


# Маркер списка в начале строки. Именно маркер, а не любая звёздочка: у
# «**Цветовая разметка:**» звёздочки — жирный шрифт, и снимать их нельзя.
_BULLET = re.compile(r"^[-*•]\s+")

# Служебные пометки конвейера в тексте листа: предупреждения сверки с OCR,
# следы противопетлевой чистки. Это наши же отметки о работе, а не содержание
# чертежа, и в сведённом листе они читаются как данные.
_SERVICE = re.compile(
    r"^\[(?:Warning|MOCK|OCR|truncated|inline)|^_?\[mock\]|^\[.{0,40}схлопнут",
    re.IGNORECASE,
)


def _content_lines(body: str) -> list[str]:
    """Строки раздела без разметки: без таблиц, курсивных пояснений и списков.

    Пояснения вроде «_Текст взят из чертежа как данные_» — наши же подписи к
    разделам, и в добор они попадать не должны.
    """
    out: list[str] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("|", ">", "#", "---")):
            continue
        if stripped.startswith("_") and stripped.endswith("_"):
            continue
        if _SERVICE.match(stripped):
            continue
        stripped = _BULLET.sub("", stripped).strip()
        if len(stripped) >= _MIN_LINE:
            out.append(stripped)
    return out


def _fragment(word: str, known: set[str]) -> bool:
    """Слово — обрывок того, что уже есть: «дской» от «складской».

    Лист режется на фрагменты, и модель читает слово, разрезанное границей
    куска, половиной. На титульном листе ПЗ так пришли «дской комплекс»,
    «СА-1/25-ПЗ» и «РЕГИОНПРОЕКТ"» — то есть уже известное, но покалеченное.
    Новой информации в таком нет, а в отчёте оно читается как отдельный факт.
    """
    return any(word in other for other in known if len(other) > len(word))


def _is_piece(word: str, known: set[str]) -> bool:
    """То же для строки текста, где короткое слово само по себе не факт.

    В таблице короткое слово как раз и есть содержание — там считаем иначе
    (`_fragment`): её ценность в числах, а они почти все короткие.
    """
    return len(word) < 3 or _fragment(word, known)


# Насколько строка должна совпасть с одной строкой чертежа, чтобы считать её
# не новой, а перечитанной. Порог взят по титульному листу ПЗ: там перечтения
# совпадают с исходной строкой на 0,6–0,9, а настоящие находки — ниже 0,3.
_REREAD = 0.6


def _reread(found: set[str], vector_lines: list[set[str]]) -> bool:
    """Строка — это уже известная строка, прочитанная с ошибками.

    Самое вредное, что приносит чтение по фрагментам: не новый факт, а
    искажённый старый. На титульном листе ПЗ модель выдала «ООО "ХОЛДИНГ
    СТРОИТЕЛЬНЫЙ КОМПЛЕКС-1"» вместо «АЛЬЯНС-1» и «Российская Федерация,
    Москва» вместо «Московская область». В доборе такое читается как ещё один
    заказчик и ещё один адрес — то есть прямая дезинформация, которую модель
    следующего шага примет за факт.

    Узнаём по перекрытию с ОДНОЙ строкой чертежа: перечтение почти целиком
    состоит из её слов, а настоящая находка — нет.
    """
    if not found:
        return False
    return any(
        len(found & line) / len(found) >= _REREAD for line in vector_lines if line
    )


def _new_lines(
    lines: list[str], known: set[str], vector_lines: list[set[str]]
) -> tuple[list[str], set[str]]:
    """Строки, несущие хоть один незнакомый и не обрывочный токен."""
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = " ".join(line.split()).upper()
        if key in seen:
            continue
        found = tokens(line)
        fresh = found - known
        if not fresh or all(_is_piece(word, known) for word in fresh):
            continue
        if _reread(found, vector_lines):
            continue
        seen.add(key)
        out.append(line)
        known |= found
    return out, known


# ── расхождения в числах ─────────────────────────────────────────────────────
#
# Счётная сверка выше отвечает на вопрос «сколько совпало». Она не видит
# главного для ПТО: когда обе версии листа говорят одно и то же, но **разными
# числами** — «Отметка низа −2.050» в чертеже против «−2.150» в альбоме. Это и
# есть разные редакции, ровно то, что проверяющий обязан заметить.
#
# Ищем так: у каждой строки берём «скелет» — текст, в котором все числа
# заменены на «#». Если один и тот же скелет есть в обоих источниках, а числа
# в нём разные — это расхождение, и его можно показать дословно, обеими
# строками. Отличие «есть только в чертеже» сюда не попадает: это разница
# чтения, а не редакции, и её считает `compare()`.

_NUMBER = re.compile(r"[-−]?\d+(?:[.,]\d+)?")
# Строки конвейера: заголовки проходов, наши же маркеры, разделители таблиц.
_SKIP_LINE = re.compile(
    r"^\s*(?:#{1,6}\s|\|[\s:|-]+\|\s*$|_[^_]*_\s*$|-{3,}\s*$|>\s)"
)
# Скелет короче этого сравнивать нельзя: «# #» совпадёт с чем угодно.
_MIN_SKELETON_CHARS = 12
_MIN_SKELETON_LETTERS = 4


def _norm_number(text: str) -> str:
    """Число к сравнимому виду: запятая → точка, минус один, нули в хвосте."""
    value = text.replace("−", "-").replace(",", ".")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    if value in ("-", ""):
        return "0"
    return value


def _skeleton(line: str) -> tuple[str, tuple[str, ...]]:
    """(текст без чисел, сами числа) — ключ для сравнения строк источников."""
    numbers = tuple(_norm_number(m.group()) for m in _NUMBER.finditer(line))
    bare = _NUMBER.sub("#", line)
    # Разделители таблиц и лишние пробелы к делу не относятся: одна и та же
    # надпись в DXF нарезана ячейками, а в PDF идёт строкой.
    bare = re.sub(r"[|·•]+", " ", bare)
    bare = re.sub(r"\s+", " ", bare).strip().lower().replace("ё", "е")
    return bare, numbers


def _skeletons(body: str) -> dict[str, list[tuple[tuple[str, ...], str]]]:
    out: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or _SKIP_LINE.match(line) or any(m in line for m in MARKERS):
            continue
        bare, numbers = _skeleton(line)
        if not numbers:
            continue
        if len(bare) < _MIN_SKELETON_CHARS:
            continue
        if sum(1 for ch in bare if ch.isalpha()) < _MIN_SKELETON_LETTERS:
            continue
        out.setdefault(bare, []).append((numbers, line))
    return out


def number_conflicts(
    vector_body: str, raster_body: str, *, limit: int = 12
) -> list[tuple[str, str, str]]:
    """Строки, совпавшие словами, но разошедшиеся числами.

    Возвращает `(что за строка, как в чертеже, как во втором источнике)`.

    Сравниваем только те скелеты, которые встречаются в каждом источнике
    **ровно один раз**. Если одна и та же формулировка идёт столбцом таблицы
    («Отметка низа») — какое из значений какому соответствует, мы не знаем, и
    выдавать пару наугад значило бы придумывать расхождение. Пропуск лучше
    выдумки: такие строки просто не показываем.
    """
    left = _skeletons(vector_body)
    right = _skeletons(raster_body)
    found: list[tuple[str, str, str]] = []
    for bare, entries in left.items():
        other = right.get(bare)
        if not other or len(entries) != 1 or len(other) != 1:
            continue
        (numbers, line), (other_numbers, other_line) = entries[0], other[0]
        if numbers == other_numbers:
            continue
        found.append((bare.replace("#", "…"), line, other_line))
        if len(found) >= limit:
            break
    return found


def compare(vector_body: str, raster_body: str) -> dict:
    """Счётная сверка двух чтений одного листа.

    Считаем не строки, а токены: одна и та же надпись в чертеже и в PDF
    нарезана по-разному — в DXF это две ячейки таблицы, в PDF одна строка, —
    и построчное сравнение показывало бы расхождение там, где его нет.
    """
    vector = tokens(vector_body)
    raster = tokens(raster_body)
    return {
        "в чертеже": len(vector),
        "в PDF": len(raster),
        "общих": len(vector & raster),
        "только в чертеже": len(vector - raster),
        "только в PDF": len(raster - vector),
    }


def _describe(counts: dict) -> list[str]:
    """Сверка источников строками отчёта."""
    both = counts["общих"]
    vector, raster = counts["в чертеже"], counts["в PDF"]
    share = f"{both / vector:.0%}" if vector else "—"
    return [
        f"##### {SRC_BOTH} Сверка источников",
        "",
        f"- слов и чисел в чертеже: {vector}, в PDF: {raster}; "
        f"совпало {both} ({share} от чертежа)",
        f"- есть только в чертеже: {counts['только в чертеже']} — в альбом это "
        "не попало или прочиталось иначе",
        f"- есть только в PDF: {counts['только в PDF']} — в чертеже этого нет "
        "(обычно содержимое внешних ссылок, которых в комплекте DWG не бывает)",
        "",
    ]


def fuse(vector_body: str, raster_body: str, *, raster_name: str = "PDF") -> str:
    """Лист из двух источников одним текстом. Основа — чертёж.

    Пустой второй источник возвращает первый нетронутым: сводить не с чем.
    """
    if not (vector_body or "").strip():
        return raster_body or ""
    if not (raster_body or "").strip():
        return vector_body

    known = tokens(vector_body)
    raster_sections = sections(raster_body)
    # В добор идёт только то, что модель ПРОЧИТАЛА с листа, а не то, что она
    # о листе рассказала. Паспорт (PASS-0) — реквизиты, они сведены отдельно
    # по основной надписи. Описание (PASS-A) — пересказ, ему своё место в
    # конце; попав в добор, оно вдобавок задваивается. Проверено на титульном
    # листе ПЗ: без этого отсева в добор уезжали «Нет: систем, труб, арматуры…»
    # и придуманные моделью ИНН с ОГРН — то есть выдумка в виде фактов.
    raster_text = "\n\n".join(
        text
        for name, text in raster_sections.items()
        if name not in ("PASS-0", "PASS-A")
    ) or (raster_body if not raster_sections else "")

    parts = [vector_body.rstrip(), ""]
    parts += _describe(compare(vector_body, raster_body))

    vector_lines = [tokens(line) for line in _content_lines(vector_body)]
    extra, known = _new_lines(_content_lines(raster_text), known, vector_lines)
    if extra:
        shown = extra[:_MAX_EXTRA]
        parts += [
            f"##### {SRC_PDF} Добор из {raster_name}",
            "",
            "_Прочитано с картинки и чертежом не подтверждено._ Там, где "
            "чертёж пришёл без внешних ссылок, здесь лежит всё содержимое "
            "листа. Там, где чертёж полон, здесь остаются ошибки узнавания: "
            "модель путает буквы и дочитывает закрытое печатью. Проверять по "
            "исходнику.",
            "",
        ]
        parts += [f"- {line}" for line in shown]
        if len(extra) > len(shown):
            # Молча обрезать нельзя: отчёт выглядел бы полным.
            parts.append(
                f"- _…и ещё {len(extra) - len(shown)} строк добора: "
                f"смотрите лист в исходном {raster_name}._"
            )
        parts.append("")

    # Таблица переносится, только если несёт своё содержание. У листа-текста
    # модель иногда пересобирает в сетку то, что и так есть в чертеже: на
    # титульном листе ПЗ так вышла «таблица» из строк объекта и раздела.
    fresh = []
    for table in _tables(raster_text):
        own = {
            word
            for word in tokens("\n".join(table)) - known
            if not _fragment(word, known)
        }
        if len(own) >= _TABLE_OWN:
            fresh.append(table)
    if fresh:
        parts += [
            f"##### {SRC_PDF} Таблицы, найденные только в {raster_name}",
            "",
            "_В чертеже такой сетки нет: либо таблица лежит во внешней ссылке, "
            "либо её линии не пережили конвертацию._",
            "",
        ]
        for table in fresh:
            parts += table + [""]

    described = raster_sections.get("PASS-A", "").strip()
    if described:
        parts += [
            f"##### {SRC_PDF} Описание листа ({raster_name}, читала модель)",
            "",
            "_Единственное, чего из чертежа не взять: что именно изображено. "
            "Это пересказ модели по картинке, а не данные файла._",
            "",
            described,
            "",
        ]
    return "\n".join(parts).rstrip() + "\n"


# ── разметка сведённого листа для интерфейса ────────────────────────────────
#
# Интерфейсу нужно нарисовать у каждого блока значок: «из чертежа», «только
# PDF», «расхождение». Разбирать для этого русские заголовки нельзя — они
# меняются от правки к правке. И ставить метку на каждую строку тоже нельзя:
# сведённый лист читает модель, и метка у каждой строки сбивает ей чтение.
#
# Поэтому блок — это раздел листа, а его описание уходит отдельным полем.

_HEADING = re.compile(r"(?m)^(#{3,5})\s+(\S+)\s*(.*)$")


def blocks(merged: str, *, base: str = "DWG") -> list[dict]:
    """Разделы сведённого листа: чем помечен и какие строки занимает.

    `base` — источник основы листа: у листа без чертежа помечать блоки как
    данные чертежа нельзя, там всё прочитано с картинки.

    Границы даём номерами строк, а не заголовками: заголовок повторяется (у
    двух PDF-источников два блока «Добор из…»), а номера строк однозначны и
    режут markdown без разбора.
    """
    lines = (merged or "").splitlines()
    found: list[dict] = []
    for number, line in enumerate(lines):
        head = _HEADING.match(line)
        if not head:
            continue
        _, first, rest = head.groups()
        marked = first in MARKERS
        # Границей блока считаем только наши собственные заголовки: разделы
        # листа «### PASS-*» и помеченные блоки сведения. Внутри пересказа
        # модели попадаются её же заголовки («### Описание: титульный лист»),
        # и по ним блок делить нельзя — это содержимое, а не структура.
        if not marked and not first.startswith("PASS-"):
            continue
        found.append(
            {
                "id": f"b{len(found) + 1}",
                "marker": first if marked else (SRC_DWG if base == "DWG" else SRC_PDF),
                "title": (rest if marked else f"{first} {rest}").strip(),
                "startLine": number,
                "endLine": len(lines),
            }
        )
    for index, block in enumerate(found[:-1]):
        block["endLine"] = found[index + 1]["startLine"]
    # Строки до первого заголовка — тоже содержимое листа: у страницы PDF это
    # весь текст, если конвейер не проставил разделы.
    if not found and lines:
        found.append(
            {
                "id": "b1",
                "marker": SRC_DWG if base == "DWG" else SRC_PDF,
                "title": "Лист",
                "startLine": 0,
                "endLine": len(lines),
            }
        )
    return found
