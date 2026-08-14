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


def system_for(kind: SheetKind) -> str:
    return {
        "plan": SYSTEM_DESC_PLAN,
        "scheme": SYSTEM_DESC_SCHEME,
        "table": SYSTEM_DESC_TABLE,
        "text": SYSTEM_DESC_TEXT,
        "mixed": SYSTEM_DESC_PLAN,
    }[kind]


def prompt_desc_for(kind: SheetKind, passport: SheetPassport) -> str:
    base = {
        "plan": PROMPT_DESC_PLAN,
        "scheme": PROMPT_DESC_SCHEME,
        "table": PROMPT_DESC_TABLE,
        "text": PROMPT_DESC_TEXT,
        "mixed": PROMPT_DESC_PLAN,
    }[kind]
    return (
        base
        + "\n\n### Якоря с текстового слоя PDF (могут быть неполными)\n"
        + passport.context_pack()
    )


def prompt_tile_for(kind: SheetKind, base_extract: str) -> str:
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


def pass_a_quality_ok(desc: str, passport: SheetPassport) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    min_len = {
        "plan": 5500,
        "scheme": 4500,
        "table": 4500,
        "text": 3000,
        "mixed": 5000,
    }.get(passport.kind, 3000)
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
        needed = ("что изображено", "из чего состоит", "где расположен", "как связан")
        missing = [n for n in needed if n not in low]
        if len(missing) >= 3:
            reasons.append("missing_sections:" + ",".join(missing))
        if re.search(r"(?i)yekaterinburg|жилы(е|х)\s+дом", desc):
            reasons.append("likely_hallucinated_object")
        if "полный читаемый текст" not in low and table_rows < 5:
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
    im: Image.Image, kind: SheetKind
) -> list[tuple[str, Image.Image, str]]:
    jobs: list[tuple[str, Image.Image, str]] = []
    if kind in ("plan", "scheme", "mixed"):
        quads = [
            ("zone_NW", 0.0, 0.0, 0.5, 0.5),
            ("zone_NE", 0.5, 0.0, 1.0, 0.5),
            ("zone_SW", 0.0, 0.5, 0.5, 0.88),
            ("zone_SE", 0.5, 0.5, 0.82, 0.88),
        ]
        for name, a, b, c, d in quads:
            jobs.append((name, _crop_frac(im, a, b, c, d), PROMPT_ZONE_SPATIAL))
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
