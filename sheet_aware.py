#!/usr/bin/env python3
"""Sheet-aware понимание строительных листов (документ-агностично).

Алгоритм:
  1) passport страницы из PDF+геометрии → sheet_kind + context_pack
  2) Pass-A / Pass-B промпты и dpi зависят от kind
  3) quality gate на Pass-A; при fail — zone-describe retry
  4) фильтр мусорных тайлов (голые 1..N таблицы)
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

import fitz
from PIL import Image

SheetKind = Literal["text", "table", "plan", "scheme", "mixed"]

_LABEL_RE = re.compile(
    r"^(?:[A-Za-zА-Яа-яЁё]\d{0,3}(?:\.\d+)?|"
    r"ДРП|ПГ\d*|УП\d+(?:\.\d+)?|Ввод\s+\S+|"
    r"∅\d+[xх×]\d+(?:\.\d+)?)$",
    re.I,
)
_TITLE_HINT = re.compile(
    r"(?im)^(?:План|Схема|Разрез|Чертёж|Чертеж|Ведомость|Таблица|Содержание|"
    r"Титульный|Общие\s+данные|Экспликация)\b.{0,120}$"
)
_DIAM_RE = re.compile(r"[∅φФ]\s*\d+|DN\s*\d+", re.I)
_AXIS_RE = re.compile(r"(?i)\bось\b|\bоси\b")


@dataclass
class SheetPassport:
    page_num: int
    kind: SheetKind
    width_pt: float
    height_pt: float
    text_len: int
    unique_labels: list[str] = field(default_factory=list)
    title_hints: list[str] = field(default_factory=list)
    diam_samples: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def context_pack(self, *, max_labels: int = 40) -> str:
        parts: list[str] = [
            f"Тип листа (эвристика): {self.kind}",
            f"Размер: {self.width_pt:.0f}×{self.height_pt:.0f} pt",
        ]
        if self.title_hints:
            parts.append("Возможные заголовки с текст-слоя PDF:")
            parts.extend(f"  - {t}" for t in self.title_hints[:6])
        if self.unique_labels:
            labels = ", ".join(self.unique_labels[:max_labels])
            parts.append(f"Уникальные короткие метки на листе: {labels}")
        if self.diam_samples:
            parts.append(
                "Примеры диаметров/DN с листа: " + ", ".join(self.diam_samples[:20])
            )
        parts.append(
            "Используй эти якоря: опиши их расположение и связи. "
            "Не выдумывай объекты/шифры, которых нет на изображении."
        )
        return "\n".join(parts)


def _cyr_ratio(s: str) -> float:
    cyr = len(re.findall(r"[А-Яа-яЁё]", s))
    letters = len(re.findall(r"[A-Za-zА-Яа-яЁё]", s))
    return cyr / letters if letters else 0.0


def build_passport(page: fitz.Page, page_num: int) -> SheetPassport:
    w, h = page.rect.width, page.rect.height
    # Паспорт уходит в промпт как якоря «не выдумывай сверх этого». Со
    # сломанным ToUnicode якорями становились кракозябры («сǭедеǸиȊ о ǸаǺоре»),
    # то есть модель получала мусор вместо опоры. Слой чиним заранее; если
    # починить нечем, приходит исходный текст и всё работает как раньше.
    try:
        from deglyph import page_text_fixed

        raw = page_text_fixed(page, quiet=True)
    except Exception:
        raw = page.get_text("text") or ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    title_hints = [m.group(0).strip() for m in _TITLE_HINT.finditer(raw)]
    for ln in lines[:40]:
        if len(ln) >= 12 and _cyr_ratio(ln) > 0.3 and ln not in title_hints:
            low = ln.lower()
            if any(
                k in low
                for k in (
                    "план",
                    "схема",
                    "разрез",
                    "ведомость",
                    "сети",
                    "водоснаб",
                    "канализ",
                    "отоплен",
                )
            ):
                title_hints.append(ln)
    title_hints = list(dict.fromkeys(title_hints))[:8]

    label_counts = Counter(
        ln for ln in lines if _LABEL_RE.match(ln) and len(ln) <= 24
    )
    unique_labels = [k for k, _ in label_counts.most_common(60)]
    diams = list(dict.fromkeys(_DIAM_RE.findall(raw)))[:30]

    longest = max(w, h)
    shortest = min(w, h)
    aspect = longest / max(shortest, 1.0)
    n_labels = sum(label_counts.values())
    n_unique = len(unique_labels)
    axis_hits = len(_AXIS_RE.findall(raw))
    pipe_ish = len(diams) + sum(
        1 for k in unique_labels if re.match(r"^[ВWKТКвwkтк]\d", k)
    )

    reasons: list[str] = []
    kind: SheetKind

    is_large = longest >= 1100 or (longest >= 900 and shortest >= 700)
    is_wide = aspect >= 1.6 and longest >= 1000

    if is_large and (n_labels >= 40 or n_unique >= 15 or pipe_ish >= 8 or is_wide):
        kind = "plan"
        reasons.append(
            f"large+labels(unique={n_unique},total={n_labels},pipeish={pipe_ish})"
        )
    elif is_large and (axis_hits >= 5 or "разрез" in raw.lower()[:800]):
        kind = "scheme"
        reasons.append(f"large+axes/разрез(axis={axis_hits})")
    elif is_large:
        kind = "scheme"
        reasons.append("large drawing default→scheme")
    else:
        table_cues = raw.lower().count("таблица") + raw.count("|")
        short_num_lines = sum(
            1
            for ln in lines
            if re.match(r"^\d{1,3}$", ln) or re.match(r"^\d+\s+\S+", ln)
        )
        if table_cues >= 2 or short_num_lines >= 40:
            kind = "table"
            reasons.append(f"a4 table cues={table_cues} short_num={short_num_lines}")
        elif len(raw) > 800 and n_unique < 8:
            kind = "text"
            reasons.append("a4 long text, few CAD labels")
        elif n_unique >= 10 and pipe_ish >= 5:
            kind = "mixed"
            reasons.append("a4 with network labels")
        else:
            kind = "text"
            reasons.append("a4 default text")

    return SheetPassport(
        page_num=page_num,
        kind=kind,
        width_pt=w,
        height_pt=h,
        text_len=len(raw),
        unique_labels=unique_labels,
        title_hints=title_hints,
        diam_samples=diams,
        reasons=reasons,
    )


SYSTEM_DESC_PLAN = (
    "Ты пишешь ЭТАЛОННЫЙ подробный Markdown по русскому строительному ПЛАНУ "
    "для последующего Q&A другой моделью. "
    "Нужна МАКСИМАЛЬНАЯ полнота: геометрия, ВСЕ читаемые таблицы целиком, "
    "ВСЯ легенда, ВЕСЬ читаемый текст штампа/подписей. "
    "Факты только с изображения. Кириллицу не латинизировать. "
    "ЗАПРЕЩЕНО: краткие пересказы; петли одних слов; голые номера 1..N без смысла; "
    "выдуманные объекты; эмодзи; копипаст одной метки десятки раз "
    "(повторы — сводкой). Графику — словами."
)

SYSTEM_DESC_SCHEME = (
    "Ты пишешь ЭТАЛОННЫЙ подробный Markdown по схеме/разрезу/П&ID для Q&A. "
    "Максимальная полнота: потоки, оборудование, связи, отметки, "
    "полные таблицы и легенда, весь читаемый текст. "
    "Без краткости, без петель, без голых 1..N, без эмодзи."
)

SYSTEM_DESC_TABLE = (
    "Ты пишешь ЭТАЛОННЫЙ Markdown по листу с таблицами/ведомостями для Q&A. "
    "Главное: ПОЛНЫЕ таблицы со ВСЕМИ читаемыми строками и числами в markdown. "
    "Плюс описание структуры и штамп. Не выдумывай числа. Не сокращай «…»."
)

SYSTEM_DESC_TEXT = (
    "Ты пишешь ЭТАЛОННЫЙ подробный Markdown по текстовому строительному листу для Q&A. "
    "Перенеси ВЕСЬ читаемый текст (разделы, списки, нормы, формулы, примечания) "
    "и любые таблицы целиком. Не выдумывай. Не делай краткое резюме вместо текста."
)

_ETALON_RULES = (
    "Требования полноты (как в эталонном .ref.md):\n"
    "- Объём: лучше избыточно подробно, чем коротко.\n"
    "- Таблицы: markdown (| col |) со ШАПкой и ВСЕМИ видимыми строками; "
    "не пиши «и т.д.» / «…» вместо строк.\n"
    "- Легенда/условные обозначения: КАЖДЫЙ знак — словами + расшифровка.\n"
    "- Экспликации зданий/сетей: все читаемые позиции.\n"
    "- Штамп: шифр, организация, объект, стадия, лист, масштаб, формат.\n"
    "- Повторяющиеся метки сетей (В1, W1…) — уникальным списком + «многократно на плане».\n"
)

PROMPT_DESC_PLAN = (
    "Опиши ВЕСЬ лист МАКСИМАЛЬНО ПОДРОБНО (эталон для Q&A по чертежу).\n"
    + _ETALON_RULES
    + "\n## Описание изображения\n\n"
    "### Описание: <точное название с штампа/заголовка>\n\n"
    "**ЧТО ИЗОБРАЖЕНО:** 4–8 предложений: тип листа, системы (В0/В1/В3/…), "
    "объект, масштаб, характер графики.\n\n"
    "**ИЗ ЧЕГО СОСТОИТ:** развёрнутые списки:\n"
    "- здания/блоки с номерами и названиями (все читаемые);\n"
    "- сети, диаметры ∅/DN, материалы;\n"
    "- арматура, ПГ, камеры, колодцы, футляры, УП, вводы с номерами;\n"
    "- экспликация — ПОЛНАЯ markdown-таблица, если видна;\n"
    "- легенда — полный список;\n"
    "- штамп — все поля.\n\n"
    "**ГДЕ РАСПОЛОЖЕНЫ ЭЛЕМЕНТЫ:** подробно по зонам (СЗ/СВ/ЮЗ/ЮВ, центр) "
    "и относительно зданий — куда идут магистрали.\n\n"
    "**КАК СВЯЗАНЫ:** поток от точки подключения → узлы → вводы/потребители.\n\n"
    "**ПОЛНЫЙ ЧИТАЕМЫЙ ТЕКСТ (дополнительно):** выпиши отдельным блоком "
    "весь остальной читаемый текст с листа (подписи, примечания, сноски), "
    "который ещё не попал в таблицы выше.\n"
)

PROMPT_DESC_SCHEME = (
    "Опиши схему/разрез МАКСИМАЛЬНО ПОДРОБНО (эталон для Q&A).\n"
    + _ETALON_RULES
    + "\n## Описание изображения\n\n"
    "### Описание: <название>\n\n"
    "**ЧТО ИЗОБРАЖЕНО:**\n\n"
    "**ИЗ ЧЕГО СОСТОИТ:** оборудование, арматура, трубы, позиции, "
    "полные таблицы сигналов/спецификаций если есть, легенда, штамп.\n\n"
    "**ГДЕ РАСПОЛОЖЕНЫ ЭЛЕМЕНТЫ:**\n\n"
    "**КАК СВЯЗАНЫ:**\n\n"
    "**ПОЛНЫЙ ЧИТАЕМЫЙ ТЕКСТ (дополнительно):** весь оставшийся текст.\n"
)

PROMPT_DESC_TABLE = (
    "Разбери лист с таблицами МАКСИМАЛЬНО ПОДРОБНО (эталон).\n"
    + _ETALON_RULES
    + "\n## Описание изображения\n\n"
    "### Описание: <название>\n\n"
    "**ЧТО ИЗОБРАЖЕНО:** какие таблицы/ведомости, объект, программа/автор если видно.\n\n"
    "**ИЗ ЧЕГО СОСТОИТ:** для КАЖДОЙ таблицы — заголовок + ПОЛНЫЙ markdown "
    "со всеми строками и числами (не структура «о чём таблица», а сами данные).\n\n"
    "**ГДЕ РАСПОЛОЖЕНЫ ЭЛЕМЕНТЫ:**\n\n"
    "**КАК СВЯЗАНЫ:**\n\n"
    "**ПОЛНЫЙ ЧИТАЕМЫЙ ТЕКСТ (дополнительно):** примечания, формулы, штамп.\n"
)

PROMPT_DESC_TEXT = (
    "Перенеси содержимое текстового листа МАКСИМАЛЬНО ПОЛНО (эталон).\n"
    + _ETALON_RULES
    + "\n## Описание изображения\n\n"
    "### Описание: <заголовок>\n\n"
    "**ЧТО ИЗОБРАЖЕНО:**\n\n"
    "**ИЗ ЧЕГО СОСТОИТ:** структура + ПОЛНЫЙ текст разделов (не саммари). "
    "Списки и таблицы — целиком.\n\n"
    "**ГДЕ РАСПОЛОЖЕНЫ ЭЛЕМЕНТЫ:**\n\n"
    "**КАК СВЯЗАНЫ:**\n\n"
    "**ПОЛНЫЙ ЧИТАЕМЫЙ ТЕКСТ:** если что-то не вошло выше — допиши здесь.\n"
)

PROMPT_TILE_SPATIAL = (
    "Это ФРАГМЕНТ строительного плана/схемы. Выдай МАКСИМАЛЬНО ПОДРОБНЫЙ ответ:\n"
    "1) ПРОСТРАНСТВО: что нарисовано; куда идут линии; здания/узлы.\n"
    "2) ТЕКСТ: ВСЕ читаемые подписи, диаметры, УП, вводы, отметки "
    "(уникальные; повторы — сводкой).\n"
    "3) ТАБЛИЦЫ на фрагменте: ПОЛНЫЙ markdown со всеми строками.\n"
    "4) ЛЕГЕНДА на фрагменте: каждый знак словами.\n"
    "ЗАПРЕЩЕНО: таблица из голых номеров 1,2,3…; оси 1..N без смысла; эмодзи; "
    "фраза «и т.д.» вместо данных.\n"
)

PROMPT_TILE_TABLE = (
    "Извлеки с фрагмента ВСЕ таблицы/ведомости в markdown (| col |).\n"
    "Каждая видимая строка обязательна. Числа точно. Не обрезай середину. "
    "Не пиши «…». Плюс весь остальной читаемый текст фрагмента.\n"
)

PROMPT_TILE_TEXT = (
    "Извлеки ВЕСЬ читаемый текст с фрагмента (абзацы, списки, примечания). "
    "Таблицы — полным markdown. Ничего не сокращай до резюме.\n"
)

PROMPT_ZONE_SPATIAL = (
    "Опиши ЭТУ зону МАКСИМАЛЬНО ПОДРОБНО: объекты, сети, направления, "
    "весь читаемый текст, полные таблицы markdown. Без петель и голых 1..N.\n"
)

PROMPT_ZONE_EXTRACT = (
    "Зона с таблицами/экспликацией/легендой/штампом. "
    "Извлеки ВСЁ: полные markdown-таблицы (все строки), полный список легенды, "
    "весь текст штампа. Эталонная полнота, без «…».\n"
)

# ── LEAN: разделение ролей проходов ─────────────────────────────────────────
#
# Промпты выше требуют от КАЖДОГО прохода описать лист целиком: PASS-A просит
# «ЧТО ИЗОБРАЖЕНО / ИЗ ЧЕГО СОСТОИТ / ГДЕ РАСПОЛОЖЕНЫ / КАК СВЯЗАНЫ» и сверх
# того «ПОЛНЫЙ ЧИТАЕМЫЙ ТЕКСТ», а каждый тайл PASS-B — «ПРОСТРАНСТВО: что
# нарисовано» плюс снова весь текст. Один факт приходит по четыре раза, разными
# словами и с разными ошибками: замер на 6 листах дал 495 тыс. символов на
# 421 уникальный факт (1176 символов на факт) и 352 оборота «вероятно/возможно».
#
# Здесь роли разведены:
#   PASS-A  — смысл листа: что это, из чего состоит и где, как связано. Один раз.
#   PASS-B  — только вычитка текста с фрагмента. Никаких описаний картинки.
# Пересечения между проходами нет по построению, поэтому и склеивать их можно
# без семантической дедупликации, которой у нас нет.

_LEAN_RULES = (
    "Правила вывода:\n"
    "- Каждый факт называй ОДИН раз. Не пересказывай в следующем разделе то, "
    "что уже написал в предыдущем.\n"
    "- Не выписывай текст листа отдельным блоком «весь текст»: он уже разнесён "
    "по разделам.\n"
    "- Повторяющиеся метки сетей (В1, W1, К1…) — один раз списком с пометкой "
    "«многократно на плане».\n"
    "- Пиши только то, что НАПЕЧАТАНО на листе. Нечитаемое → «неразборчиво».\n"
    "- ЗАПРЕЩЕНЫ догадки и слова «вероятно», «возможно», «по-видимому», "
    "«скорее всего»: либо факт с листа, либо «неразборчиво».\n"
    "- ЗАПРЕЩЕНО пояснять лист своими знаниями: ГОСТ/СП/СНиП, номера пунктов, "
    "типовые значения. Любое число должно быть напечатано на листе.\n"
    "- Кириллицу не латинизировать (В1≠B1, ИГЭ≠IGE).\n"
    "- Не повторяй формулировки этой инструкции в ответе.\n"
)

SYSTEM_DESC_LEAN = (
    "Ты извлекаешь содержание русского строительного листа для инженера ПТО, "
    "который сверяет проект с техническим заданием. "
    "Ему нужен ОДИН точный разбор листа, а не несколько пересказов одного и того же. "
    "Полнота считается по фактам (марки, диаметры, позиции, отметки, шифры), "
    "а не по объёму текста: каждый факт ровно один раз. "
    "Выдуманный факт вреднее пропущенного — его не видно глазами. "
    "Если не знаешь, где объект расположен, место не указывай: приписать "
    "двадцати объектам одно и то же место хуже, чем не указать его совсем. "
    "Кириллицу не латинизировать. Графические значки — словами. Без эмодзи."
)

PROMPT_DESC_LEAN_PLAN = (
    "Разбери лист ОДИН раз, по разделам ниже.\n"
    + _LEAN_RULES
    + "\n### Описание: <точное название с штампа или заголовка>\n\n"
    "**ЧТО ЭТО:** 2–4 предложения: тип листа (план/схема/разрез), системы "
    "(В0/В1/В3/Т3/К1…), объект, масштаб — если напечатан.\n\n"
    "**СОСТАВ И РАЗМЕЩЕНИЕ:** маркированный список. В КАЖДОМ пункте объект и "
    "сразу его место на листе, одной строкой: "
    "«- ВК-3, колодец водомерный — юго-запад, у здания 2». "
    "Сюда входят здания и блоки с номерами, сети с диаметрами ∅/DN, арматура, "
    "колодцы и камеры, вводы, футляры, углы поворота, отметки. "
    "Отдельного раздела «где расположено» не делай — место указывается здесь.\n\n"
    "**СВЯЗИ:** откуда приходит среда → через какие узлы → куда уходит; "
    "границы очередей; ссылки «см. лист…».\n\n"
    "**ЛЕГЕНДА:** знак → расшифровка, как напечатано на листе.\n\n"
    "**ШТАМП:** шифр, организация, объект, стадия, лист, масштаб, формат.\n\n"
    "**ЦВЕТОВАЯ РАЗМЕТКА:** что на листе выделено цветом — заливка столбцов "
    "и строк, цветные линии и зоны, штриховки — и что означает выделение. "
    "Цветных выделений нет — раздел пропусти, выдумывать нельзя.\n\n"
    "**ТАБЛИЦЫ:** каждую видимую таблицу — одной GFM-таблицей со всеми строками "
    "(«…» и «и т.д.» запрещены). Таблиц нет — раздел пропусти.\n"
)

PROMPT_DESC_LEAN_SCHEME = (
    "Разбери схему/разрез ОДИН раз, по разделам ниже.\n"
    + _LEAN_RULES
    + "\n### Описание: <точное название>\n\n"
    "**ЧТО ЭТО:** 2–4 предложения: что за схема, система, объект.\n\n"
    "**СОСТАВ И РАЗМЕЩЕНИЕ:** оборудование, арматура, трубопроводы с "
    "диаметрами, позиции и марки — каждый пункт с местом на схеме "
    "(слева/справа/сверху/снизу, между какими узлами).\n\n"
    "**СВЯЗИ:** поток по схеме от входа к выходу через узлы.\n\n"
    "**ЛЕГЕНДА:** знак → расшифровка.\n\n"
    "**ШТАМП:** все поля.\n\n"
    "**ЦВЕТОВАЯ РАЗМЕТКА:** что на листе выделено цветом — заливка столбцов "
    "и строк, цветные линии и зоны, штриховки — и что означает выделение. "
    "Цветных выделений нет — раздел пропусти, выдумывать нельзя.\n\n"
    "**ТАБЛИЦЫ:** полные GFM-таблицы (спецификации, ведомости сигналов).\n"
)

PROMPT_DESC_LEAN_TABLE = (
    "На листе таблицы — перенеси их, а не пересказывай.\n"
    + _LEAN_RULES
    + "\n### Описание: <название листа>\n\n"
    "**ЧТО ЭТО:** 1–3 предложения: какие таблицы и о чём.\n\n"
    "**ТАБЛИЦЫ:** для КАЖДОЙ — подзаголовок и полная GFM-таблица: шапка "
    "(многоуровневую сплющи в «Группа: подколонка»), ВСЕ строки, ВСЕ ячейки, "
    "пустая ячейка → «-», нечитаемая → «?». Числа переписывай посимвольно.\n\n"
    "**ЦВЕТОВАЯ РАЗМЕТКА:** что на листе выделено цветом — заливка столбцов "
    "и строк, цветные линии и зоны, штриховки — и что означает выделение. "
    "Цветных выделений нет — раздел пропусти, выдумывать нельзя.\n\n"
    "**ТЕКСТ ВНЕ ТАБЛИЦ:** заголовки, примечания, сноски — один раз, без "
    "повторения ячеек.\n\n"
    "**ШТАМП:** все поля.\n"
)

PROMPT_DESC_LEAN_TEXT = (
    "Это текстовый лист. Перенеси его содержание один раз, без пересказа.\n"
    + _LEAN_RULES
    + "\n### Описание: <заголовок листа>\n\n"
    "**ЧТО ЭТО:** 1–2 предложения.\n\n"
    "**ТЕКСТ:** полный текст листа по разделам, в исходном порядке — абзацы, "
    "списки, примечания, формулы. Не резюмируй и не сокращай. Таблицы — GFM.\n\n"
    "**ШТАМП:** все поля.\n"
)

PROMPT_DESC_OVER_LAYER = (
    "Текст этого листа уже извлечён из PDF и приведён ниже — инженер его видит. "
    "Переписывать его НЕ НУЖНО. Твоя работа — объяснить графику, которой в "
    "тексте не видно: как идут системы, что где расположено, что означают "
    "условные знаки.\n"
    + _LEAN_RULES
    + "\n### Описание: <название листа со штампа>\n\n"
    "**ЧТО ЭТО:** 2–4 предложения: тип листа, какие системы, объект, масштаб.\n\n"
    "**ХОД СИСТЕМ:** для каждой системы (В0, В1, К1, Т3…) отдельным пунктом: "
    "откуда начинается, куда идёт, через какие узлы и колодцы проходит, где "
    "заканчивается. На плане сетей это главное.\n\n"
    "**РАЗМЕЩЕНИЕ ПО ЛИСТУ:** 5–8 строк крупными блоками: что в какой части "
    "листа (север/юг/запад/восток/центр, правая полоса, низ). Пиши про группы "
    "объектов и зоны, а НЕ про каждый объект по отдельности.\n\n"
    "**ЛЕГЕНДА:** знак → расшифровка, как напечатано на листе.\n\n"
    "**ЦВЕТОВАЯ РАЗМЕТКА:** что на листе выделено цветом — заливка столбцов "
    "и строк, цветные линии и зоны, штриховки — и что означает выделение. "
    "Цветных выделений нет — раздел пропусти, выдумывать нельзя.\n\n"
    "**ЧЕГО НЕТ В СПИСКЕ НИЖЕ:** подписи, пометки и значения, которые видно на "
    "чертеже, но которых нет в приведённом тексте. Если всё есть — так и напиши.\n\n"
    "ЗАПРЕЩЕНО: переписывать подряд метки, диаметры и номера из списка ниже — "
    "они уже извлечены; приписывать одно и то же место многим объектам; "
    "выдавать длинные однотипные перечни.\n"
)

PROMPT_TILE_LEAN = (
    "Это ФРАГМЕНТ листа. Твоя единственная задача — ВЫЧИТАТЬ ТЕКСТ. "
    "Картинку описывать не нужно: этим занят другой проход.\n"
    "1) ПОДПИСИ: все читаемые надписи фрагмента, по одной в строке, "
    "уникальные — коды, марки, диаметры ∅/DN, отметки, номера, названия. "
    "Порядок как на листе.\n"
    "2) ТАБЛИЦЫ: полным markdown со всеми строками.\n"
    "ЗАПРЕЩЕНО: объяснять, что нарисовано и зачем; догадки и слова «вероятно», "
    "«возможно»; голые номера 1..N; повтор одной метки десятки раз; эмодзи.\n"
    "Текста на фрагменте нет → напиши «(нет текста)».\n"
)

PROMPT_ZONE_LEAN = (
    "Зона листа. Вычитай ВЕСЬ текст зоны: полные markdown-таблицы (все строки), "
    "список легенды «знак → расшифровка», текст штампа, подписи и номера. "
    "Не описывай графику и не строй догадок — только напечатанное.\n"
)


def system_for(kind: SheetKind, lean: bool = False) -> str:
    if lean:
        return SYSTEM_DESC_LEAN
    return {
        "plan": SYSTEM_DESC_PLAN,
        "scheme": SYSTEM_DESC_SCHEME,
        "table": SYSTEM_DESC_TABLE,
        "text": SYSTEM_DESC_TEXT,
        "mixed": SYSTEM_DESC_PLAN,
    }[kind]


def prompt_desc_for(
    kind: SheetKind,
    passport: SheetPassport,
    lean: bool = False,
    over_layer: bool = False,
) -> str:
    # Лист, чей текст уже взят из PDF: модель не перечисляет содержимое заново,
    # а объясняет графику поверх готового списка. Иначе она тратит ответ на
    # переписывание меток, которые и так извлечены точнее, чем читаются с
    # картинки, и на плотном плане срывается в однотипный перечень.
    if lean and over_layer and kind in ("plan", "scheme", "mixed", "table"):
        return (
            PROMPT_DESC_OVER_LAYER
            + "\n\n### Якоря с текстового слоя PDF (могут быть неполными)\n"
            + passport.context_pack()
        )
    table = (
        {
            "plan": PROMPT_DESC_LEAN_PLAN,
            "scheme": PROMPT_DESC_LEAN_SCHEME,
            "table": PROMPT_DESC_LEAN_TABLE,
            "text": PROMPT_DESC_LEAN_TEXT,
            "mixed": PROMPT_DESC_LEAN_PLAN,
        }
        if lean
        else {
            "plan": PROMPT_DESC_PLAN,
            "scheme": PROMPT_DESC_SCHEME,
            "table": PROMPT_DESC_TABLE,
            "text": PROMPT_DESC_TEXT,
            "mixed": PROMPT_DESC_PLAN,
        }
    )
    return (
        table[kind]
        + "\n\n### Якоря с текстового слоя PDF (могут быть неполными)\n"
        + passport.context_pack()
    )


def prompt_tile_for(
    kind: SheetKind, base_extract: str, lean: bool = False
) -> str:
    # В lean-режиме тип листа не важен: тайл всегда только вычитывает текст.
    if lean:
        return PROMPT_TILE_LEAN
    if kind in ("plan", "scheme", "mixed"):
        return PROMPT_TILE_SPATIAL
    if kind == "table":
        return PROMPT_TILE_TABLE
    if kind == "text":
        return PROMPT_TILE_TEXT
    return base_extract


def render_budget(kind: SheetKind, page_max: int) -> dict[str, int]:
    if kind == "plan":
        return {
            "page_max": max(page_max, 3600),
            "desc_max": 3200,
            "tile_max": 900,
            "max_tiles": 12,
            "tile_send_px": 1800,
            "pass_a_tokens": 8192,
            "tile_tokens": 4096,
        }
    if kind == "scheme":
        return {
            "page_max": max(page_max, 3000),
            "desc_max": 2800,
            "tile_max": 850,
            "max_tiles": 10,
            "tile_send_px": 1600,
            "pass_a_tokens": 8192,
            "tile_tokens": 4096,
        }
    if kind == "table":
        return {
            "page_max": max(page_max, 2400),
            "desc_max": 2200,
            "tile_max": 800,
            "max_tiles": 8,
            "tile_send_px": 1800,
            "pass_a_tokens": 8192,
            "tile_tokens": 6144,
        }
    return {
        "page_max": max(page_max, 2000),
        "desc_max": 2000,
        "tile_max": 750,
        "max_tiles": 8,
        "tile_send_px": 1600,
        "pass_a_tokens": 6144,
        "tile_tokens": 4096,
    }


def _stamped_location(desc: str) -> bool:
    """Один и тот же «хвост» приписан многим объектам подряд.

    Признак вырожденного ответа: модель обязана указать место, не знает его и
    штампует последнее известное — «Футляр ∅325x4.0 — юго-запад, у здания 10»
    двадцать раз подряд. Такой ответ выглядит содержательным и проходит любую
    проверку длины, но данных в нём нет, поэтому ловим его отдельно.
    """
    tails: Counter = Counter()
    for line in desc.splitlines():
        item = line.strip().lstrip("-*• ").strip()
        if " — " not in item:
            continue
        tail = item.rsplit(" — ", 1)[1].strip().lower()
        if len(tail) >= 6:
            tails[tail] += 1
    return bool(tails) and tails.most_common(1)[0][1] >= 6


def pass_a_quality_ok(
    desc: str,
    passport: SheetPassport,
    lean: bool = False,
    over_layer: bool = False,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if lean and _stamped_location(desc):
        reasons.append("stamped_location")
    # В lean-режиме описание короче по построению — оно больше не обязано
    # пересказывать лист четырьмя способами. Прежние пороги завалили бы
    # каждый лист и запустили дорогой зональный retry на ровном месте.
    min_len = (
        {
            "plan": 2500,
            "scheme": 2000,
            "table": 2000,
            "text": 1500,
            "mixed": 2200,
        }
        if lean
        else {
            "plan": 5500,
            "scheme": 4500,
            "table": 4500,
            "text": 3000,
            "mixed": 5000,
        }
    ).get(passport.kind, 3000 if not lean else 1500)
    if lean and over_layer:
        # Здесь описание короткое по замыслу: содержимое листа уже извлечено из
        # PDF, модель добавляет только графику, ход систем и связи.
        min_len = 1200
    if not desc or len(desc.strip()) < min_len:
        reasons.append(f"too_short<{min_len}")
    if "[truncated-repeat]" in desc or desc.count("подземные") >= 8:
        reasons.append("repetition_loop")
    # мало markdown-таблиц на table/plan — признак недобора
    table_rows = len(re.findall(r"(?m)^\|.+\|$", desc))
    if passport.kind == "table" and table_rows < 8:
        reasons.append("tables_too_thin")
    if passport.kind in ("plan", "scheme", "mixed"):
        low = desc.lower()
        if lean and over_layer:
            needed = ("что это", "ход систем", "размещение по листу")
        elif lean:
            needed = ("что это", "состав и размещение", "связи")
        else:
            needed = (
                "что изображено",
                "из чего состоит",
                "где расположен",
                "как связан",
            )
        missing = [n for n in needed if n not in low]
        if len(missing) >= (2 if lean else 3):
            reasons.append("missing_sections:" + ",".join(missing))
        if re.search(r"(?i)yekaterinburg|жилы(е|х)\s+дом", desc):
            reasons.append("likely_hallucinated_object")
        # Вне lean недобором считается отсутствие блока «полный читаемый текст».
        # В lean этот блок запрещён намеренно — он и был четвёртым пересказом.
        if not lean and "полный читаемый текст" not in low and table_rows < 5:
            reasons.append("missing_full_text_or_tables")
        if passport.title_hints:
            keys: list[str] = []
            for th in passport.title_hints[:3]:
                keys.extend(re.findall(r"[А-Яа-яЁё]{5,}", th))
            skip = {
                "здания",
                "сооружения",
                "наименование",
                "проектируемый",
                "проектируемое",
            }
            keys = [k.lower() for k in keys if k.lower() not in skip]
            if keys and not any(k in low for k in keys[:8]):
                reasons.append("title_mismatch_soft")
    hard = [r for r in reasons if r != "title_mismatch_soft"]
    ok = not hard
    if "title_mismatch_soft" in reasons and any(
        r.startswith("missing_sections") for r in reasons
    ):
        ok = False
    return ok, reasons


def is_garbage_tile(text: str) -> bool:
    if not text or len(text.strip()) < 20:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    num_cells = 0
    for ln in lines:
        if ln.startswith("|") and re.search(r"\|\s*\d{1,4}\s*\|", ln):
            # строка только из чисел/пустых ячеек
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if cells and all(re.fullmatch(r"\d{0,4}", c or "") for c in cells):
                num_cells += 1
    if num_cells >= 25:
        return True
    axis_run = sum(1 for ln in lines if re.match(r"^(?:[-*•]\s*)?ось\s+\d+", ln, re.I))
    if axis_run >= 20:
        return True
    bare = sum(1 for ln in lines if re.match(r"^(?:[-*•]\s*)?\d{1,4}$", ln))
    return bare >= 30


def _crop_frac(im: Image.Image, x0: float, y0: float, x1: float, y1: float) -> Image.Image:
    w, h = im.size
    return im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))


def generic_describe_zones(
    im: Image.Image, kind: SheetKind, lean: bool = False
) -> list[tuple[str, Image.Image, str]]:
    jobs: list[tuple[str, Image.Image, str]] = []
    if kind in ("plan", "scheme", "mixed"):
        quads = [
            ("zone_NW", 0.0, 0.0, 0.5, 0.5),
            ("zone_NE", 0.5, 0.0, 1.0, 0.5),
            ("zone_SW", 0.0, 0.5, 0.5, 0.88),
            ("zone_SE", 0.5, 0.5, 0.82, 0.88),
        ]
        # Вне lean каждая четверть описывается заново — отсюда три версии одного
        # места на листе, расходящиеся между собой. В lean зона только вычитывает
        # текст, а картину листа целиком даёт единственный PASS-A.
        quad_prompt = PROMPT_ZONE_LEAN if lean else PROMPT_ZONE_SPATIAL
        for name, a, b, c, d in quads:
            jobs.append((name, _crop_frac(im, a, b, c, d), quad_prompt))
        # правая полоса — экспликация/легенда: EXTRACT, не краткое описание
        jobs.append(
            (
                "zone_right_extract",
                _crop_frac(im, 0.68, 0.02, 0.99, 0.78),
                PROMPT_ZONE_EXTRACT,
            )
        )
        jobs.append(
            (
                "zone_legend_bottom",
                _crop_frac(im, 0.55, 0.55, 0.99, 0.82),
                PROMPT_ZONE_EXTRACT,
            )
        )
        jobs.append(
            (
                "zone_stamp",
                _crop_frac(im, 0.70, 0.78, 1.0, 1.0),
                PROMPT_ZONE_EXTRACT,
            )
        )
    elif kind == "table":
        jobs.append(
            (
                "zone_main_table",
                _crop_frac(im, 0.03, 0.05, 0.97, 0.78),
                PROMPT_TILE_TABLE,
            )
        )
        jobs.append(
            (
                "zone_table_upper",
                _crop_frac(im, 0.03, 0.05, 0.97, 0.45),
                PROMPT_TILE_TABLE,
            )
        )
        jobs.append(
            (
                "zone_table_lower",
                _crop_frac(im, 0.03, 0.40, 0.97, 0.82),
                PROMPT_TILE_TABLE,
            )
        )
        jobs.append(
            (
                "zone_stamp",
                _crop_frac(im, 0.55, 0.78, 1.0, 1.0),
                PROMPT_ZONE_EXTRACT,
            )
        )
    elif kind == "text":
        jobs.append(
            (
                "zone_full_text",
                _crop_frac(im, 0.05, 0.05, 0.95, 0.85),
                PROMPT_TILE_TEXT,
            )
        )
    return jobs


def passport_markdown(passport: SheetPassport) -> str:
    return (
        f"- kind: `{passport.kind}`\n"
        f"- size_pt: {passport.width_pt:.0f}×{passport.height_pt:.0f}\n"
        f"- text_len: {passport.text_len}\n"
        f"- reasons: {', '.join(passport.reasons) or '—'}\n"
        f"- title_hints: {'; '.join(passport.title_hints[:4]) or '—'}\n"
        f"- labels_sample: {', '.join(passport.unique_labels[:25]) or '—'}\n"
    )
