#!/usr/bin/env python3
"""Hugging Face Inference Providers — бенч PDF→MD кандидатов под 4×A16.

Требует HF_TOKEN в .env (см. .env.example).
Каталог моделей: HF_MODEL_CANDIDATES.md / CATALOG ниже.

Примеры:
  python hf_api_bench.py --list
  python hf_api_bench.py --role vlm --model qwen3vl-8b --pages 1
  python hf_api_bench.py --role vlm --model qwen3vl-32b --pages 2,4 --high-dpi --stamp-crop
  python hf_api_bench.py --pipeline --model qwen3vl-32b --pages 2,4,5 --high-dpi --stamp-crop
  python hf_api_bench.py --role vlm --model qwen3vl-32b --pages 2,4 --no-two-pass
  python hf_api_bench.py --role synth --model qwen25-32b --draft bench_3pages_qwen_instruct.md --pages 1,3,5
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import fitz
from PIL import Image

# Windows-консоль (cp1251) роняет print с '→'/кириллицей — принудительно UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent
DEFAULT_PAGES = "1,3,5"
HARD_PAGES = frozenset({2, 4})  # ОДИ / КР3 — слабые страницы
DEFAULT_OCR_DIR = ROOT / "bench_ocr_all6.md.pages"

HIGH_DPI_DEFAULTS = {
    "page_max": 2800,
    "tile_max": 750,
    "image_max_px": 1600,
    "jpeg_quality": 92,
    "max_tiles": 8,
}

PROMPT_STAMP = (
    "Это ШТАМП (угловая рамка) строительного чертежа. Выпиши ВЕСЬ текст точно:\n"
    "- ШИФР документа полностью (формат NN-ХСА-1/25-XXX, например 28-ХСА-1/25-ОДИ);\n"
    "- организация-проектировщик (ООО «…»);\n"
    "- наименование объекта / стройки;\n"
    "- ФИО (ГИП, разработал, проверил), стадия, лист, формат.\n"
    "Построчно, без выдумок. Кириллицу не латинизировать."
)

PROMPT_ZONE_TEP = (
    "Это таблица ТЕХНИКО-ЭКОНОМИЧЕСКИХ ПОКАЗАТЕЛЕЙ (ТЭП). "
    "Выпиши markdown-таблицей КАЖДУЮ строку: наименование показателя и ЕГО ЧИСЛО "
    "(площадь участка, площадь застройки, площадь озеленения/твёрдых покрытий и т.д.). "
    "Числа переноси ТОЧНО, с пробелами-разрядами как на листе (например 300 236, 62 459). "
    "Заголовок таблицы тоже выпиши."
)

PROMPT_SECTION_TITLES = (
    "Это верхняя полоса листа с ЗАГОЛОВКАМИ разрезов/схем. "
    "Выпиши построчно КАЖДЫЙ заголовок целиком, сохраняя римские номера линий "
    "(XII-XII, XIII-XIII, XIV-XIV, IV-IV, V-V…), оси и номера блоков. "
    "Например: «Разрез по оси Д в осях 1-15 для блока 1», "
    "«Геологический разрез по линии XII-XII (по отчету ИГИ)»."
)

PROMPT_ZONE_TABLE = (
    "Это зона ТАБЛИЦ / экспликации / легенды строительного листа. "
    "Извлеки таблицы строго в markdown (| col |). "
    "Сохрани коды объектов (1а, 1б, 1ш1…), ИГЭ, СКВ, числа ячеек. "
    "Не выдумывай и не дублируй одинаковые строки."
)

PROMPT_ZONE_LEGEND = (
    "Это зона УСЛОВНЫХ ОБОЗНАЧЕНИЙ / легенды. "
    "Перечисли каждый символ и его расшифровку списком. "
    "Сохрани номера ИГЭ в кружках и подписи слоёв."
)


def _crop_frac(im: Image.Image, x0: float, y0: float, x1: float, y1: float) -> Image.Image:
    w, h = im.size
    return im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))


def crop_content_zones(im: Image.Image, page_num: int) -> list[tuple[str, Image.Image, str]]:
    """Зоны с высокой плотностью фактов (координаты выверены по рендеру листов).

    Штамп (шифр/организация/объект) даём на КАЖДОМ листе — это самые частые
    промахи чек-листа (doc_code / design_org / object_name).
    """
    jobs: list[tuple[str, Image.Image, str]] = []

    # Штамп — правый нижний угол на всех форматных листах.
    jobs.append(("zone_stamp", _crop_frac(im, 0.83, 0.79, 1.0, 1.0), PROMPT_STAMP))

    if page_num == 1:
        # ПБ (A3): гидравлика сверху, блок автоматики снизу (4 верт. блока).
        # Промахи чек-листа — маркировка контроллеров, шкафы, шины, сигналы У1-У4.
        jobs.append(
            (
                "zone_auto_controllers",
                _crop_frac(im, 0.02, 0.49, 0.82, 0.63),
                PROMPT_ZONE_LEGEND
                + " Верхний ряд блока автоматики: 4 чёрных контроллера. Выпиши "
                "маркировку контроллеров (ищи «C2000-AP8» — подпись в левом верхнем "
                "углу каждого блока) и интерфейсные шины: «К2000-КДЛ-2С», «ДПЛС», "
                "«ModBus/RTU».",
            )
        )
        jobs.append(
            (
                "zone_auto_l",
                _crop_frac(im, 0.02, 0.58, 0.45, 0.93),
                PROMPT_ZONE_LEGEND
                + " Левая половина блока автоматики (шкафы ШУПН и ШУЗ). Выпиши "
                "названия шкафов (ШУПН, ШУЗ) и все сигналы-подписи, обязательно коды "
                "У1, У2, У3, У4 («сигнал на закрытие/открытие затвора с эл/приводом») "
                "и номера 01-04, 06-07, 101-118, 201-211.",
            )
        )
        jobs.append(
            (
                "zone_auto_r",
                _crop_frac(im, 0.44, 0.58, 0.82, 0.93),
                PROMPT_ZONE_LEGEND
                + " Правая половина блока автоматики (шкафы ШКР). Выпиши названия "
                "шкафов «ШКР(рез.№1)», «ШКР(рез.№2)» и сигналы-подписи с номерами "
                "119-134, 08-11, 501-506, 601-606.",
            )
        )
    elif page_num == 2:
        # ОДИ (A1x3): таблицы прижаты к правому краю листа.
        jobs.append(
            (
                "zone_explication",
                _crop_frac(im, 0.78, 0.0, 1.0, 0.14),
                PROMPT_ZONE_TABLE
                + " Заголовок «ЭКСПЛИКАЦИЯ ЗДАНИЙ И СООРУЖЕНИЙ», "
                "коды 1а/1б/1в/1е/1к1/1т/1ш + названия, блоки Industrial City / Block N.",
            )
        )
        jobs.append(("zone_tep", _crop_frac(im, 0.78, 0.13, 1.0, 0.33), PROMPT_ZONE_TEP))
        jobs.append(
            (
                "zone_right_legend",
                _crop_frac(im, 0.78, 0.31, 1.0, 0.60),
                PROMPT_ZONE_LEGEND
                + " Условные обозначения, пути МГН, кадастровые границы, бортовой камень.",
            )
        )
    elif page_num == 3:
        # КР1: шапки разрезов сверху, таблица грунтов в центре, легенда справа.
        jobs.append(
            (
                "zone_section_titles",
                _crop_frac(im, 0.25, 0.0, 1.0, 0.13),
                PROMPT_SECTION_TITLES,
            )
        )
        jobs.append(
            (
                "zone_soil_table",
                _crop_frac(im, 0.25, 0.40, 0.63, 0.98),
                PROMPT_ZONE_TABLE
                + " «Сравнительная таблица… физико-механических свойств грунтов»: "
                "шапки колонок (Удельное сцепление, Угол внутреннего трения, "
                "Модуль деформации, Плотность грунта), строки ИГЭ-1…ИГЭ-12.",
            )
        )
        jobs.append(
            (
                "zone_right_legend",
                _crop_frac(im, 0.60, 0.40, 0.90, 0.80),
                PROMPT_ZONE_LEGEND + " Номера ИГЭ в кружках и подписи слоёв.",
            )
        )
    elif page_num == 4:
        # КР3 (A2x3): широкая плотная таблица грунтов — режем на 2 половины с
        # перекрытием, иначе мелкие числа/шапки колонок нечитаемы одним кропом.
        _kr3_table = (
            PROMPT_ZONE_TABLE
            + " Это часть Таблицы 7.2.1 «…физико-механических свойств грунтов». "
            "Выпиши markdown с шапками колонок и всеми числами. Обязательно ищи: "
            "групп-заголовки (Физико-механические характеристики грунтов, "
            "Дисперсные грунты); колонки (Удельное сцепление С кПа, "
            "Угол внутреннего трения, Модуль деформации Е МПа, Плотность грунта, "
            "Коэф. пористости); источники (по СП, Стат. зондирование, "
            "Лаб. исследования, Архивные, Рекомендуемое); α=0,85 / α=0,95; "
            "строки ИГЭ-1…ИГЭ-12."
        )
        jobs.append(
            ("zone_soil_table_l", _crop_frac(im, 0.585, 0.50, 0.735, 0.96), _kr3_table)
        )
        jobs.append(
            ("zone_soil_table_r", _crop_frac(im, 0.72, 0.50, 0.875, 0.96), _kr3_table)
        )
        jobs.append(
            (
                "zone_section_titles",
                _crop_frac(im, 0.0, 0.0, 0.85, 0.10),
                PROMPT_SECTION_TITLES
                + " Здесь: «Схема расположения фундаментов на геологическом разрезе "
                "по линии IV-IV / V-V / VI-VI / VII-VII (по отчету ИГИ)».",
            )
        )
        jobs.append(
            (
                "zone_mid_legend",
                _crop_frac(im, 0.86, 0.50, 1.0, 0.90),
                PROMPT_ZONE_LEGEND + " Номера ИГЭ в кружках и подписи слоёв.",
            )
        )
    elif page_num == 5:
        jobs.append(
            (
                "zone_full_table",
                _crop_frac(im, 0.05, 0.05, 0.95, 0.82),
                PROMPT_ZONE_TABLE
                + " Полная таблица физ-мех характеристик по всем ИГЭ.",
            )
        )
    return jobs

# ── каталог: только то, что реалистично потом поставить на 4×A16 ─────────────
# preferred_provider — live Inference Provider (auto часто падает с model_not_supported)

@dataclass(frozen=True)
class ModelSpec:
    id: str
    hf_id: str
    role: str  # vlm | synth
    fit: str  # easy | awq | tight
    preferred_provider: str
    notes: str = ""
    prompt_style: str = "vlm"  # vlm | deepseek_ocr


CATALOG: list[ModelSpec] = [
    # VLM (проверено: live mapping на Hub)
    ModelSpec(
        "qwen3vl-8b",
        "Qwen/Qwen3-VL-8B-Instruct",
        "vlm",
        "easy",
        "featherless-ai",
        "класс локального winner; API smoke",
    ),
    ModelSpec(
        "qwen25vl-7b",
        "Qwen/Qwen2.5-VL-7B-Instruct",
        "vlm",
        "easy",
        "featherless-ai",
        "иногда 503 у featherless",
    ),
    ModelSpec(
        "qwen3vl-30b-a3b",
        "Qwen/Qwen3-VL-30B-A3B-Instruct",
        "vlm",
        "awq",
        "novita",
        "MoE, лучший серверный VLM-кандидат; alt: featherless-ai",
    ),
    ModelSpec(
        "qwen3vl-32b",
        "Qwen/Qwen3-VL-32B-Instruct",
        "vlm",
        "awq",
        "featherless-ai",
        "dense 32B — сильнее общий VLM на hard pages",
    ),
    # ── новая волна (2026-08): мультимодальные Qwen3.6, GLM-V, Kimi, gemma-4 ──
    ModelSpec(
        "qwen36-35b-a3b",
        "Qwen/Qwen3.6-35B-A3B",
        "vlm",
        "awq",
        "deepinfra",
        "мультимодальный MoE A3B — прямой наследник серверного класса; "
        "thinking отключается через chat_template_kwargs; alt: scaleway",
    ),
    ModelSpec(
        "qwen36-27b",
        "Qwen/Qwen3.6-27B",
        "vlm",
        "awq",
        "deepinfra",
        "dense 27B мультимодальный, наследник 32B; thinking отключён; alt: ovhcloud",
    ),
    ModelSpec(
        "qwen3vl-235b",
        "Qwen/Qwen3-VL-235B-A22B-Instruct",
        "vlm",
        "tight",
        "deepinfra",
        "MoE 235B-A22B — потолок качества Qwen VL; на 4×A16 НЕ влезет, "
        "только API ($0.2/$0.88 deepinfra); alt: novita",
    ),
    ModelSpec(
        "glm-46v-flash",
        "zai-org/GLM-4.6V-Flash",
        "vlm",
        "easy",
        "novita",
        "дешёвый vision 10B ($0.3/$0.9); в smoke искажал фразы — проверить",
    ),
    ModelSpec(
        "glm-45v",
        "zai-org/GLM-4.5V",
        "vlm",
        "tight",
        "novita",
        "MoE 108B-A12B vision ($0.6/$1.8); на 4×A16 не влезет",
    ),
    ModelSpec(
        "kimi-k3",
        "moonshotai/Kimi-K3",
        "vlm",
        "tight",
        "deepinfra",
        "Moonshot триллионник, мультимодальный; ДОРОГОЙ ($2.85/$14.25) — "
        "только как потолок качества, не для прода",
    ),
    ModelSpec(
        "gemma4-26b-a4b",
        "google/gemma-4-26B-A4B-it",
        "vlm",
        "awq",
        "deepinfra",
        "MoE A4B, самый дешёвый ($0.07/$0.34); gemma3 выдумывала — "
        "проверить галлюцинации; alt: novita",
    ),
    ModelSpec(
        "deepseek-ocr",
        "deepseek-ai/DeepSeek-OCR",
        "vlm",
        "easy",
        "novita",
        "doc-OCR specialist; preset prompts, single-turn",
        prompt_style="deepseek_ocr",
    ),
    ModelSpec(
        "gemma3-12b",
        "google/gemma-3-12b-it",
        "vlm",
        "easy",
        "deepinfra",
        "Google multimodal; влезет на 4×A16",
    ),
    ModelSpec(
        "gemma3-27b",
        "google/gemma-3-27b-it",
        "vlm",
        "awq",
        "deepinfra",
        "крупнее; alt: featherless-ai / scaleway",
    ),
    ModelSpec(
        "gemma3-4b",
        "google/gemma-3-4b-it",
        "vlm",
        "easy",
        "featherless-ai",
        "быстрый sanity-check",
    ),
    # Text synth
    ModelSpec(
        "qwen25-14b",
        "Qwen/Qwen2.5-14B-Instruct",
        "synth",
        "easy",
        "featherless-ai",
        "",
    ),
    ModelSpec(
        "qwen25-32b",
        "Qwen/Qwen2.5-32B-Instruct",
        "synth",
        "awq",
        "featherless-ai",
        "исторический synthesis рычаг; бывает busy",
    ),
    ModelSpec(
        "gemma2-27b",
        "google/gemma-2-27b-it",
        "synth",
        "easy",
        "featherless-ai",
        "",
    ),
    ModelSpec(
        "llama31-8b",
        "meta-llama/Llama-3.1-8B-Instruct",
        "synth",
        "easy",
        "novita",
        "alt: deepinfra / nscale / featherless-ai",
    ),
    ModelSpec(
        "qwen35-35b-a3b",
        "Qwen/Qwen3.5-35B-A3B",
        "synth",
        "awq",
        "novita",
        "уже на сервере GPTQ; alt: deepinfra",
    ),
]

SYSTEM_VLM = (
    "Ты извлекаешь содержимое русских строительных чертежей в Markdown. "
    "Правила:\n"
    "1) Текст и числа — только то, что видно на изображении. Не выдумывай.\n"
    "2) Таблицы — ТОЛЬКО markdown с шапкой (| col |). Повторяющиеся строки НЕ дублируй.\n"
    "3) Кириллицу не латинизировать (В1≠B1, ХСА≠XCA, ИГЭ≠IGE).\n"
    "4) Без предисловий и рассуждений вне структуры.\n"
    "5) На чертеже почти всегда есть текст: размеры, оси, подписи, штриховка с номерами. "
    "Пиши (пусто) ТОЛЬКО если на фрагменте реально нет ни одной буквы/цифры.\n"
    "6) Не перечисляй голые номера подряд (1,2,3…100) без названий объектов.\n"
    "7) Графические символы/значки описывай СЛОВАМИ по-русски (например «круг», "
    "«треугольник», «стрелка вниз», «задвижка»). НИКОГДА не вставляй эмодзи, "
    "смайлы или значки Unicode — только текст."
)

PROMPT_TILE = (
    "Извлеки ВЕСЬ читаемый текст с ЭТОГО фрагмента.\n"
    "Таблицы → полноценный markdown (| ячейка | ячейка |) с шапкой и ВСЕМИ строками "
    "(не обрезай середину, не пиши «…» вместо строк). Каждая строка таблицы — "
    "отдельная строка markdown.\n"
    "Списки/легенды → маркированный список.\n"
    "Коды, DN, шифры, ФИО, отметки, оси, размеры — сохрани точно.\n"
    "Не повторяй одну и ту же ячейку/число/метку (В1, W1, К1) десятки раз — "
    "достаточно уникальных значений.\n"
    "Графические значки описывай словами (например «стрелка», «круг»); "
    "эмодзи и Unicode-смайлы не использовать.\n"
    "Не пиши (пусто), если видишь хоть один символ/цифру/подпись."
)

SYSTEM_DESC = (
    "Ты подробно описываешь русские строительные чертежи и схемы для Q&A. "
    "Цель: по тексту ответа человек (и LLM) должен понять, КАК выглядит лист, "
    "где что стоит и как связано — без просмотра картинки. Пиши МАКСИМАЛЬНО подробно. "
    "Факты только с чертежа; не выдумывай коды/диаметры/помещения. "
    "Кириллицу не латинизировать (В1≠B1, ХСА≠XCA). "
    "ЗАПРЕЩЕНО: голые номера 1..N без имён; штамповать одно название на разные номера; "
    "эмодзи/смайлы; повторять одну и ту же короткую метку (В1, W1, К1) десятки раз — "
    "достаточно указать метку один раз и где она встречается. "
    "Графические символы — словами («кран шаровый», «стрелка вниз»). "
    "Если текст на чертеже наложился/нечитаем — напиши «неразборчиво», не выдумывай."
)

PROMPT_DESC = (
    "Опиши ВЕСЬ лист МАКСИМАЛЬНО ПОДРОБНО (эталон для Q&A по чертежу). "
    "Чем детальнее пространственное описание — тем лучше. Формат:\n\n"
    "## Описание изображения\n\n"
    "### Описание: <точное название листа с чертежа>\n\n"
    "**ЧТО ИЗОБРАЖЕНО:** 3–6 предложений: тип листа (схема/план/разрез/таблица), "
    "система (водоснабжение/отопление/…), объект/блок, масштаб если виден, "
    "общий характер графики (сети, здания, штамп, легенда).\n\n"
    "**ИЗ ЧЕГО СОСТОИТ:** развёрнутый маркированный список:\n"
    "- системы и трубы (В1/В2/Т3/Т4/К1/…, диаметры φ/DN/∅, материалы если есть);\n"
    "- арматура и приборы (краны, задвижки, клапаны, фильтры, счётчики, манометры, "
    "пожарные гидранты) с марками/позициями, если читаются;\n"
    "- здания/помещения/зоны с номерами и названиями;\n"
    "- уровни чистого пола (Ур.ч.п.), отметки, уклоны;\n"
    "- вводы, врезки, точки подключения, футляры, углы поворота (УП);\n"
    "- легенда/условные обозначения (каждый знак — словами + назначение);\n"
    "- штамп (шифр, организация, стадия, лист, формат).\n"
    "Короткие метки сетей (В1, W1, К1…) перечисли УНИКАЛЬНЫМИ, без копипаста "
    "по 50 раз — напиши «на плане многократно: В1, W1, К1…».\n\n"
    "**ГДЕ РАСПОЛОЖЕНЫ ЭЛЕМЕНТЫ:** подробно по зонам листа "
    "(лево/право/верх/низ/центр, углы) — что в какой зоне, куда идут магистрали, "
    "где легенда и штамп.\n\n"
    "**КАК СВЯЗАНЫ:** логика как поток: откуда приходит среда → через какие "
    "узлы/оборудование → куда уходит; границы очередей; ссылки «далее см. раздел…»; "
    "связь легенды с элементами на схеме.\n\n"
    "Если на листе несколько разных схем/блоков — отдельный "
    "### Описание: … для каждого.\n"
    "Экспликацию помещений/зданий — кодами и названиями (только читаемые, макс. 40). "
    "Не пиши про скважины/ИГЭ, если их нет. "
    "Таблицы на листе опиши структурой (шапки колонок + что в строках), "
    "не выдумывай пропущенные числа."
)

# Зональные подсказки для hard pages (добавляются к tile-промпту)
ZONE_HINTS = {
    2: (
        " Это лист ОДИ (генплан). Приоритет: таблица «ЭКСПЛИКАЦИЯ ЗДАНИЙ», "
        "ТЭП, условные обозначения, коды 1а/1б/1в/1е/1к1/1т/1ш, штамп 28-ХСА."
    ),
    4: (
        " Это лист КР (геология). Приоритет: таблица ИГЭ, условные обозначения "
        "слоёв ①–⑫, скважины СКВ с отметками устья/забоя/УГВ, штамп 28-ХСА-КР."
    ),
    5: (
        " Это лист КР (таблица грунтов). Приоритет: полная markdown-таблица "
        "физ-мех характеристик по ИГЭ-1…ИГЭ-12, все числовые ячейки."
    ),
}

# DeepSeek-OCR: только preset (Novita/docs). Single-turn, без system.
DEEPSEEK_OCR_PROMPTS = {
    "markdown": "<|grounding|>Convert the document to markdown.",
    "ocr": "<|grounding|>OCR this image.",
    "free": "Free OCR.",
    "figure": "Parse the figure.",
}

# PASS-T: лист-таблица → один вызов «перенеси таблицу как в исходнике».
# Тайлы таблицу разрывают (шапка в одном фрагменте, строки в другом);
# после авторотации целый лист в высоком разрешении работает лучше —
# проверено на стр.5 эталона (КР5): все строки/ячейки сходятся с исходником.
SYSTEM_TABLE_EXACT = (
    "Ты переносишь таблицы с русских строительных чертежей в Markdown БЕЗ ПОТЕРЬ. "
    "Кириллицу не латинизировать (ИГЭ≠IGE, В1≠B1). Числа копируй точно, "
    "запятую как десятичный разделитель сохраняй. Не выдумывай значения."
)

PROMPT_TABLE_EXACT = (
    "Воспроизведи главную таблицу листа ОДНОЙ markdown-таблицей (GFM), "
    "максимально близко к исходнику:\n"
    "1) Многоуровневую шапку сплющи в одну строку колонок вида «Группа: подколонка» "
    "(например «Удельное сцепление С, кПа: по СП»).\n"
    "2) Первая колонка — метки строк (ИГЭ-…, позиции, номера) с названиями.\n"
    "3) Сохрани ВСЕ строки и ВСЕ ячейки. Пустая ячейка → «-». Ничего не пропускай "
    "и не сокращай («…» запрещено). Объединённые ячейки повторяй по строкам.\n"
    "4) Если строка содержит сводные значения на несколько колонок (Rб, ρб, К разм "
    "и т.п.) — запиши их текстом в этой строке, не размножай пустые ячейки.\n"
    "5) После таблицы отдельными строками: заголовок листа, примечания под таблицей, "
    "шифр из штампа.\n"
    "Если на листе несколько таблиц — каждая отдельной GFM-таблицей с подзаголовком. "
    "Только markdown, без рассуждений."
)

SYSTEM_SYNTH = (
    "Ты консервативный редактор извлечений с русских строительных чертежей. "
    "Объедини черновик и OCR БЕЗ сжатия и БЕЗ выдумок. "
    "Сохрани все коды/DN/шифры. Кириллицу не латинизировать. "
    "Без рассуждений."
)

SYSTEM_SYNTH_STRICT = (
    "Ты склеиваешь два источника. ЖЁСТКИЕ правила:\n"
    "1) Сохрани ВСЕ непустые строки/факты из ЧЕРНОВИКА — не сокращай, не перефразируй таблицы.\n"
    "2) Добавь из OCR только то, чего ещё нет в черновике (коды, DN, шифры, ФИО, названия).\n"
    "3) ЗАПРЕЩЕНО выдумывать числа/скважины/марки.\n"
    "4) Кириллицу не латинизировать.\n"
    "5) Без предисловий и рассуждений — только итоговый текст."
)


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def get_spec(model_id: str) -> ModelSpec:
    for m in CATALOG:
        if m.id == model_id or m.hf_id == model_id:
            return m
    raise SystemExit(
        f"Неизвестный model id: {model_id}\nДоступно: " + ", ".join(x.id for x in CATALOG)
    )


def find_etalon_pdf() -> Path:
    for p in ROOT.iterdir():
        if p.is_dir() and p.name.startswith("ЭТАЛОН"):
            pdfs = list(p.glob("*.pdf"))
            if pdfs:
                return pdfs[0]
    raise FileNotFoundError("Эталонный PDF не найден")


def parse_pages(s: str, total: int) -> list[int]:
    out: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(max(1, int(a)), min(total, int(b)) + 1))
        else:
            n = int(part)
            if 1 <= n <= total:
                out.append(n)
    return sorted(set(out))


def dpi_for_budget(page: fitz.Page, page_max: int) -> int:
    longest = max(page.rect.width, page.rect.height)
    if longest <= 0:
        return 72
    return max(36, int(page_max / (longest / 72.0)))


_PAGE_ROTATION_CACHE: dict[tuple[str, int], int] = {}


def detect_rotation_osd(im: Image.Image, *, min_conf: float = 1.0) -> int:
    """Ориентация текста через Tesseract OSD: 0/90/180/270 (по часовой,
    сколько нужно повернуть, чтобы текст стал горизонтальным).

    Кейс: таблицы грунтов (КР5) свёрстаны на листе с поворотом 90° —
    VLM читает вертикальные заголовки по буквам и теряет структуру.
    При недоступном tesseract / низкой уверенности — 0 (не вращаем).
    """
    try:
        import pytesseract

        import local_ocr

        cmd = local_ocr.resolve_tesseract_cmd()
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        small = im.copy()
        small.thumbnail((2000, 2000))
        osd = pytesseract.image_to_osd(small, config="--psm 0")
        rot, conf = 0, 0.0
        for line in osd.splitlines():
            if line.startswith("Rotate:"):
                rot = int(line.split(":", 1)[1])
            elif line.startswith("Orientation confidence:"):
                conf = float(line.split(":", 1)[1])
        return rot if conf >= min_conf else 0
    except Exception:
        return 0


def render_page(page: fitz.Page, page_max: int) -> Image.Image:
    dpi = dpi_for_budget(page, page_max)
    pix = page.get_pixmap(dpi=dpi)
    im = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    if max(im.size) > page_max:
        scale = page_max / float(max(im.size))
        im = im.resize(
            (max(1, int(im.width * scale)), max(1, int(im.height * scale))),
            Image.LANCZOS,
        )
    key = (getattr(page.parent, "name", "") or "?", page.number)
    if key not in _PAGE_ROTATION_CACHE:
        _PAGE_ROTATION_CACHE[key] = detect_rotation_osd(im)
        if _PAGE_ROTATION_CACHE[key]:
            print(
                f"  [rotation] p{page.number + 1}: повёрнутый лист, "
                f"вращаю на {_PAGE_ROTATION_CACHE[key]}° по часовой",
                flush=True,
            )
    rot = _PAGE_ROTATION_CACHE[key]
    if rot:
        im = im.rotate(-rot, expand=True)
    return im


def tile_grid(w: int, h: int, tile_max: int, max_tiles: int = 6) -> tuple[int, int]:
    cols = max(1, -(-w // tile_max))
    rows = max(1, -(-h // tile_max))
    while rows * cols > max_tiles:
        if cols > 1 and rows > 1:
            # режем более «длинную» сторону
            if (w / cols) >= (h / rows):
                cols -= 1
            else:
                rows -= 1
        elif cols > 1:
            cols -= 1
        elif rows > 1:
            rows -= 1
        else:
            break
    return max(1, rows), max(1, cols)


def image_to_data_url(im: Image.Image, max_px: int = 1280, quality: int = 85) -> str:
    if im.mode != "RGB":
        im = im.convert("RGB")
    if max(im.size) > max_px:
        scale = max_px / float(max(im.size))
        im = im.resize(
            (max(1, int(im.width * scale)), max(1, int(im.height * scale))),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def make_client(token: str, provider: str | None):
    from huggingface_hub import InferenceClient

    kwargs: dict = {"token": token, "timeout": float(os.environ.get("HF_TIMEOUT", "180"))}
    if provider and provider != "auto":
        kwargs["provider"] = provider
    return InferenceClient(**kwargs)


def resolve_provider(spec: ModelSpec, override: str | None, token: str) -> str:
    """Явный provider обязателен: HF_PROVIDER=auto часто даёт model_not_supported."""
    if override and override != "auto":
        return override
    env = os.environ.get("HF_PROVIDER", "").strip()
    if env and env != "auto":
        return env
    # live mapping с Hub, иначе preferred из каталога
    try:
        from huggingface_hub import HfApi

        info = HfApi(token=token).model_info(
            spec.hf_id, expand=["inferenceProviderMapping"]
        )
        mapping = info.inference_provider_mapping or []
        live = [m.provider for m in mapping if getattr(m, "status", None) == "live"]
        if spec.preferred_provider in live:
            return spec.preferred_provider
        if live:
            return live[0]
    except Exception as e:
        print(f"  [provider lookup] {e}", flush=True)
    return spec.preferred_provider


@dataclass
class UsageTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    retries: int = 0
    failed_tiles: int = 0

    def add(self, usage: dict | None) -> None:
        if not usage:
            return
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.total_tokens += int(usage.get("total_tokens") or 0)
        self.calls += 1

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "retries": self.retries,
            "failed_tiles": self.failed_tiles,
        }


def extract_usage(resp) -> dict:
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, dict):
        usage = resp.get("usage")
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if isinstance(usage, dict):
        return {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def call_with_retries(fn, *, retries: int, base_delay: float, usage: UsageTotals):
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            return fn()
        except Exception as e:
            last = e
            if attempt + 1 >= retries:
                break
            usage.retries += 1
            delay = base_delay * (2**attempt)
            print(f"    retry {attempt + 1}/{retries - 1} in {delay:.0f}s: {e}", flush=True)
            time.sleep(delay)
    assert last is not None
    raise last


# Гибридные thinking-модели: без этого весь max_tokens уходит в рассуждения,
# а content возвращается пустым (проверено на Qwen3.6 @ deepinfra).
NO_THINK_HF_IDS = {"Qwen/Qwen3.6-35B-A3B", "Qwen/Qwen3.6-27B"}


def _extra_body_for(model: str) -> dict | None:
    if model in NO_THINK_HF_IDS:
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return None


def chat_text(
    client,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 2000,
    *,
    usage: UsageTotals | None = None,
) -> str:
    resp = client.chat_completion(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        extra_body=_extra_body_for(model),
        max_tokens=max_tokens,
        temperature=0.0,
    )
    if usage is not None:
        usage.add(extract_usage(resp))
    return (resp.choices[0].message.content or "").strip()


def chat_vision(
    client,
    model: str,
    system: str,
    prompt: str,
    data_url: str,
    max_tokens: int = 1200,
    *,
    prompt_style: str = "vlm",
    usage: UsageTotals | None = None,
) -> str:
    if prompt_style == "deepseek_ocr":
        # Novita: image first, then preset text; no system / multi-turn.
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
    else:
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
    resp = client.chat_completion(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.0,
        extra_body=_extra_body_for(model),
    )
    if usage is not None:
        usage.add(extract_usage(resp))
    return (resp.choices[0].message.content or "").strip()


def vision_defaults(spec: ModelSpec) -> dict:
    """Более высокий DPI/тайлы для OCR-специалистов и крупных VLM."""
    if spec.prompt_style == "deepseek_ocr":
        # Через API нет NGram processor → короче max_tokens + anti-loop.
        return {
            "page_max": 2200,
            "tile_max": 1100,
            "image_max_px": 1400,
            "jpeg_quality": 92,
            "max_tokens": 1536,
            "max_tiles": 4,
        }
    if any(k in spec.id for k in ("32b", "30b", "235b", "35b-a3b", "36-27b", "45v", "k3")):
        return {
            "page_max": 2000,
            "tile_max": 700,
            "image_max_px": 1400,
            "jpeg_quality": 88,
            "max_tokens": 2500,
            "max_tiles": 6,
        }
    return {
        "page_max": 1760,
        "tile_max": 650,
        "image_max_px": 1280,
        "jpeg_quality": 85,
        "max_tokens": 1200,
        "max_tiles": 6,
    }


USE_ZONE_HINTS = True  # тест-сетовые подсказки по номеру страницы; off для чужих PDF


def tile_prompt_for(spec: ModelSpec, ocr_prompt: str, page_num: int | None = None) -> str:
    if spec.prompt_style == "deepseek_ocr":
        return DEEPSEEK_OCR_PROMPTS.get(ocr_prompt, DEEPSEEK_OCR_PROMPTS["markdown"])
    hint = ZONE_HINTS.get(page_num or 0, "") if USE_ZONE_HINTS else ""
    return PROMPT_TILE + hint


def collapse_repetition(text: str, *, min_unit: int = 12, max_repeats: int = 3) -> str:
    """Режет API-лупы (DeepSeek и обычный VLM: 17.00;17.00;… / 112.25×N)."""
    if not text or len(text) < 40:
        return text

    # 0) повторяющийся короткий токен/ячейка в любом месте (главный кейс тайлов)
    m = re.search(r"((?:[^\n]{2,40}?(?:;|\n))\s*)\1{5,}", text)
    if m:
        unit = m.group(1)
        return (
            text[: m.start()]
            + (unit * min(max_repeats, 2)).rstrip()
            + "\n[truncated-repeat]\n"
            + text[m.end() :]
        ).strip()

    # 1) явные подряд идущие одинаковые чанки с конца
    if len(text) >= min_unit * (max_repeats + 1):
        for unit_len in range(min(120, len(text) // (max_repeats + 1)), min_unit - 1, -1):
            unit = text[-unit_len:]
            if not unit.strip():
                continue
            streak = 1
            pos = len(text) - unit_len
            while pos - unit_len >= 0 and text[pos - unit_len : pos] == unit:
                streak += 1
                pos -= unit_len
            if streak >= max_repeats:
                keep_end = pos + unit_len * max_repeats
                return text[:keep_end].rstrip() + "\n[truncated-repeat]"

    # 2) fallback: любой повтор 6+ раз — сохраняем хвост (иначе union теряет зоны)
    m2 = re.search(r"(.{6,80}?)\1{6,}", text)
    if m2:
        unit = m2.group(1)
        # пустые markdown-ячейки « | | |» — не повод резать документ
        if re.fullmatch(r"[\s|]+", unit):
            return text
        return (
            text[: m2.start()]
            + (unit * min(max_repeats, 2))
            + "\n[truncated-repeat]\n"
            + text[m2.end() :]
        ).strip()
    return text


def dedupe_lines(text: str, *, max_same: int = 2) -> str:
    """Схлопывает подряд идущие одинаковые строки (табличный мусор)."""
    out: list[str] = []
    prev = None
    streak = 0
    for ln in text.splitlines():
        key = ln.strip()
        if key and key == prev:
            streak += 1
            if streak > max_same:
                continue
        else:
            streak = 1
            prev = key if key else None
        out.append(ln)
    return "\n".join(out)


def collapse_numeric_list(text: str, *, min_run: int = 20) -> str:
    """Режет длинные монотонные простыни маркированных чисел/отметок:
    «- 112.25», «- 112.15» … «- 98.30» или «- Ось 1» … (тут ловит stem-версия).
    Это шум с крупных чертежей (нечитаемая сетка осей/горизонталей), не факты.
    """
    num_re = re.compile(r"^\s*[-*•]\s+\d{1,5}([.,]\d+)?\s*$")
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if num_re.match(lines[i]):
            j = i
            while j < len(lines) and num_re.match(lines[j]):
                j += 1
            if j - i >= min_run:
                out.extend(lines[i : i + 3])
                out.append("[truncated-numeric-list]")
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def collapse_numbered_hallucination(text: str, *, min_repeats: int = 8) -> str:
    """Обрезает списки, где много строк подряд с одним «стеблем»
    (после номера/буллета, цифры → #): «1. Кабельная лаборатория — 1» …,
    «- Ось 1» … «- Ось 326».

    Markdown-таблицы (| … |) НЕ трогаем — иначе режутся реальные строки
    «| 1 | … | … | 12 | … |» как галлюцинация.
    """
    lines = text.splitlines()
    if len(lines) < min_repeats:
        return text

    def stem(line: str) -> str | None:
        s = line.strip()
        if not s:
            return None
        # настоящие таблицы бережём целиком
        if s.startswith("|"):
            return None
        m = re.match(r"^(?:\d{1,4}[\.\)]\s+|[-*•]\s+)", s)
        if not m:
            return None
        body = s[m.end() :]
        body = re.sub(r"\d+([.,]\d+)?", "#", body)
        body = re.sub(r"\s+", " ", body).strip().lower()
        return body if len(body) >= 4 else None

    out: list[str] = []
    i = 0
    while i < len(lines):
        st = stem(lines[i])
        if st is None:
            out.append(lines[i])
            i += 1
            continue
        j = i + 1
        while j < len(lines) and stem(lines[j]) == st:
            j += 1
        run_len = j - i
        if run_len >= min_repeats:
            out.extend(lines[i : i + 2])
            out.append("[truncated-numbered-hallucination]")
            i = j
        else:
            out.extend(lines[i:j])
            i = j
    return "\n".join(out)


def collapse_inline_repetition(
    text: str, *, min_repeats: int = 4, min_seg_len: int = 6
) -> str:
    """Схлопывает повтор фразы ВНУТРИ одной строки (кейс стр.35 ИОС2:
    «…подземные инженерные сети, подземные инженерные коммуникации, …» ×N).
    dedupe_lines работает построчно и такое пропускает.

    Markdown-таблицы (|…|) не трогаем: повтор одинаковых ячеек легален.
    """
    out: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if len(s) < 60 or s.startswith("|"):
            out.append(ln)
            continue
        segs = re.split(r"\s*[,;]\s*", s)
        if len(segs) < min_repeats:
            out.append(ln)
            continue
        norm = [re.sub(r"\s+", " ", x).strip().lower() for x in segs]
        counts: dict[str, int] = {}
        for n in norm:
            if len(n) >= min_seg_len:
                counts[n] = counts.get(n, 0) + 1
        if not counts or max(counts.values()) < min_repeats:
            out.append(ln)
            continue
        seen: set[str] = set()
        kept: list[str] = []
        for seg, n in zip(segs, norm):
            if len(n) >= min_seg_len and n in seen:
                continue
            seen.add(n)
            kept.append(seg)
        out.append(", ".join(kept) + " [inline-повторы схлопнуты]")
    return "\n".join(out)


def join_value_line_runs(text: str, *, min_run: int = 6, max_len: int = 18) -> str:
    """Серию «голых» значений столбиком (DN200 / NC / 102.55 / -1.380 …, каждая
    на своей строке) склеивает в одну строку через запятую: столбики одиночных
    меток с чертежа нечитаемы и раздувают вывод, а токены при склейке
    сохраняются (recall не страдает).

    Не трогаем: markdown-таблицы, заголовки, строки с пробелами (реальные
    фразы) и короткие серии (<min_run).
    """
    val_re = re.compile(r"[\w.,/№()+±-]+", re.UNICODE)

    def as_value(ln: str) -> str | None:
        s = ln.strip()
        if not s or len(s) > max_len or s.startswith(("|", "#")):
            return None
        s = re.sub(r"^[-*•]\s+", "", s)  # буллет только с пробелом: «-1.380» — не буллет
        if not s or " " in s:
            return None
        return s if val_re.fullmatch(s) else None

    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        v = as_value(lines[i])
        if v is not None:
            j = i
            vals: list[str] = []
            while j < len(lines) and (vv := as_value(lines[j])) is not None:
                vals.append(vv)
                j += 1
            if len(vals) >= min_run:
                out.append("Значения на фрагменте: " + ", ".join(vals))
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def clean_vlm_text(text: str) -> str:
    """Anti-loop + дедуп строк + обрезка галлюцинаций вида 1,2,3…N."""
    text = collapse_repetition(text)
    text = collapse_numeric_list(text)
    text = collapse_numbered_hallucination(text)
    text = dedupe_lines(text)
    text = collapse_inline_repetition(text)
    text = join_value_line_runs(text)
    # голый возрастающий список чисел (PASS-A иногда печатает 1..500)
    m = re.search(
        r"((?:\b\d{1,4}\b(?:\s*,\s*|\s+)){25,}\b\d{1,4}\b)",
        text,
    )
    if m:
        text = (
            text[: m.start()]
            + "[truncated-number-list]\n"
            + text[m.end() :]
        )
    return text.strip()


def split_pages_md(text: str) -> dict[int, str]:
    parts = re.split(r"(?m)^## Страница\s+(\d+)\s*$", text)
    out: dict[int, str] = {}
    if len(parts) < 3:
        out[1] = text.strip()
        return out
    # parts: preamble, num, body, num, body...
    i = 1
    while i + 1 < len(parts):
        out[int(parts[i])] = parts[i + 1].strip()
        i += 2
    return out


def assemble(pages: dict[int, str]) -> str:
    return (
        "\n\n".join(f"## Страница {n}\n\n{pages[n].rstrip()}\n" for n in sorted(pages))
        + "\n"
    )


def union_texts(variants: list[str]) -> str:
    """Объединяет N прогонов одной страницы: уникальные строки (первое вхождение),
    борьба с nondeterminism ростом recall.

    Каждый variant уже прошёл clean_vlm_text — повторный collapse_repetition
    на merge опасен (пустые ячейки «| | |» режут хвост с зонами).
    """
    if len(variants) == 1:
        return variants[0]
    seen: set[str] = set()
    out_lines: list[str] = []
    for var in variants:
        for line in var.splitlines():
            norm = re.sub(r"\s+", " ", line.strip().lower())
            if not norm:
                if out_lines and out_lines[-1] != "":
                    out_lines.append("")
                continue
            if norm in seen:
                continue
            seen.add(norm)
            out_lines.append(line.rstrip())
    merged = "\n".join(out_lines)
    return collapse_numbered_hallucination(dedupe_lines(merged))


def run_vlm(
    client,
    spec: ModelSpec,
    pdf: Path,
    page_nums: list[int],
    *,
    page_max: int,
    tile_max: int,
    out_dir: Path,
    fail_fast: bool = False,
    ocr_prompt: str = "markdown",
    image_max_px: int = 1280,
    jpeg_quality: int = 85,
    max_tokens: int = 1200,
    max_tiles: int = 6,
    stamp_crop: bool = False,
    zone_crop: bool = False,
    two_pass: bool = True,
    runs: int = 1,
    retries: int = 3,
    retry_delay: float = 2.0,
    usage: UsageTotals | None = None,
    sheet_aware: bool = False,
    table_pages: set[int] | None = None,
) -> Path:
    usage = usage or UsageTotals()
    doc = fitz.open(pdf)
    pages_out: dict[int, str] = {}
    use_two_pass = two_pass and spec.prompt_style != "deepseek_ocr"
    runs = max(1, runs)

    def _extract_page(num: int) -> str:
        from sheet_aware import (
            build_passport,
            generic_describe_zones,
            is_garbage_tile,
            pass_a_quality_ok,
            passport_markdown,
            prompt_desc_for,
            prompt_tile_for,
            render_budget,
            system_for,
        )

        page = doc[num - 1]
        passport = build_passport(page, num) if sheet_aware else None
        budget = (
            render_budget(passport.kind, page_max) if passport is not None else None
        )

        local_page_max = page_max
        local_tile_max = tile_max
        local_max_tiles = max_tiles
        local_desc_max = min(2000, page_max)
        local_tile_send = image_max_px
        local_pass_a_tokens = max(max_tokens, 4000)
        local_tile_tokens = max_tokens
        if budget is not None:
            local_page_max = budget["page_max"]
            local_tile_max = budget["tile_max"]
            local_max_tiles = max(max_tiles, budget["max_tiles"])
            local_desc_max = budget["desc_max"]
            local_tile_send = budget["tile_send_px"]
            local_pass_a_tokens = max(max_tokens, budget.get("pass_a_tokens", 8192))
            local_tile_tokens = max(max_tokens, budget.get("tile_tokens", 4096))
        elif num in HARD_PAGES and page_max < HIGH_DPI_DEFAULTS["page_max"]:
            local_page_max = max(page_max, 2400)
            local_tile_max = min(tile_max, 750)
            local_max_tiles = max(max_tiles, 8)

        im = render_page(page, local_page_max)
        w, h = im.size
        base_tile_prompt = tile_prompt_for(spec, ocr_prompt, page_num=num)
        prompt = (
            prompt_tile_for(passport.kind, base_tile_prompt)
            if passport is not None
            else base_tile_prompt
        )
        sections: list[str] = []

        if passport is not None:
            sections.append(
                "### PASS-0 Паспорт листа\n\n" + passport_markdown(passport)
            )
            print(
                f"  p{num}: sheet_aware kind={passport.kind} "
                f"({', '.join(passport.reasons)})",
                flush=True,
            )

        # ── Pass A: целое изображение → описание ─────────────────────────
        if use_two_pass:
            desc_max = min(local_desc_max, max(im.size))
            desc_url = image_to_data_url(im, max_px=desc_max, quality=jpeg_quality)
            sys_desc = system_for(passport.kind) if passport is not None else SYSTEM_DESC
            user_desc = (
                prompt_desc_for(passport.kind, passport)
                if passport is not None
                else PROMPT_DESC
            )
            print(
                f"  p{num}: PASS-A describe {w}x{h}→{desc_max}px model={spec.hf_id}",
                flush=True,
            )
            t0 = time.time()
            try:
                desc = call_with_retries(
                    lambda: chat_vision(
                        client,
                        spec.hf_id,
                        sys_desc,
                        user_desc,
                        desc_url,
                        max_tokens=local_pass_a_tokens,
                        prompt_style=spec.prompt_style,
                        usage=usage,
                    ),
                    retries=retries,
                    base_delay=retry_delay,
                    usage=usage,
                )
                desc = clean_vlm_text(desc)
            except Exception as e:
                usage.failed_tiles += 1
                print(f"    PASS-A FAIL {e}", flush=True)
                if fail_fast:
                    doc.close()
                    raise
                desc = f"(ошибка описания: {e})"

            if passport is not None:
                ok, qreasons = pass_a_quality_ok(desc, passport)
                print(
                    f"    PASS-A quality={'OK' if ok else 'FAIL'} "
                    f"{qreasons or []} {len(desc)} chars "
                    f"tok={local_pass_a_tokens} {time.time()-t0:.1f}s",
                    flush=True,
                )
                if not ok:
                    zone_bits: list[str] = []
                    z_im = render_page(page, max(local_page_max, 3600))
                    for zlabel, zcrop, zprompt in generic_describe_zones(
                        z_im, passport.kind
                    ):
                        zurl = image_to_data_url(
                            zcrop, max_px=1800, quality=max(jpeg_quality, 90)
                        )
                        zt0 = time.time()
                        try:
                            ztxt = call_with_retries(
                                lambda url=zurl, zp=zprompt: chat_vision(
                                    client,
                                    spec.hf_id,
                                    sys_desc,
                                    zp
                                    + "\n\n"
                                    + passport.context_pack(max_labels=25),
                                    url,
                                    max_tokens=local_tile_tokens,
                                    prompt_style=spec.prompt_style,
                                    usage=usage,
                                ),
                                retries=retries,
                                base_delay=retry_delay,
                                usage=usage,
                            )
                            ztxt = clean_vlm_text(ztxt)
                        except Exception as e:
                            usage.failed_tiles += 1
                            ztxt = f"(ошибка зоны: {e})"
                        zone_bits.append(f"#### {zlabel}\n\n{ztxt}")
                        print(
                            f"    PASS-A retry {zlabel}: {len(ztxt)} chars "
                            f"{time.time()-zt0:.1f}s",
                            flush=True,
                        )
                    desc = (
                        desc
                        + "\n\n### Уточнение по зонам (после quality-gate)\n\n"
                        + "\n\n".join(zone_bits)
                    )
            else:
                print(f"    PASS-A: {len(desc)} chars {time.time()-t0:.1f}s", flush=True)

            sections.append(f"### PASS-A Описание листа\n\n{desc}")

        # ── PASS-T: лист-таблица → «перенеси как в исходнике» вместо тайлов ─
        if table_pages and num in table_pages:
            t_im = render_page(page, max(local_page_max, 3200))
            print(
                f"  p{num}: PASS-T таблица целиком {t_im.size[0]}x{t_im.size[1]}",
                flush=True,
            )
            def _table_rows_with_digits(txt: str) -> int:
                return sum(
                    1
                    for ln in txt.splitlines()
                    if ln.lstrip().startswith("|") and re.search(r"\d", ln)
                )

            # VLM недетерминированна: тот же лист то даёт идеальную таблицу,
            # то петлю пустых ячеек → до 3 попыток (чуть разный max_px),
            # держим лучший вариант по числу строк с цифрами.
            t0 = time.time()
            ttxt, best_rows = "", -1
            for attempt, px in enumerate((2600, 2200, 3000), start=1):
                try:
                    cand = call_with_retries(
                        lambda px=px: chat_vision(
                            client,
                            spec.hf_id,
                            SYSTEM_TABLE_EXACT,
                            PROMPT_TABLE_EXACT,
                            image_to_data_url(t_im, max_px=px, quality=90),
                            max_tokens=max(6000, local_pass_a_tokens),
                            prompt_style=spec.prompt_style,
                            usage=usage,
                        ),
                        retries=retries,
                        base_delay=retry_delay,
                        usage=usage,
                    )
                    cand = dedupe_lines(cand)  # мягкая чистка: full clean режет GFM
                    rows_ok = _table_rows_with_digits(cand)
                    print(
                        f"    PASS-T attempt {attempt} (px={px}): "
                        f"{len(cand)} chars, {rows_ok} строк с цифрами",
                        flush=True,
                    )
                    if rows_ok > best_rows:
                        ttxt, best_rows = cand, rows_ok
                    if rows_ok >= 5:
                        break
                except Exception as e:
                    usage.failed_tiles += 1
                    print(f"    PASS-T attempt {attempt} FAIL {e}", flush=True)
                    if fail_fast:
                        doc.close()
                        raise
            if not ttxt:
                ttxt = "(ошибка таблицы: все попытки PASS-T не удались)"
            print(f"    PASS-T: {len(ttxt)} chars {time.time()-t0:.1f}s", flush=True)
            sections.append("### PASS-B Таблица целиком\n\n" + ttxt)
            content = "\n\n".join(sections)
            if stamp_crop:
                try:
                    from local_ocr import append_stamp_missing, ocr_stamp

                    stamp_raw, codes = ocr_stamp(im)
                    content = append_stamp_missing(content, stamp_raw, codes)
                except Exception as e:
                    print(f"    stamp OCR skip: {e}", flush=True)
            return content

        # ── Pass B: тайлы → markdown-текст/таблицы ────────────────────────
        rows, cols = tile_grid(w, h, local_tile_max, max_tiles=local_max_tiles)
        tw, th = w / cols, h / rows
        print(
            f"  p{num}: PASS-B {w}x{h} tiles {rows}x{cols} "
            f"stamp_crop={stamp_crop} zone_crop={zone_crop} "
            f"two_pass={use_two_pass} sheet_aware={sheet_aware}",
            flush=True,
        )
        jobs: list[tuple[str, Image.Image, str, int | None]] = []
        for r in range(rows):
            for c in range(cols):
                x0, y0 = int(c * tw), int(r * th)
                x1, y1 = min(w, int((c + 1) * tw)), min(h, int((r + 1) * th))
                jobs.append((f"r{r+1}c{c+1}", im.crop((x0, y0, x1, y1)), prompt, None))

        if (
            sheet_aware
            and passport is not None
            and passport.kind in ("plan", "scheme", "mixed", "table")
            and spec.prompt_style != "deepseek_ocr"
        ):
            try:
                zone_page_max = max(local_page_max, 3600)
                zone_im = (
                    render_page(page, zone_page_max)
                    if zone_page_max > local_page_max
                    else im
                )
                for label, crop, zprompt in generic_describe_zones(
                    zone_im, passport.kind
                ):
                    jobs.append((label, crop, zprompt, 1800))
                print(
                    f"    +generic zones kind={passport.kind} "
                    f"(page_max={zone_page_max})",
                    flush=True,
                )
            except Exception as e:
                print(f"    generic-zone skip: {e}", flush=True)

        if zone_crop and spec.prompt_style != "deepseek_ocr":
            try:
                # отдельный hi-res рендер: мелкий текст таблиц/штампа иначе «плывёт»
                zone_page_max = max(local_page_max, 5600)
                zone_im = (
                    render_page(page, zone_page_max)
                    if zone_page_max > local_page_max
                    else im
                )
                zjobs = crop_content_zones(zone_im, num)
                for label, crop, zprompt in zjobs:
                    jobs.append((label, crop, zprompt, 2000))
                print(
                    f"    +{len(zjobs)} zone crops "
                    f"(page_max={zone_page_max}, send_px=2000)",
                    flush=True,
                )
            except Exception as e:
                print(f"    zone-crop skip: {e}", flush=True)

        if stamp_crop and spec.prompt_style != "deepseek_ocr":
            try:
                from local_ocr import crop_stamp_regions

                for name, crop in crop_stamp_regions(im).items():
                    jobs.append((f"stamp_{name}", crop, PROMPT_STAMP, 1600))
            except Exception as e:
                print(f"    stamp-crop skip regions: {e}", flush=True)

        tile_parts: list[str] = []
        dropped_garbage = 0
        for i, (label, crop, tile_prompt, px_override) in enumerate(jobs, start=1):
            send_px = (
                px_override
                if px_override is not None
                else (local_tile_send if sheet_aware else image_max_px)
            )
            send_q = max(jpeg_quality, 92) if label.startswith("zone_") else jpeg_quality
            url = image_to_data_url(crop, max_px=send_px, quality=send_q)
            t0 = time.time()
            try:
                txt = call_with_retries(
                    lambda url=url, tile_prompt=tile_prompt: chat_vision(
                        client,
                        spec.hf_id,
                        SYSTEM_VLM,
                        tile_prompt,
                        url,
                        max_tokens=local_tile_tokens,
                        prompt_style=spec.prompt_style,
                        usage=usage,
                    ),
                    retries=retries,
                    base_delay=retry_delay,
                    usage=usage,
                )
                txt = clean_vlm_text(txt)
            except Exception as e:
                usage.failed_tiles += 1
                print(f"    tile {i}/{len(jobs)} ({label}) FAIL {e}", flush=True)
                if fail_fast:
                    doc.close()
                    raise RuntimeError(
                        f"VLM fail-fast на p{num} tile {label}. "
                        f"Проверь provider (live для {spec.hf_id}). "
                        f"Пример: --provider {spec.preferred_provider}"
                    ) from e
                txt = f"(ошибка тайла: {e})"
            if sheet_aware and is_garbage_tile(txt):
                dropped_garbage += 1
                tile_parts.append(
                    f"--- {label} ---\n"
                    f"(отброшено: мусорный тайл — голые номера/оси 1..N)"
                )
                print(
                    f"    tile {i}/{len(jobs)} ({label}): DROPPED garbage "
                    f"{time.time()-t0:.1f}s",
                    flush=True,
                )
                continue
            tile_parts.append(f"--- {label} ---\n{txt}")
            print(
                f"    tile {i}/{len(jobs)} ({label}): {len(txt)} chars "
                f"{time.time()-t0:.1f}s",
                flush=True,
            )

        if dropped_garbage:
            print(f"    dropped_garbage_tiles={dropped_garbage}", flush=True)
        sections.append("### PASS-B Тайлы / текст\n\n" + "\n\n".join(tile_parts))
        content = "\n\n".join(sections)

        if stamp_crop:
            try:
                from local_ocr import append_stamp_missing, ocr_stamp

                stamp_raw, codes = ocr_stamp(im)
                content = append_stamp_missing(content, stamp_raw, codes)
                print(f"    stamp OCR: +{len(codes)} codes", flush=True)
            except Exception as e:
                print(f"    stamp OCR skip: {e}", flush=True)

        return content

    for num in page_nums:
        variants: list[str] = []
        for ri in range(runs):
            if runs > 1:
                print(f"  p{num}: RUN {ri + 1}/{runs}", flush=True)
            variants.append(_extract_page(num))
        content = union_texts(variants)
        (out_dir / "pages").mkdir(exist_ok=True)
        pages_dir = out_dir / "pages"
        pages_dir.mkdir(exist_ok=True)
        if runs > 1:
            runs_dir = out_dir / "runs" / f"page_{num:04d}"
            runs_dir.mkdir(parents=True, exist_ok=True)
            for ri, var in enumerate(variants, start=1):
                (runs_dir / f"run_{ri:02d}.md").write_text(var, encoding="utf-8")
        (pages_dir / f"page_{num:04d}.md").write_text(content, encoding="utf-8")
        pages_out[num] = content
        print(f"  p{num}: DONE {len(content)} chars (runs={runs})", flush=True)

    doc.close()
    out_path = out_dir / "out.md"
    out_path.write_text(assemble(pages_out), encoding="utf-8")
    return out_path


def run_union_ocr(
    draft_path: Path,
    ocr_dir: Path,
    out_path: Path,
    pages: str,
) -> Path:
    """Детерминированный union через merge_vlm_ocr.py."""
    import subprocess

    cmd = [
        sys.executable,
        str(ROOT / "merge_vlm_ocr.py"),
        "--draft",
        str(draft_path),
        "--ocr-dir",
        str(ocr_dir),
        "-o",
        str(out_path),
        "--pages",
        pages,
    ]
    print("\n[pipeline] union OCR:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=False)
    return out_path


def run_synth(
    client,
    spec: ModelSpec,
    draft_path: Path,
    page_nums: list[int],
    out_dir: Path,
    *,
    ocr_dir: Path | None = None,
    strict: bool = True,
    usage: UsageTotals | None = None,
    retries: int = 3,
    retry_delay: float = 2.0,
) -> Path:
    raw = draft_path.read_text(encoding="utf-8")
    drafts = split_pages_md(raw)
    stamp_dir = ROOT / "bench_3pages_stamp_ocr.md.pages"
    ocr_pages = ocr_dir if ocr_dir and ocr_dir.exists() else None
    pages_out: dict[int, str] = {}
    system = SYSTEM_SYNTH_STRICT if strict else SYSTEM_SYNTH
    usage = usage or UsageTotals()
    for num in page_nums:
        draft = drafts.get(num) or drafts.get(1, "")
        ocr_parts: list[str] = []
        if ocr_pages:
            op = ocr_pages / f"page_{num:04d}.md"
            if op.exists():
                ocr_parts.append(op.read_text(encoding="utf-8").strip())
        sp = stamp_dir / f"page_{num:04d}.md"
        if sp.exists():
            st = sp.read_text(encoding="utf-8")
            parts = re.split(r"\n### Штамп \(OCR\)\n", st, maxsplit=1)
            if len(parts) > 1:
                ocr_parts.append(parts[1].strip())
        ocr = "\n\n".join(p for p in ocr_parts if p)
        user = (
            f"=== ЧЕРНОВИК (сохранить целиком) ===\n{draft[:10000]}\n"
            f"=== /ЧЕРНОВИК ===\n\n"
            f"=== OCR (добавить только отсутствующее) ===\n"
            f"{(ocr or '(нет)')[:8000]}\n=== /OCR ===\n\n"
            "Итог = черновик + недостающие факты из OCR:"
        )
        t0 = time.time()
        try:
            fused = call_with_retries(
                lambda: chat_text(
                    client, spec.hf_id, system, user, max_tokens=3500, usage=usage
                ),
                retries=retries,
                base_delay=retry_delay,
                usage=usage,
            )
            # защита от сжатия: если сильно короче draft — оставить draft+ocr append
            if len(fused) < max(120, len(draft) * 0.55):
                print(
                    f"  p{num}: synth too short ({len(fused)}<{len(draft)*0.55:.0f}) — keep draft",
                    flush=True,
                )
                fused = draft
        except Exception as e:
            fused = draft
            print(f"  p{num}: synth FAIL {e}", flush=True)
        (out_dir / "pages").mkdir(exist_ok=True)
        (out_dir / "pages" / f"page_{num:04d}.md").write_text(fused, encoding="utf-8")
        pages_out[num] = fused
        print(f"  p{num}: synth {len(fused)} chars {time.time()-t0:.1f}s", flush=True)
    out_path = out_dir / "out.md"
    out_path.write_text(assemble(pages_out), encoding="utf-8")
    return out_path


def compare(out_path: Path, pages: str) -> None:
    import subprocess

    cmd = [sys.executable, str(ROOT / "compare_to_etalon.py"), str(out_path), "--pages", pages]
    print("\n" + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=False)


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="HF API bench for server-fit models")
    ap.add_argument("--list", action="store_true", help="Показать каталог кандидатов")
    ap.add_argument("--role", choices=["vlm", "synth"], default="vlm")
    ap.add_argument(
        "--pipeline",
        action="store_true",
        help="One-shot: VLM → union OCR → compare (игнорирует --role).",
    )
    ap.add_argument("--model", default=None, help="id из каталога или полный HF id")
    ap.add_argument("--pages", default=DEFAULT_PAGES)
    ap.add_argument(
        "--page-max",
        type=int,
        default=None,
        help="Макс. сторона страницы (px). По умолчанию зависит от модели.",
    )
    ap.add_argument(
        "--tile-max",
        type=int,
        default=None,
        help="Макс. сторона тайла (px). По умолчанию зависит от модели.",
    )
    ap.add_argument(
        "--ocr-prompt",
        choices=sorted(DEEPSEEK_OCR_PROMPTS),
        default="ocr",
        help="Preset для deepseek-ocr (markdown|ocr|free|figure). По умолчанию ocr.",
    )
    ap.add_argument(
        "--max-tiles",
        type=int,
        default=None,
        help="Лимит тайлов на страницу (1 = целый лист). По умолчанию 6 или 8 при --high-dpi.",
    )
    ap.add_argument(
        "--high-dpi",
        action="store_true",
        help="Высокий DPI/тайлы (page_max≈2800) — для hard pages ОДИ/КР3.",
    )
    ap.add_argument(
        "--stamp-crop",
        action="store_true",
        help="Доп. VLM-кропы углов штампа + локальный Tesseract stamp OCR.",
    )
    ap.add_argument(
        "--table-pages",
        default="",
        help="страницы-таблицы (формат как --pages): вместо тайлов — один вызов "
        "«перенеси таблицу как в исходнике» (PASS-T) по целому листу",
    )
    ap.add_argument(
        "--zone-crop",
        action="store_true",
        help="Зональные кропы экспликации/легенды/таблиц (ОДИ/КР).",
    )
    ap.add_argument(
        "--two-pass",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pass-A описание всего листа + Pass-B тайлы (по умолчанию ON). "
        "--no-two-pass отключает.",
    )
    ap.add_argument("--draft", type=Path, default=None, help="MD черновик для --role synth")
    ap.add_argument(
        "--ocr-dir",
        type=Path,
        default=None,
        help="Папка *.pages с page_XXXX.md OCR (synth / --pipeline).",
    )
    ap.add_argument(
        "--provider",
        default=None,
        help="Inference Provider (featherless-ai|novita|deepinfra). Не используй auto.",
    )
    ap.add_argument("--pdf", type=Path, default=None)
    ap.add_argument("--no-compare", action="store_true")
    ap.add_argument(
        "--fail-fast",
        action="store_true",
        help="Остановиться на первой ошибке тайла (по умолчанию keep-going + retry).",
    )
    ap.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Повторы на ошибку API/тайла (по умолчанию 3).",
    )
    ap.add_argument(
        "--retry-delay",
        type=float,
        default=2.0,
        help="Базовая задержка retry (сек), растёт ×2.",
    )
    ap.add_argument(
        "--loose-synth",
        action="store_true",
        help="Мягкий synth-промпт (по умолчанию strict).",
    )
    ap.add_argument(
        "--runs",
        type=int,
        default=1,
        help="N прогонов Pass-B на страницу с union уникальных фактов "
        "(борьба с nondeterminism, ↑recall). По умолчанию 1.",
    )
    ap.add_argument(
        "--synth",
        default=None,
        help="id текстовой модели для synthesis-сборки после VLM "
        "(например qwen3-32b-text). Сохраняет черновик, добирает факты.",
    )
    ap.add_argument(
        "--no-zone-hints",
        action="store_true",
        help="Отключить тест-сетовые подсказки по номеру страницы "
        "(обязательно для произвольных PDF, не из тест-сета).",
    )
    ap.add_argument(
        "--sheet-aware",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Классификация листа + адаптивные промпты/dpi + quality-gate Pass-A. "
        "Для сравнения: --sheet-aware vs без флага.",
    )
    args = ap.parse_args()

    if args.no_zone_hints:
        global USE_ZONE_HINTS
        USE_ZONE_HINTS = False

    if args.list:
        print(f"{'id':18} {'role':5} {'fit':5} {'provider':14} hf_id")
        for m in CATALOG:
            print(
                f"{m.id:18} {m.role:5} {m.fit:5} {m.preferred_provider:14} "
                f"{m.hf_id}  # {m.notes}"
            )
        print(
            "\nВажно: HF_PROVIDER=auto часто даёт model_not_supported. "
            "Раннер сам подставит preferred_provider."
        )
        print(
            "Флаги качества: --high-dpi --stamp-crop --zone-crop "
            "--two-pass/--no-two-pass --pipeline. "
            "По умолчанию: two-pass ON, keep-going + retries."
        )
        return 0

    if not args.model:
        print("Укажи --model (или --list)", file=sys.stderr)
        return 1

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    if not token or token.startswith("hf_xxx"):
        print(
            "Нет HF_TOKEN. Скопируй .env.example → .env и вставь токен с "
            "https://huggingface.co/settings/tokens",
            file=sys.stderr,
        )
        return 1

    role = "vlm" if args.pipeline else args.role
    spec = get_spec(args.model)
    if role != spec.role and not args.pipeline:
        print(
            f"Внимание: каталог помечает {spec.id} как role={spec.role}, "
            f"ты запустил --role {args.role}",
            flush=True,
        )

    provider = resolve_provider(spec, args.provider, token)
    client = make_client(token, provider)
    defaults = vision_defaults(spec)
    if args.high_dpi:
        defaults = {**defaults, **HIGH_DPI_DEFAULTS}
    page_max = args.page_max if args.page_max is not None else defaults["page_max"]
    tile_max = args.tile_max if args.tile_max is not None else defaults["tile_max"]
    max_tiles = (
        args.max_tiles
        if args.max_tiles is not None
        else int(defaults.get("max_tiles", 6))
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if args.pipeline:
        tag = "pipeline"
    elif role == "vlm" and args.sheet_aware:
        tag = "sheetaware"
    elif role == "vlm" and args.two_pass:
        tag = "twopass"
    else:
        tag = role
    out_dir = ROOT / "hf_runs" / f"{stamp}_{spec.id}_{tag}"
    out_dir.mkdir(parents=True, exist_ok=True)

    usage = UsageTotals()
    ocr_dir = args.ocr_dir or (DEFAULT_OCR_DIR if args.pipeline else None)

    meta = {
        "spec": asdict(spec),
        "role": role,
        "pipeline": bool(args.pipeline),
        "two_pass": bool(args.two_pass),
        "sheet_aware": bool(args.sheet_aware),
        "runs": args.runs,
        "synth": args.synth,
        "provider": provider,
        "pages": args.pages,
        "page_max": page_max,
        "tile_max": tile_max,
        "max_tiles": max_tiles,
        "high_dpi": bool(args.high_dpi),
        "stamp_crop": bool(args.stamp_crop),
        "zone_crop": bool(args.zone_crop),
        "ocr_prompt": args.ocr_prompt,
        "image_max_px": defaults["image_max_px"],
        "fail_fast": bool(args.fail_fast),
        "retries": args.retries,
        "ocr_dir": str(ocr_dir) if ocr_dir else None,
        "started_utc": stamp,
        "usage": usage.as_dict(),
    }
    print(f"HF -> {spec.hf_id} role={role} provider={provider}", flush=True)
    print(
        f"render page_max={page_max} tile_max={tile_max} max_tiles={max_tiles} "
        f"img_px={defaults['image_max_px']} high_dpi={args.high_dpi} "
        f"stamp_crop={args.stamp_crop} zone_crop={args.zone_crop} "
        f"two_pass={args.two_pass} sheet_aware={args.sheet_aware} "
        f"fail_fast={args.fail_fast} retries={args.retries}",
        flush=True,
    )
    print(f"out -> {out_dir}", flush=True)

    t0 = time.time()
    union_path: Path | None = None
    synth_path: Path | None = None
    try:
        if role == "vlm" or args.pipeline:
            pdf = args.pdf or find_etalon_pdf()
            doc = fitz.open(pdf)
            doc_page_count = doc.page_count
            page_nums = parse_pages(args.pages, doc_page_count)
            doc.close()
            out_path = run_vlm(
                client,
                spec,
                pdf,
                page_nums,
                page_max=page_max,
                tile_max=tile_max,
                out_dir=out_dir,
                fail_fast=args.fail_fast,
                ocr_prompt=args.ocr_prompt,
                image_max_px=defaults["image_max_px"],
                jpeg_quality=defaults["jpeg_quality"],
                max_tokens=defaults["max_tokens"],
                max_tiles=max_tiles,
                stamp_crop=args.stamp_crop,
                zone_crop=args.zone_crop,
                two_pass=args.two_pass,
                runs=args.runs,
                retries=args.retries,
                retry_delay=args.retry_delay,
                usage=usage,
                table_pages=(
                    set(parse_pages(args.table_pages, doc_page_count))
                    if args.table_pages
                    else None
                ),
                sheet_aware=args.sheet_aware,
            )
            if args.pipeline:
                if not ocr_dir or not Path(ocr_dir).exists():
                    print(
                        f"WARNING: OCR dir нет ({ocr_dir}), pipeline без union",
                        flush=True,
                    )
                else:
                    union_path = out_dir / "out_union.md"
                    run_union_ocr(out_path, Path(ocr_dir), union_path, args.pages)
        else:
            draft = args.draft or (ROOT / "bench_3pages_qwen_instruct.md")
            if not draft.exists():
                print(f"Draft не найден: {draft}", file=sys.stderr)
                return 1
            page_nums = parse_pages(args.pages, 6)
            out_path = run_synth(
                client,
                spec,
                draft,
                page_nums,
                out_dir,
                ocr_dir=args.ocr_dir,
                strict=not args.loose_synth,
                usage=usage,
                retries=args.retries,
                retry_delay=args.retry_delay,
            )
    except Exception as e:
        meta["error"] = str(e)
        meta["elapsed_sec"] = round(time.time() - t0, 1)
        meta["usage"] = usage.as_dict()
        (out_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nFAILED: {e}", file=sys.stderr)
        print(f"usage so far: {usage.as_dict()}", flush=True)
        return 2

    if args.synth and (role == "vlm" or args.pipeline):
        try:
            synth_spec = get_spec(args.synth)
            synth_provider = resolve_provider(synth_spec, None, token)
            synth_client = make_client(token, synth_provider)
            synth_dir = out_dir / "synth"
            synth_dir.mkdir(exist_ok=True)
            draft_for_synth = (
                union_path if (union_path and union_path.exists()) else out_path
            )
            print(
                f"\n[pipeline] synth via {synth_spec.hf_id} "
                f"draft={draft_for_synth.name}",
                flush=True,
            )
            synth_path = run_synth(
                synth_client,
                synth_spec,
                draft_for_synth,
                page_nums,
                synth_dir,
                ocr_dir=Path(ocr_dir) if ocr_dir else None,
                strict=not args.loose_synth,
                usage=usage,
                retries=args.retries,
                retry_delay=args.retry_delay,
            )
        except Exception as e:
            print(f"[pipeline] synth skip: {e}", flush=True)
            synth_path = None

    meta["elapsed_sec"] = round(time.time() - t0, 1)
    meta["output"] = str(out_path)
    if union_path and union_path.exists():
        meta["output_union"] = str(union_path)
    if synth_path and synth_path.exists():
        meta["output_synth"] = str(synth_path)
    meta["usage"] = usage.as_dict()
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nDone: {out_path} ({meta['elapsed_sec']}s)", flush=True)
    print(f"usage: {usage.as_dict()}", flush=True)

    if not args.no_compare:
        compare(out_path, args.pages)
        if union_path and union_path.exists():
            print("\n[pipeline] compare union:", flush=True)
            compare(union_path, args.pages)
        if synth_path and synth_path.exists():
            print("\n[pipeline] compare synth:", flush=True)
            compare(synth_path, args.pages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
