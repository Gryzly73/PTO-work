#!/usr/bin/env python3
"""PDF to Markdown converter for construction/architectural documents.

Accuracy features:
- Auto-tiling: large drawings are split into overlapping crops so Gemini Vision
  doesn't downscale them past the ~3072px input limit and lose fine label text.
- Verification pass: a second Gemini call re-reads the page image and rewrites
  the markdown with any missed numbers/labels/table rows.
- Truncation detection: if finish_reason=MAX_TOKENS, retry with a larger budget.
- DPI: 400 for drawings (was 300), 200 for text-rich pages.
- Drawing-aware prompt: explicitly enumerates axes, level marks, element marks,
  stamps, dimension chains.
"""

import argparse
import base64
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import fitz  # PyMuPDF

# Sibling import: reuse OCR + number-extraction primitives from audit_ocr.py
# (same dir). Putting our dir on sys.path makes this work when the script is
# invoked from any cwd. If audit_ocr or its deps (pytesseract isn't required —
# audit_ocr shells out to the tesseract binary) are missing, OCR check
# silently degrades to disabled.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from audit_ocr import (
        extract_numbers as _ocr_extract_numbers,
        normalize as _ocr_normalize,
        ocr_page as _ocr_page_to_text,
    )
    _OCR_AVAILABLE = True
except ImportError as _e:
    _OCR_AVAILABLE = False
    _OCR_IMPORT_ERR = str(_e)

try:
    from ocr_contributor import (recover_codes as _ocr_recover_codes,
                                  make_block as _ocr_make_block,
                                  synthesize_page as _ocr_synthesize_page)
    _OCR_CONTRIB_AVAILABLE = True
except ImportError:
    _OCR_CONTRIB_AVAILABLE = False

# Sibling import: <TABLE> JSON-block renderer + Defense-2.5 issue detector.
# When the model emits tables in the new JSON-in-marker format (principle 7),
# render_tables.render_tables() converts them to real GFM tables in
# post-processing, and detect_table_issues() lets process_page() trigger a
# Pro fallback inline when the primary provider produced unparseable JSON.
try:
    from render_tables import (
        render_tables as _render_tables_text,
        detect_table_issues as _detect_table_issues,
        TABLE_RE as _TABLE_RE,
    )
    _RENDER_TABLES_AVAILABLE = True
except ImportError as _re:
    _RENDER_TABLES_AVAILABLE = False
    _RENDER_TABLES_IMPORT_ERR = str(_re)


# Auto-load .env from the script's parent dir (same convention as test_multiprovider.py).
# Must run before any os.getenv() reads below.
def _load_dotenv():
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# Provider registry: maps the --provider CLI value to (backend, model_id).
# backend ∈ {"gemini", "openai-compat"}. The OpenAI-compat backend covers
# Z.AI / OpenRouter / DashScope via the openai SDK.
PROVIDERS = {
    "gemini-pro":   {"backend": "gemini",        "model": "gemini-2.5-pro",   "env": "GEMINI_API_KEY"},
    "gemini-flash": {"backend": "gemini",        "model": "gemini-2.5-flash", "env": "GEMINI_API_KEY"},
    "glm":          {"backend": "openai-compat", "model": "glm-4.6v",
                      "env": "ZHIPUAI_API_KEY", "base_url_env": "ZHIPUAI_BASE_URL",
                      "base_url_default": "https://api.z.ai/api/paas/v4",
                      # Without thinking-disabled, GLM-4.6V/4.5V spend the entire
                      # output budget on internal reasoning and return empty content.
                      "extra_body": {"thinking": {"type": "disabled"}},
                      # z.ai rejects max_tokens > 32768 with 1210 "Invalid API parameter".
                      "max_tokens_cap": 32768},
    "qwen":         {"backend": "openai-compat", "model": "qwen/qwen3-vl-235b-a22b-instruct",
                      "env": "OPENROUTER_API_KEY", "base_url_env": "OPENROUTER_BASE_URL",
                      "base_url_default": "https://openrouter.ai/api/v1"},
    "qwen3-vl-8b":  {"backend": "openai-compat", "model": "Qwen/Qwen3-VL-8B-Instruct:novita",
                      "env": "HF_TOKEN", "base_url_env": "HF_ROUTER_BASE_URL",
                      "base_url_default": "https://router.huggingface.co/v1",
                      "max_tokens_cap": 32768},
    "qwen3-vl-30b": {"backend": "openai-compat", "model": "Qwen/Qwen3-VL-30B-A3B-Instruct:novita",
                      "env": "HF_TOKEN", "base_url_env": "HF_ROUTER_BASE_URL",
                      "base_url_default": "https://router.huggingface.co/v1",
                      "max_tokens_cap": 32768},
    # Qwen3-VL-32B-Instruct — DENSE (not MoE) vision model, 131K ctx, OCR in 32
    # languages. Bigger/stronger sibling of qwen3-vl-8b; one model does both tiles
    # and merge. Via OpenRouter. Candidate for local MLX deploy on a 64GB box.
    "qwen3-vl-32b": {"backend": "openai-compat", "model": "qwen/qwen3-vl-32b-instruct",
                      "env": "OPENROUTER_API_KEY", "base_url_env": "OPENROUTER_BASE_URL",
                      "base_url_default": "https://openrouter.ai/api/v1",
                      "max_tokens_cap": 32768},
    # Qwen3-32B — DENSE TEXT model (no vision), 131K ctx, 100+ languages, strong
    # instruction-following. Intended as a --merge-provider: stitch tile texts the
    # vision model already produced (a language task, not a vision one). "vision":
    # False → the merge step passes no image to it. reasoning disabled so thinking
    # tokens don't eat the output budget. Local-deployable on a 64GB box. See ADR-037.
    # Gemma 4 — Google's open VLM, SAME lineage as the Gemini ground truth (worth
    # testing fairly: the prior Gemma-3-27b run was crippled by an 8192-tok provider
    # cap that chopped its tables). Produces clean Cyrillic (no latinization). Two
    # variants: 26b-a4b = MoE (4B active → local-deployable, fast), 31b = dense.
    # Via OpenRouter paid tier (cheap) to avoid the free-tier 429s. See ADR-044.
    "gemma4-26b": {"backend": "openai-compat", "model": "google/gemma-4-26b-a4b-it",
                      "env": "OPENROUTER_API_KEY", "base_url_env": "OPENROUTER_BASE_URL",
                      "base_url_default": "https://openrouter.ai/api/v1",
                      "max_tokens_cap": 8192},
    "gemma4-31b": {"backend": "openai-compat", "model": "google/gemma-4-31b-it",
                      "env": "OPENROUTER_API_KEY", "base_url_env": "OPENROUTER_BASE_URL",
                      "base_url_default": "https://openrouter.ai/api/v1",
                      "max_tokens_cap": 8192},
    "qwen3-32b-text": {"backend": "openai-compat", "model": "qwen/qwen3-32b",
                      "env": "OPENROUTER_API_KEY", "base_url_env": "OPENROUTER_BASE_URL",
                      "base_url_default": "https://openrouter.ai/api/v1",
                      "vision": False,
                      "extra_body": {"reasoning": {"enabled": False}},
                      "max_tokens_cap": 32768},
    # Claude Sonnet 4.6 — strong instruction-following + adaptive thinking. As a
    # --merge-provider it stitches the tile texts the vision model produced ("just
    # assemble all the text, invent nothing"). "vision": False → text-only merge
    # (no image, no re-reading pixels → no latinization risk). thinking adaptive +
    # effort high = the "thinking high" the user asked for. Needs ANTHROPIC_API_KEY.
    # NOT local-deployable (cloud) — use for top-quality stitching, not local goal.
    "claude-sonnet": {"backend": "anthropic", "model": "claude-sonnet-4-6",
                      "env": "ANTHROPIC_API_KEY",
                      "vision": False,
                      "thinking": {"type": "adaptive"}, "effort": "high",
                      "max_tokens_cap": 32768},
    # claude-cli — headless `claude -p`, reuses the local Claude Code login
    # (subscription); NO API key. As a --merge-provider it stitches tile texts
    # via Sonnet with NO image (vision=False) and tools forbidden — pure text
    # assembly, invent nothing. The right choice when the user won't add an
    # ANTHROPIC_API_KEY. See ADR-038.
    "claude-cli": {"backend": "claude-cli", "model": "sonnet",
                      "vision": False,
                      "max_tokens_cap": 32768},
}

DPI_TEXT = 200
DPI_DRAWING = 500
TEXT_RICH_THRESHOLD = 2500
# Any page larger than ~A4 is treated as a drawing regardless of text density:
# big formats (A3+) almost always carry drawings with many label-text characters
# that would otherwise trip TEXT_RICH_THRESHOLD and downgrade DPI.
LARGE_FORMAT_PT = 900  # A4 is 595x842pt; anything with a side > 900pt is A3+

# Gemini Vision downscales inputs to ~3072px on the longest side. Tile to stay
# under that limit so Gemini reads tiles at native resolution (no downscaling).
TILE_MAX_PIXEL = 2800
TILE_OVERLAP_PCT = 0.18

# Hard cap on any single Gemini call — protects against silent network hangs.
# (Observed: pages stalled for hours with 0% CPU before this was added.)
GEMINI_CALL_TIMEOUT = 240

# How many tile calls per page to send in parallel. Tile calls are independent,
# so concurrency = N gives roughly N× speedup on drawing pages (bounded by
# Gemini's rate limits — Pro tier ≈ 5 RPS).
TILE_CONCURRENCY = 6

# DPI used for the low-res full-page image passed to the merge step. Sized to fit
# under Gemini's ~3072px input limit on A3 long side — provides macro structure
# (block boundaries, table extents) without competing with per-tile fine detail.
MERGE_FULL_PAGE_DPI = 220
# Hard cap on the longest side (px) of that full-page merge image. A fixed 220 DPI
# on an A0/A1 foldout produces a 5000-7000px PNG whose base64 alone overruns the
# provider request-body limit (observed: HTTP 413 on Novita merge). _capped_merge_dpi
# lowers DPI on big sheets so the merge image always stays under this. See ADR-035.
# (Was 3000; raised to match REQUEST_MAX_PIXEL — Novita passed 4502px, 413'd at 5263px.)
MERGE_MAX_PIXEL = 4096
# Universal hard cap on the longest side (px) of ANY single image in a request,
# enforced in the provider call() right before the API call (defense-in-depth on top
# of the per-step DPI caps). Novita returns HTTP 413 "request entity too large" on
# images above ~5000px; capping every outgoing image here means a big sheet can't 413
# the request even if an upstream DPI cap is wrong. On 413 anyway (byte-size, not just
# pixels) the call halves the image REQUEST_SHRINK_RETRIES times before giving up. See ADR-041.
REQUEST_MAX_PIXEL = 4096
REQUEST_MIN_PIXEL = 768        # never shrink a 413-retry image below this (stays legible)
REQUEST_SHRINK_RETRIES = 2     # extra halving retries on HTTP 413
# Char budget for the concatenated tile-outputs in ONE merge request. With many
# tiles (A1/A0 → 60-80 tiles × up to MAX_TILE_CHARS) a single merge call's body
# blew past the provider limit (413). Above this budget the merge goes hierarchical:
# tiles are batched, each batch merged text-only, then the partials merged with the
# full-page image. See ADR-035.
MERGE_MAX_INPUT_CHARS = 120000
# Backstop on hierarchical-merge recursion. Each level either fits (done) or
# strictly reduces the block count, so this is rarely approached; it only guards
# against a degenerate roll-up that never converges.
MERGE_MAX_DEPTH = 4

# Per-tile output cap. Observed runaway: Gemini Flash emitting 968k chars from a
# 470-token completion; Qwen hitting 32k token max with 72k chars. Truncate so
# one bad tile can't poison the merge step. See ADR-028.
MAX_TILE_CHARS = 30000
# frequency_penalty for openai-compat providers (qwen/glm). 0.0 = off (gemini default).
# Set ~0.4 (via --freq-penalty) to kill repetition-loop degeneration on dense drawings.
FREQUENCY_PENALTY = 0.0
# When True, deromanize_mixed_script folds Latin homoglyphs to Cyrillic in
# mixed-script tokens (conservative — pure-Latin tokens untouched). Off by default
# (Gemini path unaffected); the qwen wrapper turns it on. See ADR-036.
DEROMANIZE_MIXED = False
# Optional separate model for the tile-merge (stitching) step — see --merge-provider.
# Tiles stay on the main (vision) provider; only the merge calls route here. None =
# merge uses the main provider (current behaviour). MERGE_VISION says whether that
# model can take the full-page image (text-only merge models can't). See ADR-037.
MERGE_CALL_FN = None
MERGE_VISION = True

# --- Tile-level detect-and-rerun (ADR-039) ---------------------------------
# When a SINGLE tile's output shows a failure SIGNATURE (model error, empty
# output, a flood of "(не читается)", or a repetition loop), re-run THAT tile
# escalated — currently a higher-DPI re-render of its clip so small text gets
# more pixels — and keep whichever output scores better, so a re-run can NEVER
# regress a tile. The remediation intelligence is a DETERMINISTIC policy
# (signature → action), not an LLM in the decision loop; the model is invoked
# only as the re-run action. Budget-capped per page (cost guard) and logged.
# Off by default (Gemini path unaffected); the qwen wrapper enables it. Its
# value is robustness on hard real sheets where the small VLM under-reads a
# dense tile — a strict no-op on already-clean pages (no signature → no re-run,
# byte-identical output). See ADR-039.
TILE_RERUN_ENABLED = False
# Which failure signatures are allowed to TRIGGER a re-run. Restricted to the
# UNAMBIGUOUS hard failures: a tile is worthless if the model errored or
# degenerated into a repetition loop, so re-running it can only help. The "soft"
# signatures ('empty', 'illegible_flood') are deliberately EXCLUDED: on a clean
# page they fire on legitimately-empty margin tiles and partially-dense tiles,
# and replacing those fragments demonstrably regresses the merged table (iter 1,
# ADR-039) — tile-local "better" is not page-level better. See ledger iter 2.
TILE_RERUN_SIGNATURES = ("error", "repetition")
TILE_RERUN_BUDGET = 3          # max escalated re-runs per page (cost ceiling)
TILE_ILLEGIBLE_TOKEN = "(не читается)"
TILE_ILLEGIBLE_FLOOD = 4       # >= this many illegible markers → suspect tile…
TILE_ILLEGIBLE_SHARE = 0.5     # …AND markers >= this share of non-blank lines
TILE_ILLEGIBLE_STRONG = 8      # OR this many markers regardless of dilution
TILE_MIN_CHARS = 12            # stripped output shorter than this → "empty"
TILE_REPETITION_SHRINK = 0.6   # sanitizer cuts output below this ratio → loop
RERUN_TILE_MAX_PIXEL = 4000    # re-render ceiling (> TILE_MAX_PIXEL=2800)

# --- Native-resolution DPI cap (ADR-040) -----------------------------------
# A page dominated by a single embedded raster image (a scan/photo/exported-image
# page) gains NOTHING from being rendered above that image's native pixel
# resolution — it only upscales (no new detail) while multiplying the tile count
# and the multi-tile merge's nondeterministic row-loss variance. Measured: a
# 3509px table image rendered at DPI_DRAWING=500 → 24368px → 63 tiles → TABLE
# production score swung 38.9–94.4% across runs (ADR-039 / ledger iter 2). When
# enabled, cap a raster-DOMINATED page's render DPI so the rendered page matches
# the embedded image's native pixels (table 63→~2 tiles, drawing 117→~6). Vector
# pages are UNTOUCHED — higher DPI adds real detail there. Gated strictly on image
# coverage, never on content (generic). Off by default. See ADR-040.
RASTER_NATIVE_DPI_CAP = False
RASTER_COVER_MIN = 0.9   # one image must cover ≥ this share of page area to cap

# Compensation layer 1 — OCR contributor (ADR-042). When True, after each page is
# finalized, Tesseract (full page + bottom-right штамп corner crop) recovers the
# dense alphanumeric CODES the VLM dropped (doc-code/штамп, equipment marks) and
# appends the missing ones. The штамп doc_code is missed by EVERY vision model on
# EVERY page — a fixed-corner OCR job, not a capacity job. Off by default. See
# tools/ocr_contributor.py + real_doc_testset/RESULTS.md.
OCR_CONTRIBUTE = False

# Compensation layer 2 — synthesis (ADR-043). When set (via --synthesize PROVIDER),
# after a DRAWING page is finalized, fuse its draft with tiled-Tesseract text via a
# strong TEXT model: keep the 8b structure, fold in the dense content 8b dropped.
# Lifts the worst drawings hugely (ОДИ 12.5→62.5%, ПБ 43.8→56.2% on the hard set);
# a local qwen3-32b synthesizer matched the cloud-Sonnet ceiling. DRAWINGS ONLY by
# default — on dense TABLES the reformatting loses rows (КР3 53→13). Most real-doc
# tables are A4 text-rich → auto-skipped; --synthesize-all overrides the gate.
SYNTH_CALL_FN = None
SYNTH_ALL = False

# Verify-pass: if the verified output is shorter than this fraction of initial,
# reject and keep initial. GLM-4.6V tends to compress verify output (~75%
# shorter is normal), so use a stricter threshold for it; Gemini compresses
# less aggressively, so 0.6 is fine. See ADR-030.
VERIFY_MIN_RATIO_DEFAULT = 0.6
VERIFY_MIN_RATIO_BY_PROVIDER = {
    "glm": 0.85,
}

SYSTEM_PROMPT = """Ты эксперт по извлечению содержимого технических документов в Markdown.

Извлеки СОДЕРЖИМОЕ ВСЕЙ страницы в Markdown по следующим принципам.

1. ПОЛНОТА — обязательная.
Извлекай ВСЁ что видно на странице: каждую надпись, число, метку, символ, элемент, таблицу, штамп, легенду, выноску, фотографию, рамку, иллюстрацию, маркер. Если это видно — это должно быть в выводе. Не выбирай "важное" — извлекай всё.

2. ТОЧНОСТЬ — буквально как написано.
Текст — БУКВАЛЬНО как на странице. Числа — точно как написаны: "0,000", "-0,200", "6.3 т", не округляй, не меняй разделители ("," vs "."). Язык — язык документа. НЕ переводи. НЕ перефразируй. НЕ нормализуй сокращения, шифры, имена, марки: видишь "АББ" — пиши "АББ" (не "АБК"); "ХСА" — не "ХАС"; "T1.1" — сохраняй ровно тот алфавит (латиница/кириллица), что в источнике.

3. СТРУКТУРА ВЫВОДА ПОВТОРЯЕТ СТРУКТУРУ СТРАНИЦЫ.
Сам выбери уместную организацию Markdown под содержимое: заголовки разделов где видны разделы, таблицы где видны таблицы, списки где видны списки, отдельные подразделы для отдельных визуальных блоков (штамп, легенда, спецификация, схема, узел и т.д.). Порядок чтения — сверху вниз, слева направо. НЕ добавляй "# Страница N" — система добавит.

4. ОПИСАНИЕ ВИЗУАЛЬНОГО СОДЕРЖИМОГО — раздел "## Описание изображения".
Если на странице есть нетекстовое содержимое (чертёж, схема, план, разрез, диаграмма, график, фотография, иллюстрация, символическая запись, любая визуальная композиция) — добавь раздел "## Описание изображения" с детальным описанием, чтобы читатель без доступа к картинке мог понять, что на ней:
- ЧТО изображено (тип: план / разрез / схема / узел / график / фото / диаграмма / ...)
- ИЗ ЧЕГО состоит (все видимые компоненты с их марками / номерами / подписями)
- ГДЕ расположены элементы относительно друг друга (слева/справа/сверху/снизу/в центре, по осям, по отметкам, в каких рамках/группах)
- КАК связаны (стрелки, линии, потоки, соединения, направления, цвета)
- РАЗМЕРЫ / отметки / маркеры с указанием того, что они обозначают (если видно)

Если на странице несколько отдельных визуальных блоков (например, основная схема + 3 детальных узла + фото + диаграмма) — для КАЖДОГО отдельный подраздел "### Описание: <название/идентификатор>" с теми же полями. Не объединяй описания разных блоков.

5. ГРАНИЦЫ — только видимое.
Описывай ТОЛЬКО что физически видно на странице. Запрещено:
- выдумывать элементы, связи, размеры, которых нет
- "исправлять" то, что выглядит как опечатка (см. пункт 2)
- объяснять "зачем", "как работает", "почему", расшифровывать значения, делать выводы
- использовать знания о подобных документах вообще — описывай ровно эту страницу

6. НЕЯСНОЕ — помечай.
Если не уверен в прочтении — процитируй ровно то, что видишь; если действительно нечитаемо — пометь "[неразборчиво]". НЕ угадывай.

7. ТАБЛИЦЫ — JSON-блок в маркерах `<TABLE>`. НЕ markdown-таблица.

ВМЕСТО `|...|` markdown-таблицы выводи КАЖДУЮ таблицу СТРОГО в следующем формате:

<TABLE id="t1" title="Название таблицы как написано над ней">
{"headers":["Колонка1","Колонка2","Колонка3"],
"rows":[
["значение1","значение2","значение3"],
["значение4","значение5","значение6"]
],
"color_marks":[
{"target":"строка значение1","color":"розовый"},
{"target":"ячейка [значение4 × Колонка2]","color":"жёлтый"}
]}
</TABLE>

ПРАВИЛА:
- id="tN" — счётчик в пределах страницы (t1, t2, ...).
- title — точное название таблицы из изображения; пустая строка если названия нет.
- headers — массив строк, одна на колонку. Многоуровневая шапка (объединённые ячейки) → склей в ОДНУ строку через ` — `. Пример: верх "A", низ "x"/"y" → headers = ["A — x", "A — y"].
- rows — массив массивов строк, по одному массиву на строку данных. ВСЕГДА одинаковая длина = len(headers). Пустую ячейку оставляй пустой строкой "". Нечитаемую ячейку → "[неразборчиво]" (НЕ "").
- ВСЕ значения в ячейках — JSON-строки. Числа тоже в кавычках: "1850" а не 1850. Сохраняй разделитель как в источнике ("0,000", не "0.000").
- Экранирование внутри строк: `\\"` для кавычки, `\\n` для переноса строки, `\\\\` для обратного слэша. Многострочная ячейка → одна строка JSON с `\\n` внутри: `"первая\\nвторая"`.
- color_marks — список цветовых отметок, по одной на цветовую группу:
  - формат target: `"столбец Колонка1"` / `"строка значение1"` (ключ = значение в первой колонке строки) / `"ячейка [значение1 × Колонка1]"` (отдельная ячейка)
  - color — русское название как видишь: розовый, жёлтый, голубой, зелёный, серый, бежевый, светло-серый и т.п.
  - Если заливки нет — пиши `"color_marks":[]` (пустой массив, поле обязательно).
- Повторяющаяся таблица для разных секций → каждой секции отдельный `<TABLE>` с собственным id и title="Название секции".
- Доп. столбцы только у одной секции (нет в других) → отдельный `<TABLE>` с своим id, title указывает на секцию-источник.
- ЗАПРЕЩЕНО: markdown-таблица с `|` и `---`. Только `<TABLE>...</TABLE>` JSON-блок. Если видишь таблицу — эмитируй JSON-блок, не markdown.
- ЗАПРЕЩЕНО: pretty-print / выравнивание пробелами / отступы в JSON. Компактный JSON, переносы строк только между headers / каждой row / color_marks (как в примере). НИКАКИХ дефисов или пробелов для красоты.
- ЗАПРЕЩЕНО: комментарии в JSON, trailing comma, одинарные кавычки."""

PROMPT_TEXT_RICH = """Извлеки ВСЁ содержимое страницы в Markdown по правилам системы-промпта.

Текстовый слой PDF (вспомогательный источник; верь изображению при расхождении):
{text}"""

PROMPT_DRAWING = """Извлеки ВСЁ содержимое страницы в Markdown по правилам системы-промпта. Изображение — основной источник.

Текстовый слой PDF (вспомогательный, может быть неполным или нарушать порядок):
{text}"""

PROMPT_TILE = """Это ОДИН ФРАГМЕНТ страницы: строка {row}, столбец {col} из сетки {rows}x{cols}. Соседние фрагменты пересекаются с этим (~12% по краям).

Извлеки ВСЁ что физически видно НА ЭТОМ ФРАГМЕНТЕ — текст, числа, элементы, таблицы (даже частичные), визуальное содержимое. Используй принципы системы-промпта.

Дополнительно:
- НЕ додумывай, что может быть на соседних фрагментах
- Если элемент частично уходит за границу — отметь это (например, "пайплайн T1.1 входит сверху, идёт вниз, уходит за правую границу фрагмента")
- Раздел "### Содержимое фрагмента" — описание того, что изображено ВНУТРИ фрагмента и куда уходят связи к краям

Текстовый слой ВСЕЙ страницы (для контекста):
{text}"""

PROMPT_TILE_MERGE = """Тебе даны извлечения с {n} фрагментов одной страницы и УМЕНЬШЕННОЕ изображение всей страницы. Фрагменты пересекаются на краях (~12%).

Используй уменьшенное изображение страницы ТОЛЬКО для двух целей:
1) восстановить блоки, разорванные между фрагментами (таблицы условных обозначений, схемы узлов, штамп, спецификации) — собрать их как один цельный блок;
2) проверить порядок чтения и взаимное расположение блоков.
НЕ читай мелкие числа/символы с уменьшенной картинки — для этого фрагменты. Тонкие лейблы и подписи бери только из фрагментов.

Объедини в ОДИН цельный Markdown-документ страницы по правилам системы-промпта:
- Таблицы — ЕДИНЫЙ `<TABLE>` блок на каждую логическую таблицу (формат — принцип 7 системы-промпта). Объедини строки из всех фрагментов где видна эта таблица, дубликаты на пересечениях устрани. Если фрагменты эмитировали отдельные `<TABLE>` блоки для одной логической таблицы — слей их в один с общими headers и объединённым rows.
- ЗАПРЕЩЕНО конвертировать `<TABLE>` блоки в markdown-таблицу с `|` и `---`. Только JSON-формат принципа 7.
- Удали дубликаты на пересечениях
- Сохрани порядок чтения: сверху вниз, слева направо
- Сшей описания фрагментов в раздел "## Описание изображения" + по подразделу "### Описание: <название>" для каждого отдельного визуального блока на странице (если их несколько). Если связь шла через границу фрагмента (в одном уходит вправо, в другом входит слева) — это одна связь
- НИКАКИХ упоминаний фрагментов / "Фрагмент 1,2" в финальном выводе
- НЕ добавляй того, чего не было в извлечениях фрагментов — ИСКЛЮЧЕНИЕ: если фрагменты явно обрезали многострочную таблицу/легенду/штамп (видно по картинке, что блок продолжается), допиши недостающие строки/ячейки опираясь на полную картинку, помечая их в исходном алфавите как видно

Извлечения с фрагментов:

{tile_outputs}"""

PROMPT_TILE_MERGE_PARTIAL = """Тебе даны извлечения с НЕСКОЛЬКИХ соседних фрагментов одной страницы (часть всей сетки). Фрагменты пересекаются на краях (~12%). Изображения страницы здесь НЕТ — работай только по тексту извлечений.

Слей эти фрагменты в ОДИН частичный Markdown-кусок по правилам системы-промпта:
- объедини строки одной логической таблицы из разных фрагментов, дубликаты на пересечениях устрани;
- сохрани порядок чтения сверху-вниз, слева-направо;
- НИКАКИХ упоминаний «Фрагмент N» в выводе;
- кириллицу НЕ латинизировать;
- НЕ добавляй того, чего не было во фрагментах (картинки нет — достраивать обрезанные блоки нельзя, это сделает финальная склейка);
- если таблица/блок явно продолжается за пределы этих фрагментов — оставь как есть, не дописывай.

Извлечения с фрагментов:

{tile_outputs}"""

PROMPT_TILE_MERGE_TEXT = """Тебе даны извлечения с {n} фрагментов ОДНОЙ страницы (сетка тайлов, метки «Фрагмент строка,столбец»). Фрагменты пересекаются на краях (~12-18%). Изображения НЕТ — собирай ЦЕЛЬНЫЙ документ страницы только по тексту фрагментов.

Объедини в ОДИН цельный Markdown-документ по правилам системы-промпта:
- по меткам «Фрагмент r,c» восстанови раскладку: r — сверху вниз, c — слева направо;
- ОДНУ логическую таблицу, разрезанную между фрагментами, собери в ЕДИНЫЙ `<TABLE>` блок (формат — принцип 7): объедини строки из всех фрагментов, где видна эта таблица; на нахлёстах строки повторяются — дубликаты устрани;
- сшей описания фрагментов в раздел «## Описание изображения» (+ подразделы «### Описание: <название>» для отдельных блоков); связь, уходящая за границу в одном фрагменте и входящая в соседнем — это ОДНА связь;
- сохрани порядок чтения сверху-вниз, слева-направо;
- НИКАКИХ упоминаний «Фрагмент N» в финале;
- кириллицу НЕ латинизировать (В1≠B1, ХСА≠XCA, Т1.1≠T1.1 — сохрани алфавит как во фрагментах);
- НЕ добавляй того, чего не было во фрагментах (картинки нет — выдумывать недостающее нельзя).

Извлечения с фрагментов:

{tile_outputs}"""

PROMPT_VERIFY = """Тебе дано текущее Markdown-извлечение страницы и сама страница (изображение). Сверь и выведи ИСПРАВЛЕННЫЙ ПОЛНЫЙ Markdown по правилам системы-промпта.

Найди и устрани:
- Пропуски: что есть на изображении, но нет в извлечении (текст, числа, строки таблиц, элементы штампа, отдельные визуальные блоки, для которых нет подраздела "### Описание: ...")
- Ошибки прочтения: неверные цифры/буквы/знаки
- Нормализацию: верни оригинальные сокращения/шифры/имена как на изображении (АББ ≠ АБК; T ≠ Т)
- Выдуманное: удали из описания всё, чего на изображении нет
- Неполные описания визуальных блоков: уточни состав/расположение/связи

ТАБЛИЦЫ: сохраняй формат `<TABLE id="..." title="...">{{JSON}}</TABLE>` принципа 7 системы-промпта. Если в текущем извлечении таблица в markdown-формате `|...|` — конвертируй её в `<TABLE>` JSON-блок. Если уже в JSON — исправь только содержимое (rows, headers, color_marks), формат не меняй. Валидный JSON обязателен.

Выводи только финальный документ. Без пометок "исправлено", без комментариев.

Текущее извлечение:
{current_md}"""


# ── DATA-FIRST набор промптов (--data-first) ───────────────────────────────────
# Лёгкий, заточенный под мелкие локальные VLM (qwen3-vl-8b). Снимает «форматный
# налог»: вместо построения Markdown/<TABLE>-JSON просим данные построчно/CSV. На
# слепых тестах против эталона поднял qwen3-vl-8b: таблица 88→93% (вернулись
# под-блоки и итоги), чертёж 65→85% (марки помещений 0/7→7/7, меньше галлюцинаций).
# Подменяет SYSTEM_PROMPT + все per-page/tile промпты разом (см. main --data-first).
DATA_FIRST_SYSTEM_PROMPT = """Ты — точный экстрактор данных с русских инженерных документов (проектная документация). Задача — ДОСЛОВНО перенести в текст ВСЁ видимое: числа, коды, обозначения, подписи, тексты штампов, легенд, спецификаций, таблиц.

ФОРМАТ ВЫВОДА — ПРОСТОЙ ТЕКСТ, НЕ Markdown. Никаких #-заголовков, **жирного**, |таблиц|. Только данные построчно.

ЖЕЛЕЗНЫЕ ПРАВИЛА:
1. ПОЛНОТА. Переноси ВСЁ что видно — каждую надпись, число, метку, символ. Не выбирай «важное».
2. ТОЧНОСТЬ — буквально. Числа как написаны («0,000», «-0,200», «6.3 т»), не округляй, разделитель («,»/«.») как в источнике.
3. НИКОГДА не заменяй русские буквы латинскими и наоборот. «В1» — это кириллица, не «B1». «ХСА» — не «XCA». «ПЕ3.1» — не «ПЭЗ.1». Латинское «T1.1» оставь латиницей. Сохраняй ровно тот алфавит, что в источнике.
4. НЕ выдумывай. Если фрагмент нечитаем — напиши «(не читается)», НЕ угадывай.
5. НЕ переводи, НЕ перефразируй, НЕ «исправляй» опечатки (АББ ≠ АБК; ХСА ≠ ХАС).
6. ТАБЛИЦЫ — как CSV: каждая строка таблицы = одна текстовая строка, ячейки через «;». Уровни шапки — отдельными строками с префиксом «ШАПКА;». НЕ склеивай уровни шапки, НЕ выравнивай столбцы. Пустая ячейка — пусто между «;» (две «;» подряд). Извлекай ВСЕ строки, включая под-блоки и итоговые строки.
7. ЧЕРТЁЖ/СХЕМА — каждый факт отдельной строкой: марки и номера (формат номер/буква, напр. «119/Г»); текст штампа (объект, организация, стадия, шифр, листы, ФИО, даты, формат); легенда и условные обозначения; спецификации узлов (позиция; наименование; точная марка ИЛИ «(не читается)»); отметки, температуры, размеры."""

DATA_FIRST_PROMPT_TEXT_RICH = """Извлеки ВСЁ содержимое страницы простым текстом по правилам системы-промпта (данные построчно, таблицы как CSV, кириллицу не латинизировать).

Текстовый слой PDF (вспомогательный источник; верь изображению при расхождении):
{text}"""

DATA_FIRST_PROMPT_DRAWING = """Извлеки ВСЁ содержимое страницы простым текстом по правилам системы-промпта. Изображение — основной источник. Марки/номера, текст штампа, легенду, спецификации, отметки/температуры/размеры — каждый факт отдельной строкой. Таблицы — как CSV. Кириллицу НЕ латинизировать, ничего не выдумывать.

Текстовый слой PDF (вспомогательный, может быть неполным):
{text}"""

DATA_FIRST_PROMPT_TILE = """Это ОДИН ФРАГМЕНТ страницы: строка {row}, столбец {col} из сетки {rows}x{cols}. Соседние фрагменты пересекаются (~12% по краям).

Извлеки ВСЁ что физически видно НА ЭТОМ ФРАГМЕНТЕ простым текстом по правилам системы-промпта (данные построчно, таблицы как CSV, кириллицу не латинизировать, не выдумывать). НЕ додумывай соседние фрагменты. Если элемент уходит за границу — отметь это.

Текстовый слой ВСЕЙ страницы (для контекста):
{text}"""

DATA_FIRST_PROMPT_TILE_MERGE = """Тебе даны извлечения с {n} фрагментов одной страницы и УМЕНЬШЕННОЕ изображение всей страницы. Фрагменты пересекаются (~12%).

Уменьшенное изображение используй ТОЛЬКО чтобы: 1) восстановить блоки, разорванные между фрагментами (таблицы, легенды, узлы, штамп); 2) проверить порядок чтения. Мелкие числа/подписи бери из фрагментов, НЕ с уменьшенной картинки.

Объедини в ОДИН цельный документ простым текстом по правилам системы-промпта:
- данные построчно; таблицы как CSV (одна строка = строка, ячейки через «;», уровни шапки строками «ШАПКА;»);
- объедини одну логическую таблицу из всех фрагментов в один блок, дубликаты на пересечениях устрани;
- сохрани порядок чтения сверху-вниз, слева-направо;
- НИКАКИХ упоминаний «Фрагмент N» в выводе;
- кириллицу НЕ латинизировать; не добавляй того, чего не было во фрагментах (исключение — дописать строки явно обрезанной таблицы/легенды/штампа, опираясь на полную картинку).

Извлечения с фрагментов:

{tile_outputs}"""

DATA_FIRST_PROMPT_TILE_MERGE_PARTIAL = """Тебе даны извлечения с НЕСКОЛЬКИХ соседних фрагментов одной страницы (часть всей сетки). Фрагменты пересекаются (~12%). Изображения страницы здесь НЕТ — работай только по тексту извлечений.

Слей эти фрагменты в ОДИН частичный кусок простым текстом по правилам системы-промпта:
- данные построчно; таблицы как CSV (одна строка = строка, ячейки через «;», уровни шапки строками «ШАПКА;»);
- объедини одну логическую таблицу из этих фрагментов в один блок, дубликаты на пересечениях устрани;
- сохрани порядок чтения сверху-вниз, слева-направо;
- НИКАКИХ упоминаний «Фрагмент N» в выводе;
- кириллицу НЕ латинизировать;
- НЕ добавляй того, чего не было во фрагментах (картинки нет — обрезанные блоки достроит финальная склейка); если блок явно продолжается за пределы этих фрагментов — оставь как есть.

Извлечения с фрагментов:

{tile_outputs}"""

DATA_FIRST_PROMPT_TILE_MERGE_TEXT = """Тебе даны извлечения с {n} фрагментов ОДНОЙ страницы (сетка тайлов, метки «Фрагмент строка,столбец»). Фрагменты пересекаются (~12-18%). Изображения НЕТ — собирай ЦЕЛЬНЫЙ документ страницы только по тексту фрагментов.

Объедини в ОДИН цельный документ ПРОСТЫМ ТЕКСТОМ по правилам системы-промпта:
- по меткам «Фрагмент r,c» восстанови раскладку: r — сверху вниз, c — слева направо;
- данные построчно; таблицы как CSV (одна строка = строка, ячейки через «;», уровни шапки строками «ШАПКА;»);
- ОДНУ логическую таблицу, разрезанную между фрагментами, собери в один блок: объедини строки из всех фрагментов, дубликаты на нахлёстах устрани, порядок строк сохрани;
- марки/номера, штамп, легенду, спецификации, отметки/температуры — каждый факт отдельной строкой, как во фрагментах;
- сохрани порядок чтения сверху-вниз, слева-направо;
- НИКАКИХ упоминаний «Фрагмент N» в финале;
- кириллицу НЕ латинизировать (В1≠B1, ХСА≠XCA, Т1.1≠T1.1 — алфавит как во фрагментах);
- НЕ добавляй того, чего не было во фрагментах (картинки нет — выдумывать нельзя).

Извлечения с фрагментов:

{tile_outputs}"""

DATA_FIRST_PROMPT_VERIFY = """Тебе дано текущее извлечение страницы (простой текст) и сама страница (изображение). Сверь и выведи ИСПРАВЛЕННОЕ ПОЛНОЕ извлечение по правилам системы-промпта (простой текст, таблицы как CSV).

Найди и устрани:
- Пропуски: что есть на изображении, но нет в извлечении (числа, строки таблиц, элементы штампа, марки помещений, под-блоки и итоговые строки таблиц).
- Ошибки прочтения: неверные цифры/буквы/знаки.
- Латинизацию кириллицы: верни кириллицу (B1→В1, XCA→ХСА), сохрани исходный алфавит.
- Выдуманное: удали всё, чего на изображении нет.
НЕ меняй формат на Markdown. Выводи только финальное извлечение, без пометок.

Текущее извлечение:
{current_md}"""


def parse_page_range(page_range_str, total_pages):
    """Parse page range like '5-10' or '3' into list of 0-based indices."""
    if not page_range_str:
        return list(range(total_pages))
    pages = []
    for part in page_range_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            start = max(1, int(start))
            end = min(total_pages, int(end))
            pages.extend(range(start - 1, end))
        else:
            p = int(part)
            if 1 <= p <= total_pages:
                pages.append(p - 1)
    return sorted(set(pages))


def render_full_page(page, dpi):
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes("png"), pix.width, pix.height


def raster_native_dpi(page, dpi):
    """If `page` is dominated by a single embedded raster image, return the DPI at
    which the rendered page matches that image's NATIVE pixel resolution, capped at
    `dpi`. Rendering a raster page above native only upscales (no new detail) while
    multiplying tiles + merge variance, so we never render above native on such
    pages. Returns `dpi` UNCHANGED for vector-rich pages (no dominant image →
    higher DPI there adds real detail). Pure read of page geometry — generic, keys
    off image coverage, not content. See ADR-040."""
    try:
        infos = page.get_image_info()
    except Exception:
        return dpi
    page_area = page.rect.width * page.rect.height
    if not infos or page_area <= 0:
        return dpi
    # Largest image by placement area on the page.
    def _area(im):
        x0, y0, x1, y1 = im["bbox"]
        return abs(x1 - x0) * abs(y1 - y0)
    best = max(infos, key=_area)
    if _area(best) / page_area < RASTER_COVER_MIN:
        return dpi  # not raster-dominated (vector page) → leave DPI as is
    img_w, img_h = best.get("width", 0), best.get("height", 0)
    if img_w <= 0 or img_h <= 0:
        return dpi
    # DPI that renders each axis to ≤ native pixels; the binding (smaller) one caps.
    native_dpi = min(img_w * 72.0 / page.rect.width,
                     img_h * 72.0 / page.rect.height)
    return max(72, min(dpi, int(native_dpi)))


def compute_tile_grid(width_px, height_px):
    """Pick a grid (rows, cols) so each tile fits under TILE_MAX_PIXEL."""
    cols = max(1, -(-width_px // TILE_MAX_PIXEL))   # ceil div
    rows = max(1, -(-height_px // TILE_MAX_PIXEL))
    return rows, cols


def compute_tile_rects(page_rect, rows, cols, overlap_pct):
    """Return list of (row, col, fitz.Rect) tiles in page coordinates with overlap."""
    w, h = page_rect.width, page_rect.height
    tw, th = w / cols, h / rows
    ow, oh = tw * overlap_pct, th * overlap_pct
    tiles = []
    for r in range(rows):
        for c in range(cols):
            x0 = max(0, c * tw - ow)
            y0 = max(0, r * th - oh)
            x1 = min(w, (c + 1) * tw + ow)
            y1 = min(h, (r + 1) * th + oh)
            tiles.append((r, c, fitz.Rect(x0, y0, x1, y1)))
    return tiles


def render_tile(page, clip, dpi):
    pix = page.get_pixmap(dpi=dpi, clip=clip)
    return pix.tobytes("png")


def cap_image_pixels(png_bytes, max_px):
    """Downscale a PNG (Lanczos) so its longest side is <= max_px. Returns the
    bytes UNCHANGED if already within the cap, and — on any failure — the original
    bytes (never raises): a slightly-too-big image that 413s is recoverable via the
    call() retry, a crash here is not. Generic guard against oversized request
    images regardless of which upstream DPI cap produced them. See ADR-041."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes))
        longest = max(im.size)
        if longest <= max_px:
            return png_bytes
        scale = max_px / float(longest)
        new_size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
        im = im.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        print(f"    cap_image_pixels failed ({e}); sending original image", file=sys.stderr)
        return png_bytes


def extract_text_and_images(pdf_path, page_indices):
    """First pass: get text layer and render full-page PNG. Decide if tiling is needed."""
    doc = fitz.open(pdf_path)
    results = []
    for idx in page_indices:
        page = doc[idx]
        text = page.get_text("text").strip()
        is_large_format = max(page.rect.width, page.rect.height) > LARGE_FORMAT_PT
        is_text_rich = (len(text) > TEXT_RICH_THRESHOLD) and not is_large_format
        dpi = DPI_TEXT if is_text_rich else DPI_DRAWING
        # Never render a raster-dominated page above its image's native resolution —
        # upscaling only multiplies tiles + merge variance (ADR-040).
        if RASTER_NATIVE_DPI_CAP:
            capped = raster_native_dpi(page, dpi)
            if capped < dpi:
                print(f"    page {idx+1}: raster-dominated → render DPI capped "
                      f"{dpi}→{capped} (native resolution)", file=sys.stderr)
                dpi = capped
        png, w, h = render_full_page(page, dpi)
        needs_tiling = (max(w, h) > TILE_MAX_PIXEL) and not is_text_rich
        results.append({
            "page_num": idx + 1,
            "text": text,
            "png": png,
            "width": w,
            "height": h,
            "is_text_rich": is_text_rich,
            "dpi": dpi,
            "needs_tiling": needs_tiling,
            "page_rect": fitz.Rect(page.rect),
        })
    doc.close()
    return results


def _is_transient(err_str):
    """5xx, rate-limits, and network hiccups deserve a retry."""
    s = err_str.lower()
    return any(x in s for x in ("502", "503", "504", "bad gateway",
                                 "service unavailable", "gateway timeout",
                                 "internal server error", "connection reset",
                                 "timeout", "temporarily unavailable"))


def _is_too_large(err_str):
    """HTTP 413 — request body too large. Recovered by shrinking the image and
    retrying (see call()), not by waiting like a transient error."""
    s = err_str.lower()
    return "413" in s or "too large" in s or "request entity" in s


def _run_with_timeout(fn, timeout):
    """Run fn() with a hard timeout. Hung HTTP requests are interrupted instead
    of blocking the whole pipeline indefinitely. Returns (result, error_str).
    On timeout, returns (None, '[Error: call timeout]')."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout), None
        except concurrent.futures.TimeoutError:
            return None, "[Error: call timeout]"
    finally:
        executor.shutdown(wait=False)


def _gemini_call_once(client, model, image_pngs, text_prompt, max_tokens):
    """One Gemini Vision API call. Returns (text, finish_reason, error_str)."""
    from google.genai import types as _gtypes
    parts = [_gtypes.Part.from_bytes(data=img, mime_type="image/png") for img in image_pngs]
    parts.append(_gtypes.Part.from_text(text=text_prompt))
    try:
        response = client.models.generate_content(
            model=model,
            contents=parts,
            config=_gtypes.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.0,
                max_output_tokens=max_tokens,
            ),
        )
    except Exception as e:
        return "", "", str(e)
    text = response.text or ""
    try:
        fr = str(response.candidates[0].finish_reason)
    except Exception:
        fr = ""
    return text, fr, ""


def _openai_compat_call_once(client, model, image_pngs, text_prompt, max_tokens, extra_body=None):
    """One OpenAI-compatible vision call (GLM / Qwen / etc.). Returns
    (text, finish_reason, error_str)."""
    content = []
    for img in image_pngs:
        b64 = base64.b64encode(img).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})
    content.append({"type": "text", "text": text_prompt})
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]
    kwargs = dict(model=model, messages=messages, temperature=0.0, max_tokens=max_tokens)
    if FREQUENCY_PENALTY:
        kwargs["frequency_penalty"] = FREQUENCY_PENALTY
    if extra_body:
        kwargs["extra_body"] = extra_body
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as e:
        return "", "", str(e)
    text = resp.choices[0].message.content or ""
    try:
        fr = str(resp.choices[0].finish_reason or "")
    except Exception:
        fr = ""
    return text, fr, ""


def _anthropic_call_once(client, model, image_pngs, text_prompt, max_tokens, options=None):
    """One Claude (Anthropic) call via the official SDK. Streams and returns the
    final message so any max_tokens works without an HTTP-timeout guard trip.
    `options` carries thinking/effort (e.g. Sonnet with adaptive thinking + high
    effort) passed through extra_body so it works across SDK versions. Returns
    (text, finish_reason, error_str). See ADR-037."""
    options = options or {}
    content = []
    for img in image_pngs:
        b64 = base64.b64encode(img).decode()
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": b64}})
    content.append({"type": "text", "text": text_prompt})
    extra_body = {}
    if options.get("thinking"):
        extra_body["thinking"] = options["thinking"]
    if options.get("effort"):
        extra_body["output_config"] = {"effort": options["effort"]}
    try:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            extra_body=extra_body or None,
        ) as stream:
            msg = stream.get_final_message()
    except Exception as e:
        return "", "", str(e)
    # Concatenate only text blocks (skip thinking blocks).
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    fr = str(getattr(msg, "stop_reason", "") or "")
    return text, fr, ""


def _claude_cli_call_once(cli_path, model, image_pngs, text_prompt, max_tokens):
    """One headless Claude Code (`claude -p`) call — reuses the local Claude Code
    login (subscription), so NO API key is needed. Text-only: the merge step that
    uses this passes no image (vision=False). The data-first SYSTEM_PROMPT is set
    as the session system prompt (replacing Claude Code's coding-agent default);
    the merge prompt is fed on stdin. Returns (text, finish_reason, error_str).
    See ADR-038."""
    if image_pngs:
        return "", "", "[Error: claude-cli backend is text-only (no image input)]"
    cmd = [cli_path, "-p", "--model", model, "--output-format", "text",
           "--system-prompt", SYSTEM_PROMPT,
           # Pure text transform — forbid all tools so it can't touch the FS.
           "--disallowed-tools", "Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebFetch", "WebSearch"]
    try:
        proc = subprocess.run(cmd, input=text_prompt, capture_output=True,
                              text=True, timeout=GEMINI_CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "", "", "[Error: claude-cli timeout]"
    except Exception as e:
        return "", "", f"[Error: claude-cli spawn failed: {e}]"
    if proc.returncode != 0:
        return "", "", f"[Error: claude-cli exit {proc.returncode}: {(proc.stderr or '').strip()[:300]}]"
    return (proc.stdout or "").strip(), "stop", ""


def make_provider_call_fn(provider):
    """Return a callable (image_pngs, text_prompt, max_tokens) → str with
    retries, per-call timeout, and truncation-detection (auto-bump tokens once
    on MAX_TOKENS finish_reason). Provider-specific clients are built once and
    captured in the closure.

    Errors and exhausted retries return a string starting with '[Error: ...]'
    so callers can detect failures without exceptions."""
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'. Known: {sorted(PROVIDERS)}")
    cfg = PROVIDERS[provider]
    # Providers without an "env" (e.g. claude-cli) authenticate out-of-band
    # (local Claude Code login) and need no API key.
    api_key = None
    if cfg.get("env"):
        api_key = os.getenv(cfg["env"])
        if not api_key:
            raise RuntimeError(f"{cfg['env']} not set for provider '{provider}'. "
                               f"Add it to .env or export it.")

    backend = cfg["backend"]
    model = cfg["model"]
    max_tokens_cap = cfg.get("max_tokens_cap")  # provider-side hard limit if any

    if backend == "gemini":
        from google import genai as _genai
        client = _genai.Client(api_key=api_key)
        call_once = lambda imgs, prompt, mt: _gemini_call_once(client, model, imgs, prompt, mt)
    elif backend == "openai-compat":
        from openai import OpenAI as _OpenAI
        base_url = os.getenv(cfg["base_url_env"], cfg["base_url_default"])
        client = _OpenAI(base_url=base_url, api_key=api_key)
        extra_body = cfg.get("extra_body")
        call_once = lambda imgs, prompt, mt: _openai_compat_call_once(client, model, imgs, prompt, mt, extra_body)
    elif backend == "anthropic":
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=api_key)
        options = {"thinking": cfg.get("thinking"), "effort": cfg.get("effort")}
        call_once = lambda imgs, prompt, mt: _anthropic_call_once(client, model, imgs, prompt, mt, options)
    elif backend == "claude-cli":
        cli = cfg.get("cli_path", "claude")
        call_once = lambda imgs, prompt, mt: _claude_cli_call_once(cli, model, imgs, prompt, mt)
    else:
        raise ValueError(f"Unknown backend '{backend}'")

    def call(image_pngs, text_prompt, max_tokens=65536, max_retries=3):
        if max_tokens_cap is not None:
            max_tokens = min(max_tokens, max_tokens_cap)
        # Cap every outgoing image's longest side up front so an oversized sheet
        # can't 413 the request before we start (root cause of the merge-image
        # 413s). Idempotent on already-small tiles. See ADR-041.
        imgs = [cap_image_pixels(p, REQUEST_MAX_PIXEL) for p in image_pngs]
        current_max = max_tokens
        bumped = False
        shrinks = 0
        last_err = ""
        for attempt in range(max_retries):
            result, timeout_err = _run_with_timeout(
                lambda: call_once(imgs, text_prompt, current_max),
                GEMINI_CALL_TIMEOUT,
            )
            if timeout_err:
                print(f"  Call timed out after {GEMINI_CALL_TIMEOUT}s (attempt {attempt+1}/{max_retries})", file=sys.stderr)
                if attempt < max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                return timeout_err

            text, fr, err = result
            if err:
                last_err = err[:500]
                # HTTP 413: body too large (byte-size, even after the pixel cap on a
                # very dense sheet). Halve the image(s) and retry. See ADR-041.
                if imgs and _is_too_large(last_err) and shrinks < REQUEST_SHRINK_RETRIES:
                    shrinks += 1
                    cap = max(REQUEST_MIN_PIXEL, REQUEST_MAX_PIXEL // (2 ** shrinks))
                    imgs = [cap_image_pixels(p, cap) for p in imgs]
                    print(f"  HTTP 413 — shrinking images to <= {cap}px, retry {shrinks}/{REQUEST_SHRINK_RETRIES}", file=sys.stderr)
                    continue
                if attempt < max_retries - 1 and _is_transient(last_err):
                    wait = 2 ** (attempt + 1)
                    print(f"  Retry in {wait}s: {last_err}", file=sys.stderr)
                    time.sleep(wait)
                    continue
                return f"[Error: {last_err}]"

            # Truncation: bump tokens once and retry.
            if (fr.endswith("MAX_TOKENS") or fr.lower() == "length") and not bumped:
                ceiling = max_tokens_cap if max_tokens_cap is not None else 131072
                if current_max < ceiling:
                    new_max = min(ceiling, current_max * 2)
                    print(f"  Output truncated ({fr}), retrying with token budget {new_max}", file=sys.stderr)
                    current_max = new_max
                    bumped = True
                    continue
            return text
        return f"[Error: exhausted retries — last: {last_err}]"

    return call


def extract_page_full(call_fn, page_data):
    """Single-image extraction: full page + text layer."""
    if page_data["is_text_rich"]:
        prompt = PROMPT_TEXT_RICH.format(text=page_data["text"] or "(пусто)")
    else:
        prompt = PROMPT_DRAWING.format(text=page_data["text"] or "(пусто)")
    return call_fn([page_data["png"]], prompt)


def _capped_merge_dpi(page_rect):
    """DPI for the full-page merge image, lowered so its longest side stays under
    MERGE_MAX_PIXEL. A fixed MERGE_FULL_PAGE_DPI on an A0/A1 foldout yields a
    5000-7000px PNG whose base64 alone trips the provider 413; this caps it while
    keeping small pages at the full MERGE_FULL_PAGE_DPI. See ADR-035.

    No 72-DPI floor: on a giant foldout (page longer than ~57in at 4096px cap) even
    72 DPI overruns MERGE_MAX_PIXEL — the old `max(72, …)` floor silently defeated
    the cap and was the real cause of the merge-image 413s (e.g. a 146in sheet → 72
    DPI → 10530px). The pixel cap must win; the merge image is only for macro block
    boundaries, so a low DPI is fine. See ADR-041."""
    longest_in = max(page_rect.width, page_rect.height) / 72.0  # points → inches
    if longest_in <= 0:
        return MERGE_FULL_PAGE_DPI
    return max(1, min(MERGE_FULL_PAGE_DPI, int(MERGE_MAX_PIXEL / longest_in)))


def _chunk_by_chars(blocks, max_chars):
    """Group consecutive blocks into batches whose joined length stays under
    max_chars (a single oversized block becomes its own batch)."""
    batches, cur, cur_len = [], [], 0
    for b in blocks:
        if cur and cur_len + len(b) > max_chars:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(b)
        cur_len += len(b)
    if cur:
        batches.append(cur)
    return batches


def _merge_tile_outputs(call_fn, tile_blocks, n_total, full_page_img, depth=0,
                        merge_vision=True):
    """Stitch tile outputs into one page document, going hierarchical when the
    concatenated text would overrun a single merge request (provider 413).

    - Fits in MERGE_MAX_INPUT_CHARS → one final merge. If the merge model is
      vision-capable it gets the low-res full-page image (reconstructs blocks
      split across tiles); a text-only merge model (--merge-provider, e.g. a dense
      text LLM or Claude with thinking) gets the grid-labelled tile texts only and
      stitches by reading order + overlap. See ADR-035, ADR-037.
    - Too big → batch the blocks under the char budget, merge each batch
      text-only (no image) in parallel, then recurse on the partials.

    Termination is guaranteed: each level either fits (terminal model merge),
    strictly reduces the block count (batching grouped blocks), or — when the
    budget is smaller than a single block, so batching can't group anything —
    falls back to a LOCAL concatenation (no request, never 413). The depth cap is
    a final backstop. In production (data-first tiles are tiny; markdown tiles cap
    at MAX_TILE_CHARS « budget) the fits-branch handles the common case directly."""
    total_chars = sum(len(b) for b in tile_blocks)
    # Final merge: everything fits in one call.
    if total_chars <= MERGE_MAX_INPUT_CHARS:
        merged_text = "\n\n".join(tile_blocks)
        if merge_vision:
            merge_prompt = PROMPT_TILE_MERGE.format(n=n_total, tile_outputs=merged_text)
            return call_fn([full_page_img], merge_prompt)
        # Text-only merge model: no image, grid-labelled texts only.
        merge_prompt = PROMPT_TILE_MERGE_TEXT.format(n=n_total, tile_outputs=merged_text)
        return call_fn([], merge_prompt)

    batches = _chunk_by_chars(tile_blocks, MERGE_MAX_INPUT_CHARS)
    # Batching grouped nothing (each block already ≥ budget) or we've recursed too
    # deep → a model merge can't shrink it and would either loop or 413. Return a
    # locally concatenated document instead. Rare: only on pathologically large
    # pages whose content exceeds the budget even after roll-up.
    if len(batches) >= len(tile_blocks) or depth >= MERGE_MAX_DEPTH:
        print(f"    Merge (depth {depth}): {len(tile_blocks)} blocks ({total_chars} ch) "
              f"exceed budget after roll-up → local concatenation (no further merge)", file=sys.stderr)
        return "\n\n".join(tile_blocks)

    print(f"    Hierarchical merge (depth {depth}): {len(tile_blocks)} blocks "
          f"({total_chars} ch) → {len(batches)} text-only batches", file=sys.stderr)
    partials = [None] * len(batches)

    def _merge_batch(bi):
        merged_text = "\n\n".join(batches[bi])
        prompt = PROMPT_TILE_MERGE_PARTIAL.format(tile_outputs=merged_text)
        return call_fn([], prompt)

    with concurrent.futures.ThreadPoolExecutor(max_workers=TILE_CONCURRENCY) as ex:
        futs = {ex.submit(_merge_batch, bi): bi for bi in range(len(batches))}
        for fut in concurrent.futures.as_completed(futs):
            partials[futs[fut]] = fut.result()

    return _merge_tile_outputs(call_fn, partials, n_total, full_page_img, depth + 1,
                               merge_vision=merge_vision)


def extract_page_tiled(call_fn, page_data, pdf_path):
    """Tile-based extraction for large drawings + merge pass.

    Tile calls run in parallel (TILE_CONCURRENCY workers) — they're independent,
    so this gives a near-linear speedup on drawing pages."""
    rows, cols = compute_tile_grid(page_data["width"], page_data["height"])
    doc = fitz.open(pdf_path)
    page = doc[page_data["page_num"] - 1]
    tiles = compute_tile_rects(page_data["page_rect"], rows, cols, TILE_OVERLAP_PCT)
    print(f"    Tiling {rows}x{cols} = {len(tiles)} tiles at {page_data['dpi']} DPI (parallel={TILE_CONCURRENCY})", file=sys.stderr)

    # Render all tiles serially (fast — local PyMuPDF, no API). Keep each tile's
    # clip so a failed tile can be re-rendered at higher DPI for a re-read (ADR-039).
    rendered = []
    for r, c, clip in tiles:
        rendered.append((r, c, render_tile(page, clip, page_data["dpi"]), clip))
    # Also render a low-DPI full-page image for the merge step to spot blocks
    # that got split across tile boundaries (legends, schematic nodes, stamps).
    # DPI is capped so big A0/A1 sheets don't produce an oversized merge image (413).
    merge_dpi = _capped_merge_dpi(page_data["page_rect"])
    if merge_dpi < MERGE_FULL_PAGE_DPI:
        print(f"    Merge image DPI capped {MERGE_FULL_PAGE_DPI}→{merge_dpi} (large sheet)", file=sys.stderr)
    full_page_for_merge = page.get_pixmap(dpi=merge_dpi).tobytes("png")
    doc.close()

    # Per-page re-run budget (thread-safe) for the tile detect-and-rerun policy.
    rerun_budget = {"used": 0, "max": TILE_RERUN_BUDGET, "lock": threading.Lock()}

    def _process_tile(item):
        r, c, tile_png, clip = item
        prompt = PROMPT_TILE.format(
            row=r + 1, col=c + 1, rows=rows, cols=cols,
            text=page_data["text"] or "(пусто)",
        )
        out = call_fn([tile_png], prompt, 32768)
        # Cap runaway tile output so merge doesn't get poisoned (see ADR-028).
        if not out.startswith("[Error") and len(out) > MAX_TILE_CHARS:
            print(f"    Tile r{r+1}c{c+1}: runaway {len(out)} chars → truncated to {MAX_TILE_CHARS}", file=sys.stderr)
            out = out[:MAX_TILE_CHARS] + "\n[...truncated runaway output...]"
        # Detect-and-rerun: a bad tile (error/empty/illegible-flood/repetition)
        # gets a budget-capped escalated re-read; no-op on healthy tiles (ADR-039).
        if TILE_RERUN_ENABLED:
            out = _remediate_tile(r, c, out, clip, call_fn, prompt, pdf_path,
                                  page_data["page_num"], page_data["dpi"], rerun_budget)
        return (r, c, out)

    # Parallel calls — preserve tile order in the merged output.
    tile_outputs = [None] * len(rendered)
    with concurrent.futures.ThreadPoolExecutor(max_workers=TILE_CONCURRENCY) as ex:
        futures = {ex.submit(_process_tile, item): i for i, item in enumerate(rendered)}
        for fut in concurrent.futures.as_completed(futures):
            i = futures[fut]
            r, c, out = fut.result()
            tile_outputs[i] = f"### Фрагмент {r+1},{c+1}\n{out}"

    # Hierarchical merge — bounds each merge request under MERGE_MAX_INPUT_CHARS
    # so pages with many tiles don't 413 on a single all-tiles merge call.
    # --merge-provider routes the stitching to a separate (e.g. text/reasoning)
    # model; tiles stay on this page's vision call_fn. See ADR-037.
    merge_fn = MERGE_CALL_FN or call_fn
    merge_vision = MERGE_VISION if MERGE_CALL_FN is not None else True
    return _merge_tile_outputs(merge_fn, tile_outputs, len(tiles), full_page_for_merge,
                               merge_vision=merge_vision)


def verification_pass(call_fn, page_data, initial_md):
    """Second pass: re-read image, fix anything missing/wrong."""
    prompt = PROMPT_VERIFY.format(current_md=initial_md)
    return call_fn([page_data["png"]], prompt)


_REPEAT_SPACE_RE = re.compile(r" {10,}")
_REPEAT_DASH_RE = re.compile(r"-{30,}")
_LONG_LINE_THRESHOLD = 5000
_DUPLICATE_HEADER_THRESHOLD = 3
_HEADER_RE = re.compile(r"^(#{2,}\s+.+?)\s*$", re.MULTILINE)


# Latin→Cyrillic homoglyphs: ONLY glyphs visually identical across the two
# alphabets. Uppercase set mirrors sverka's canon_shifr (_HOMO); lowercase limited
# to the truly-identical pairs. Used by deromanize_mixed_script.
_LAT2CYR_HOMO = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
}
_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё./\-]+")
_HAS_CYR_RE = re.compile(r"[А-Яа-яЁё]")
_HAS_LAT_RE = re.compile(r"[A-Za-z]")


def deromanize_mixed_script(text):
    """Conservative Cyrillic repair: in any token that ALREADY mixes Cyrillic and
    Latin letters (e.g. a VLM emitted «ХCA» — Cyrillic Х + Latin C,A — for «ХСА»),
    fold the Latin HOMOGLYPHS to their Cyrillic twins. Pure-Latin tokens (genuine
    Latin marks like «T1.1», equipment names) are LEFT UNTOUCHED — only a mixed
    token is unambiguous evidence of a homoglyph slip, so this can't corrupt a
    legitimately-Latin code. Non-homoglyph Latin letters inside a mixed token stay
    as-is too. See ADR-036; user chose the conservative variant 2026-06-23."""
    def _fix(m):
        tok = m.group(0)
        if not _HAS_CYR_RE.search(tok) or not _HAS_LAT_RE.search(tok):
            return tok  # pure-Latin or pure-Cyrillic → nothing to disambiguate
        return "".join(_LAT2CYR_HOMO.get(ch, ch) for ch in tok)
    return _TOKEN_RE.sub(_fix, text)


def sanitize_repetition_loops(text):
    """Collapse pathological LLM repetition runs that bloat output without adding
    information. Two patterns observed in Gemini Flash on dense drawing pages:

    - Padding pressed inside a table cell can balloon to 100k+ spaces on one line
      (model keeps emitting space tokens until MAX_TOKENS). Threshold 10 — normal
      markdown never indents that deep.
    - One column of a table-separator row can fill with 100k+ dashes. Threshold 30
      — normal separators stay well under that.

    Bytes saved go from MBs to KBs on affected pages; legitimate content is
    untouched because thresholds are well above what well-formed markdown emits."""
    text = _REPEAT_SPACE_RE.sub(" ", text)
    text = _REPEAT_DASH_RE.sub("-----", text)
    return text


def tile_failure_signature(text):
    """Deterministic failure-signature classifier for ONE tile's output. Returns
    a signature string ('error' | 'empty' | 'repetition' | 'illegible_flood') or
    None if the tile looks healthy. Pure function — no I/O, no model. GENERIC: it
    keys off structural fingerprints, never document content, so it works on any
    unseen drawing. See ADR-039.

    Ordering matters: hard failures (error/empty) first, then degeneration
    (repetition), then under-resolution (illegible flood) last so an honest "few
    illegible marks" tile is not misread as a failure."""
    if text is None:
        return "empty"
    stripped = text.strip()
    if stripped.startswith("[Error"):
        return "error"
    if len(stripped) < TILE_MIN_CHARS:
        return "empty"
    # Repetition loop: the sanitizer collapses pathological space/dash runs; if it
    # removes a large fraction, the tile degenerated into a repeat-loop.
    if len(sanitize_repetition_loops(stripped)) < TILE_REPETITION_SHRINK * len(stripped):
        return "repetition"
    # Illegible flood: the prompt tells the model to write "(не читается)" for marks
    # it genuinely can't read — a FEW are honest. Many (both an absolute count AND a
    # high share of the tile's non-blank lines) means the tile is under-resolved
    # (too few pixels for the text), not honestly-illegible content.
    illegible = stripped.count(TILE_ILLEGIBLE_TOKEN)
    if illegible >= TILE_ILLEGIBLE_STRONG:
        return "illegible_flood"  # many markers → systematic under-read (any dilution)
    if illegible >= TILE_ILLEGIBLE_FLOOD:
        n_lines = sum(1 for ln in stripped.splitlines() if ln.strip())
        if n_lines and illegible >= TILE_ILLEGIBLE_SHARE * n_lines:
            return "illegible_flood"
    return None


def tile_quality_score(text):
    """Higher = better. Used to keep the better of {original, re-run} so a
    remediation can NEVER make a tile worse. Generic, content-free: rewards
    extracted content length (after collapsing repetition loops so they don't
    inflate the score) and heavily penalizes illegible markers. An error/None
    scores -inf so any real output beats it."""
    if text is None:
        return float("-inf")
    if text.strip().startswith("[Error"):
        return float("-inf")
    s = sanitize_repetition_loops(text.strip())
    return len(s) - 200 * s.count(TILE_ILLEGIBLE_TOKEN)


def _rerender_tile_hi(pdf_path, page_num, clip, base_dpi):
    """Re-render a single tile's clip at a higher DPI so small text gets more
    pixels for a re-read. DPI is scaled so the tile's longest side reaches
    RERUN_TILE_MAX_PIXEL. Returns PNG bytes, or None if the DPI lever is already
    exhausted (tile already at/above the re-run ceiling) or rendering failed.
    Opens its own fitz doc — fitz documents are not safe to share across the
    tile-worker threads. See ADR-039."""
    longest_pt = max(clip.width, clip.height)
    if longest_pt <= 0:
        return None
    hi_dpi = int(RERUN_TILE_MAX_PIXEL / (longest_pt / 72.0))
    if hi_dpi <= base_dpi:
        return None  # already at/above the re-run ceiling — no headroom
    try:
        doc = fitz.open(pdf_path)
        png = render_tile(doc[page_num - 1], clip, hi_dpi)
        doc.close()
        return png
    except Exception as e:  # pragma: no cover - defensive
        print(f"    tile-rerun: re-render failed: {e}", file=sys.stderr)
        return None


def _remediate_tile(r, c, out, clip, call_fn, prompt, pdf_path, page_num,
                    base_dpi, budget):
    """If a tile's output shows a failure signature, attempt a budget-capped
    escalated re-run (higher-DPI re-render of the same clip) and keep whichever
    output scores better. Deterministic policy, thread-safe budget, logged.
    Returns the (possibly improved) output text — never worse than the input.
    See ADR-039."""
    sig = tile_failure_signature(out)
    if not sig:
        return out
    if sig not in TILE_RERUN_SIGNATURES:
        # Soft signature (empty/illegible_flood): detected but NOT remediated —
        # re-running these regresses the merge (ADR-039). Left for the merge to
        # handle with the full-page image.
        return out
    tag = f"r{r+1}c{c+1}"
    with budget["lock"]:
        if budget["used"] >= budget["max"]:
            print(f"    tile-rerun {tag}: {sig}, budget exhausted "
                  f"({budget['used']}/{budget['max']}) — keeping original", file=sys.stderr)
            return out
        budget["used"] += 1
        attempt_no = budget["used"]
    hi_png = _rerender_tile_hi(pdf_path, page_num, clip, base_dpi)
    if hi_png is None:
        print(f"    tile-rerun {tag}: {sig}, DPI lever exhausted — keeping original",
              file=sys.stderr)
        return out
    print(f"    tile-rerun {tag}: {sig} → hi-DPI re-read ({attempt_no}/{budget['max']})",
          file=sys.stderr)
    rerun = call_fn([hi_png], prompt, 32768)
    if not rerun.startswith("[Error") and len(rerun) > MAX_TILE_CHARS:
        rerun = rerun[:MAX_TILE_CHARS] + "\n[...truncated runaway output...]"
    before, after = tile_quality_score(out), tile_quality_score(rerun)
    if after > before:
        print(f"    tile-rerun {tag}: re-run kept (score {before:.0f}→{after:.0f})",
              file=sys.stderr)
        return rerun
    print(f"    tile-rerun {tag}: re-run not better ({before:.0f}≥{after:.0f}) — "
          f"keeping original", file=sys.stderr)
    return out


def detect_hallucination_cascade(text):
    """Return a warning string if the page text shows fingerprints of the
    repetition-loop hallucination cascade observed on file 06 page 26
    (2026-05-28): Gemini Flash emits a long run of one char, loses context, then
    re-describes the same image 2-4 times, then drifts into invented content
    (e.g. tables from a different document).

    Detection signals (both survive sanitization, since hallucinated text itself
    is well-formed prose — only the trigger and the duplicate-section fingerprint
    remain after cleanup):

    1. **Any single line over 5000 chars** before sanitization — repetition runs
       always appear as a single huge line.
    2. **Any H2/H3 header repeated >=3 times** within one page — legitimate
       multi-image pages cap at 2, this catches the cascade.

    Returns "" if clean, otherwise a short reason string suitable to embed in
    the page output as `[Warning: ...]` for grep-based auditing alongside
    `[Error: ...]`."""
    for line in text.split("\n"):
        if len(line) > _LONG_LINE_THRESHOLD:
            return f"raw line of {len(line)} chars — model emitted a repetition run, hallucination cascade likely"
    headers = _HEADER_RE.findall(text)
    counts = {}
    for h in headers:
        counts[h] = counts.get(h, 0) + 1
    for h, c in counts.items():
        if c >= _DUPLICATE_HEADER_THRESHOLD:
            return f"section header '{h[:60]}' repeated {c}x — hallucination cascade likely"
    return ""


def _finalize_page(raw_text, extra_warnings=()):
    """Apply sanitization, then check for hallucination cascade signs and prepend
    a [Warning: ...] marker if found. Additional warnings (e.g. from OCR
    completeness check) are appended in the same prefix block. The marker keeps
    polluted content visible (so user can inspect) while making the page
    greppable for re-run."""
    warning = detect_hallucination_cascade(raw_text)
    cleaned = sanitize_repetition_loops(raw_text)
    if DEROMANIZE_MIXED:
        cleaned = deromanize_mixed_script(cleaned)
    warnings_out = []
    if warning:
        warnings_out.append(warning)
    warnings_out.extend(w for w in extra_warnings if w)
    if warnings_out:
        prefix = "\n".join(f"[Warning: {w}]" for w in warnings_out)
        return f"{prefix}\n\n{cleaned}"
    return cleaned


# OCR-completeness defaults. User-tunable via CLI.
# ASYMMETRIC by design: the strong signal is MD-missing-vs-OCR (model dropped
# a chunk of the page). The reverse case (MD has many more numbers than OCR)
# is the NORMAL state on technical drawings — Gemini reads small/rotated/
# stylized text Tesseract can't, so a 2-4x MD/OCR ratio is routine. We only
# flag MD>>OCR at extreme ratios (>5x) where invention becomes more likely
# than legitimate over-reading.
# Intersect tier is the orthogonal signal: catches the case where counts
# match but the actual numbers don't overlap (e.g. model emitted a different
# set of marks entirely — observed when Flash cascades into invented content).
OCR_CHECK_MIN_OCR_NUMBERS = 5  # below this, OCR is too sparse to trust as a check
OCR_HARD_COUNT_LOW = 0.5   # md/ocr < 0.5  → HARD FAIL (missed half+)
OCR_HARD_COUNT_HIGH = 5.0  # md/ocr > 5.0  → HARD FAIL (extreme over-reading, possible hallucination)
OCR_SOFT_COUNT_LOW = 0.7
OCR_SOFT_COUNT_HIGH = 3.0
OCR_HARD_INTERSECT = 0.4   # < 40% of OCR numbers present in MD → HARD FAIL
OCR_SOFT_INTERSECT = 0.6


def _md_numbers_normalized(md_text):
    """Reuse audit_ocr's number-token extraction + OCR-confusion folding on the
    Markdown text. Returns a set of normalized digit tokens (≥2 chars after
    normalization) — same shape as the OCR side, so set operations are valid."""
    out = set()
    for t in _ocr_extract_numbers(md_text):
        k = _ocr_normalize(t)
        if len(k) >= 2:
            out.add(k)
    return out


def _ocr_numbers_for_page(pdf_path, page_idx):
    """Render the PDF page and run Tesseract; return normalized number set.
    Returns None on OCR failure (so caller can skip the check rather than
    treat empty-output as a missing-content signal). Isolated tempdir per call
    so this is safe under --page-concurrency parallelism."""
    parent = os.path.dirname(os.path.abspath(pdf_path)) or "."
    try:
        with tempfile.TemporaryDirectory(prefix=".pdfmd_ocr_", dir=parent) as wd:
            ocr_text = _ocr_page_to_text(pdf_path, page_idx, wd)
    except Exception as e:
        print(f"    [ocr-check] OCR failed for p{page_idx+1}: {e}", flush=True)
        return None
    if not ocr_text or not ocr_text.strip():
        return None
    nums = set()
    for t in _ocr_extract_numbers(ocr_text):
        k = _ocr_normalize(t)
        if len(k) >= 2:
            nums.add(k)
    return nums


def check_ocr_completeness(pdf_path, page_idx, md_text, ocr_numbers=None,
                           hard_low=OCR_HARD_COUNT_LOW, hard_high=OCR_HARD_COUNT_HIGH,
                           soft_low=OCR_SOFT_COUNT_LOW, soft_high=OCR_SOFT_COUNT_HIGH,
                           hard_intersect=OCR_HARD_INTERSECT,
                           soft_intersect=OCR_SOFT_INTERSECT,
                           min_ocr=OCR_CHECK_MIN_OCR_NUMBERS):
    """Compare numeric tokens between an OCR pass of the page and the Gemini
    markdown. Returns dict {status: OK|SOFT_WARN|HARD_FAIL, reason: str,
    ocr_numbers: set|None}. ocr_numbers is returned so the caller can pass it
    back on a retry instead of re-running Tesseract.

    The check is asymmetric in spirit but symmetric in code: too few numbers
    in MD (vs OCR) means a chunk of the page was missed; too many means the
    model invented numbers. The intersection ratio is a separate signal — it
    catches the case where counts match but the sets are disjoint (e.g. model
    swapped one set of marks for another)."""
    if ocr_numbers is None:
        ocr_numbers = _ocr_numbers_for_page(pdf_path, page_idx)
    if ocr_numbers is None:
        return {"status": "OK", "reason": "OCR unavailable, check skipped", "ocr_numbers": None}
    if len(ocr_numbers) < min_ocr:
        return {"status": "OK",
                "reason": f"OCR found only {len(ocr_numbers)} numbers (<{min_ocr}), check skipped",
                "ocr_numbers": ocr_numbers}

    md_numbers = _md_numbers_normalized(md_text)
    count_ratio = len(md_numbers) / len(ocr_numbers)
    intersect = ocr_numbers & md_numbers
    intersect_pct = len(intersect) / len(ocr_numbers)
    info = (f"OCR={len(ocr_numbers)} MD={len(md_numbers)} "
            f"ratio={count_ratio:.2f} intersect={intersect_pct:.2f}")

    # HARD FAIL — likely the model dropped half the page OR invented numbers.
    if count_ratio < hard_low:
        return {"status": "HARD_FAIL",
                "reason": f"MD has <{int(hard_low*100)}% of OCR numbers (missed content) — {info}",
                "ocr_numbers": ocr_numbers}
    if count_ratio > hard_high:
        return {"status": "HARD_FAIL",
                "reason": f"MD has >{int(hard_high*100)}% of OCR numbers (possible hallucination) — {info}",
                "ocr_numbers": ocr_numbers}
    if intersect_pct < hard_intersect:
        return {"status": "HARD_FAIL",
                "reason": f"only {int(intersect_pct*100)}% of OCR numbers found in MD (disjoint sets) — {info}",
                "ocr_numbers": ocr_numbers}

    # SOFT WARN — flag but don't retry.
    if count_ratio < soft_low or count_ratio > soft_high or intersect_pct < soft_intersect:
        return {"status": "SOFT_WARN",
                "reason": f"OCR completeness borderline — {info}",
                "ocr_numbers": ocr_numbers}

    return {"status": "OK", "reason": info, "ocr_numbers": ocr_numbers}


def _extract(call_fn, page_data, pdf_path):
    if page_data["needs_tiling"]:
        return extract_page_tiled(call_fn, page_data, pdf_path)
    return extract_page_full(call_fn, page_data)


def process_page(call_fn, page_data, pdf_path, verify=True, provider="gemini-pro",
                 fallback_call_fn=None, fallback_provider=None,
                 ocr_check=True):
    """Extract one page to markdown. Auto-mitigation:
    - If raw extraction shows hallucination cascade (long-run trigger or
      duplicated section headers) and `fallback_call_fn` is provided, retry
      the extraction once with the fallback. Cascade is a known Flash failure
      mode on dense drawings; Pro doesn't share the same trigger.
    - After verify, run OCR completeness check (Tesseract on the rendered page).
      If MD's numeric tokens diverge from OCR's by more than the HARD thresholds,
      trigger the same Pro fallback once; if the fallback also fails, embed a
      [Warning: ...] marker. SOFT divergences only get the marker, no retry.
    - If a retry produces another cascade or error, accept the first attempt
      with a [Warning: ...] marker for grep audit.
    - `[Error: ...]` results pass through unchanged (provider already retried
      transient errors internally; no point retrying here)."""
    page_num = page_data.get("page_num", "?")
    page_idx = page_num - 1 if isinstance(page_num, int) else None
    initial = _extract(call_fn, page_data, pdf_path)
    used_fallback = False

    # Defense 1: hallucination-cascade fingerprint (repetition runs, dup headers).
    if fallback_call_fn is not None and not initial.startswith("[Error"):
        cascade = detect_hallucination_cascade(initial)
        if cascade:
            print(f"    [auto-retry p{page_num}] cascade on {provider}: {cascade[:80]}", flush=True)
            retry = _extract(fallback_call_fn, page_data, pdf_path)
            retry_cascade = detect_hallucination_cascade(retry)
            if not retry.startswith("[Error") and not retry_cascade:
                print(f"    [auto-retry p{page_num}] resolved on {fallback_provider}", flush=True)
                initial = retry
                used_fallback = True
            else:
                print(f"    [auto-retry p{page_num}] fallback also bad ({retry_cascade or 'error'}), keeping primary with warning", flush=True)

    # Verify pass (skipped if we already swapped in Pro fallback — that output is the strongest signal).
    if verify and not used_fallback and len(initial.strip()) >= 50 and not initial.startswith("[Error"):
        verified = verification_pass(call_fn, page_data, initial)
        min_ratio = VERIFY_MIN_RATIO_BY_PROVIDER.get(provider, VERIFY_MIN_RATIO_DEFAULT)
        if not verified.startswith("[Error") and len(verified.strip()) >= len(initial.strip()) * min_ratio:
            initial = verified

    # Defense 2.5: <TABLE> JSON-block parse — primary protection against the
    # markdown-table-padding loop class (long dash runs in `|---|...|` and
    # silent empty `| | | |` rows). Catches malformed tables before they hit
    # the renderer. Same fallback pattern as cascade + OCR check: try Pro once
    # if available and not already used, else just emit a marker.
    json_warning = None
    if _RENDER_TABLES_AVAILABLE and not initial.startswith("[Error"):
        table_issues = _detect_table_issues(initial)
        if table_issues and fallback_call_fn is not None and not used_fallback:
            summary = "; ".join(f"{tid}: {reason}" for tid, reason in table_issues[:3])
            more = f" (+{len(table_issues)-3} more)" if len(table_issues) > 3 else ""
            print(f"    [json-retry p{page_num}] table issues on {provider}: {summary}{more}", flush=True)
            retry = _extract(fallback_call_fn, page_data, pdf_path)
            if not retry.startswith("[Error"):
                retry_issues = _detect_table_issues(retry)
                if len(retry_issues) < len(table_issues):
                    print(f"    [json-retry p{page_num}] resolved/improved on {fallback_provider} ({len(table_issues)}->{len(retry_issues)} issues)", flush=True)
                    initial = retry
                    used_fallback = True
                    table_issues = retry_issues
                else:
                    print(f"    [json-retry p{page_num}] fallback also has {len(retry_issues)} issue(s), keeping primary with warning", flush=True)
            else:
                print(f"    [json-retry p{page_num}] fallback errored, keeping primary with warning", flush=True)
        if table_issues:
            preview = "; ".join(f"{tid}: {reason}" for tid, reason in table_issues[:5])
            more = f" (+{len(table_issues)-5} more)" if len(table_issues) > 5 else ""
            json_warning = f"TABLE json parse: {preview}{more}"

    # Defense 2: OCR completeness check — independent of cascade fingerprint,
    # catches the case where Gemini silently dropped a chunk of the page (no
    # repetition pathology, output looks fine, but half the numbers are missing).
    ocr_warning = None
    if (ocr_check and _OCR_AVAILABLE and page_idx is not None
            and len(initial.strip()) >= 50 and not initial.startswith("[Error")):
        ocr_result = check_ocr_completeness(pdf_path, page_idx, initial)
        if ocr_result["status"] == "HARD_FAIL":
            if fallback_call_fn is not None and not used_fallback:
                print(f"    [ocr-retry p{page_num}] {ocr_result['reason']}", flush=True)
                retry = _extract(fallback_call_fn, page_data, pdf_path)
                if not retry.startswith("[Error"):
                    retry_check = check_ocr_completeness(pdf_path, page_idx, retry,
                                                         ocr_numbers=ocr_result["ocr_numbers"])
                    if retry_check["status"] != "HARD_FAIL":
                        print(f"    [ocr-retry p{page_num}] resolved on {fallback_provider} ({retry_check['reason']})", flush=True)
                        initial = retry
                        used_fallback = True
                        ocr_warning = None if retry_check["status"] == "OK" else f"OCR check (post-fallback): {retry_check['reason']}"
                    else:
                        print(f"    [ocr-retry p{page_num}] fallback also failed OCR check ({retry_check['reason']}), keeping primary with warning", flush=True)
                        ocr_warning = f"OCR check: {ocr_result['reason']}"
                else:
                    print(f"    [ocr-retry p{page_num}] fallback errored, keeping primary with warning", flush=True)
                    ocr_warning = f"OCR check: {ocr_result['reason']}"
            else:
                ocr_warning = f"OCR check: {ocr_result['reason']}"
        elif ocr_result["status"] == "SOFT_WARN":
            ocr_warning = f"OCR check: {ocr_result['reason']}"

    finalized = _finalize_page(initial, extra_warnings=[json_warning, ocr_warning])

    # Compensation layer 1: OCR contributor — append the dense codes (штамп
    # doc-code, equipment marks) the VLM dropped. Only codes ABSENT from the page
    # are added; never overwrites model content. Skips error pages. ADR-042.
    if (OCR_CONTRIBUTE and _OCR_CONTRIB_AVAILABLE and page_idx is not None
            and not finalized.lstrip().startswith("[Error")):
        try:
            res = _ocr_recover_codes(pdf=pdf_path, page_idx=page_idx, existing_md=finalized)
            block = _ocr_make_block(res["missing"])
            if block:
                print(f"    [ocr-contrib p{page_num}] +{len(res['missing'])} codes", flush=True)
                finalized = finalized.rstrip() + "\n" + block + "\n"
        except Exception as e:
            print(f"    [ocr-contrib p{page_num}] skipped: {e}", flush=True)

    # Compensation layer 2: synthesis (ADR-043). DRAWING pages only (unless
    # --synthesize-all): fuse the draft with tiled-Tesseract text via a strong text
    # model. Guarded inside synthesize_page (returns the draft on any failure /
    # catastrophic shortening).
    is_drawing = not page_data.get("is_text_rich", False)
    if (SYNTH_CALL_FN is not None and _OCR_CONTRIB_AVAILABLE and page_idx is not None
            and (is_drawing or SYNTH_ALL) and not finalized.lstrip().startswith("[Error")
            and len(finalized.strip()) >= 50):
        try:
            synth_fn = lambda p: SYNTH_CALL_FN([], p, 8000)  # text-only call
            synthed = _ocr_synthesize_page(finalized, synth_fn, pdf=pdf_path, page_idx=page_idx)
            if synthed and synthed != finalized:
                print(f"    [synth p{page_num}] {len(finalized)}→{len(synthed)} chars", flush=True)
                finalized = synthed
        except Exception as e:
            print(f"    [synth p{page_num}] skipped: {e}", flush=True)

    return finalized


def find_completed_pages(output_path):
    """Parse an existing markdown file for `## Страница N` headers — these are
    pages we've already extracted and can skip on resume."""
    if not os.path.exists(output_path):
        return set()
    with open(output_path, encoding="utf-8") as f:
        content = f.read()
    return {int(m) for m in re.findall(r"^## Страница (\d+)", content, re.MULTILINE)}


def main():
    global DPI_DRAWING, TILE_MAX_PIXEL, TILE_OVERLAP_PCT, FREQUENCY_PENALTY
    global MERGE_MAX_INPUT_CHARS, MERGE_MAX_PIXEL, DEROMANIZE_MIXED
    global MERGE_CALL_FN, MERGE_VISION, TILE_RERUN_ENABLED, RASTER_NATIVE_DPI_CAP, OCR_CONTRIBUTE
    global SYNTH_CALL_FN, SYNTH_ALL
    global SYSTEM_PROMPT, PROMPT_TEXT_RICH, PROMPT_DRAWING, PROMPT_TILE
    global PROMPT_TILE_MERGE, PROMPT_TILE_MERGE_PARTIAL, PROMPT_TILE_MERGE_TEXT, PROMPT_VERIFY
    parser = argparse.ArgumentParser(description="Convert PDF to Markdown via PyMuPDF + Gemini")
    parser.add_argument("pdf", help="Path to PDF file")
    parser.add_argument("-o", "--output", help="Output markdown file (default: <pdf_name>.md)")
    parser.add_argument("--pages", help="Page range, e.g. '1-10' or '3,5,7-9'")
    parser.add_argument("--no-verify", action="store_true", help="Skip verification pass (faster, less accurate)")
    parser.add_argument("--no-tile", action="store_true", help="Disable auto-tiling for large drawings")
    parser.add_argument("--no-resume", action="store_true", help="Ignore any existing output file and start fresh (default: resume by skipping already-extracted pages)")
    parser.add_argument("--no-auto-fallback", action="store_true", help="Disable auto-fallback to gemini-pro on hallucination cascades (default: enabled when primary != gemini-pro)")
    parser.add_argument("--no-ocr-check", action="store_true",
                        help="Disable OCR (Tesseract) completeness check that compares numeric tokens between OCR and Gemini output and triggers gemini-pro fallback when MD is missing >50%% of OCR's numbers (default: enabled if pytesseract/tesseract available)")
    parser.add_argument("--provider", default="gemini-pro",
                        choices=sorted(PROVIDERS.keys()),
                        help="Vision provider (default: gemini-pro)")
    parser.add_argument("--drawing-dpi", type=int, default=None,
                        help=f"Override DPI_DRAWING (default {DPI_DRAWING}). "
                             f"Lower = fewer tiles but may lose fine label detail.")
    parser.add_argument("--tile-max-pixel", type=int, default=None,
                        help=f"Override TILE_MAX_PIXEL (default {TILE_MAX_PIXEL}). "
                             f"Larger = fewer tiles but tiles may exceed API downscale limit (~3072px).")
    parser.add_argument("--tile-overlap", type=float, default=None,
                        help=f"Override TILE_OVERLAP_PCT (default {TILE_OVERLAP_PCT}).")
    parser.add_argument("--merge-max-chars", type=int, default=None,
                        help=f"Override MERGE_MAX_INPUT_CHARS (default {MERGE_MAX_INPUT_CHARS}). "
                             f"Tile-outputs text above this triggers hierarchical (batched) "
                             f"merge to avoid provider 413 on many-tile pages. Lower if a "
                             f"provider still 413s on the merge call.")
    parser.add_argument("--merge-max-pixel", type=int, default=None,
                        help=f"Override MERGE_MAX_PIXEL (default {MERGE_MAX_PIXEL}). "
                             f"Cap on the full-page merge image's longest side; lower if "
                             f"the merge image itself trips a request-size limit.")
    parser.add_argument("--page-concurrency", type=int, default=1,
                        help="Process N pages in parallel (default 1 = sequential). "
                             "Pages are independent — each spawns its own tile workers. "
                             "Safe N depends on provider's RPM/TPM tier (Gemini Flash Tier 1: 300 RPM, 2M TPM → N=4-8 fine).")
    parser.add_argument("--no-highdpi-repair", action="store_true",
                        help="Disable Defense 4: after the main run, pages still tagged "
                             "[Warning: OCR check: ...] are auto-re-extracted on 600 DPI "
                             "+ gemini-pro (subprocess to tools/PDF_в_маркдаун/repair_pages_highdpi.py, "
                             "in-place with a .pre-highdpi.bak). No-op when --no-ocr-check or no warnings remain.")
    parser.add_argument("--highdpi-repair-dpi", type=int, default=450,
                        help="DPI for Defense 4 re-extract (default: 450). "
                             "Bumped down from 600 after a 10+ hour hang on one page "
                             "during the КР2 run — 450 still resolves rotated/small text "
                             "but produces smaller payloads less prone to MAX_TOKENS storms.")
    parser.add_argument("--no-table-render", action="store_true",
                        help="Disable final-stage rendering of <TABLE> JSON blocks "
                             "into GFM markdown tables. With this flag the file is "
                             "left containing raw <TABLE id=...>{json}</TABLE> blocks "
                             "(useful for debugging the model's JSON output).")
    parser.add_argument("--ocr-contribute", action="store_true",
                        help="Compensation layer 1: after each page, run Tesseract "
                             "(full page + bottom-right штамп corner crop) and append "
                             "the dense CODES the VLM dropped (doc-code/штамп, equipment "
                             "marks) that are absent from the page. The штамп doc_code is "
                             "missed by every vision model on every page — a fixed-corner "
                             "OCR job, not a capacity one. On the hard test set this "
                             "recovers doc_code on 3/6 pages (+3-4 checklist items). "
                             "Never overwrites model content. See ADR-042.")
    parser.add_argument("--synthesize", default=None, choices=sorted(PROVIDERS.keys()),
                        help="Compensation layer 2: after each DRAWING page, fuse its draft "
                             "with a tiled-Tesseract OCR pass via this (text) PROVIDER — keep "
                             "the VLM structure, fold in the dense content the VLM dropped. "
                             "Lifts the worst drawings hugely (ОДИ 12.5→62.5%%, ПБ 43.8→56.2%% "
                             "on the hard set); local qwen3-32b-text matched the cloud ceiling. "
                             "DRAWINGS ONLY (dense tables regress under reformatting); most "
                             "real-doc tables are A4 text-rich and auto-skip. Pair with "
                             "--ocr-contribute. See ADR-043.")
    parser.add_argument("--synthesize-all", action="store_true",
                        help="Apply --synthesize to ALL pages, not just drawings (overrides the "
                             "drawing-only gate). Risky on dense tables — they can lose rows.")
    parser.add_argument("--data-first", action="store_true",
                        help="Use the data-first/plain-text prompt set (CSV tables, "
                             "line-per-fact, no Markdown/<TABLE>-JSON) instead of the "
                             "default Markdown prompts. Tuned for small local VLMs "
                             "(qwen3-vl-8b): on blind tests vs ground truth it lifted "
                             "table 88->93%% (recovered sub-blocks+totals) and drawing "
                             "65->85%% (room marks 0/7->7/7, fewer hallucinations). "
                             "Emits no <TABLE> blocks, so pair with --no-table-render. "
                             "Gemini path unaffected unless you pass this flag.")
    parser.add_argument("--deromanize-mixed", action="store_true",
                        help="Post-fix: in mixed-script tokens (a word containing BOTH "
                             "Cyrillic and Latin letters, e.g. «ХCA» for «ХСА»), fold the "
                             "Latin homoglyphs (A B C E H K M O P T X Y / a c e o p x y) to "
                             "Cyrillic. Conservative — pure-Latin tokens (T1.1, equipment "
                             "names) are left untouched, so it can't corrupt a genuine Latin "
                             "code. Off by default; the qwen wrapper enables it (ADR-036).")
    parser.add_argument("--freq-penalty", type=float, default=0.0,
                        help="frequency_penalty for openai-compat providers (qwen/glm). "
                             "Default 0.0. Set ~0.4 to kill repetition-loop degeneration "
                             "on dense drawings (cut +10C loop 89->3 reps in testing). "
                             "No effect on the gemini backend.")
    parser.add_argument("--cap-native-dpi", action="store_true",
                        help="Cap render DPI on raster-dominated pages (a page that is "
                             "essentially one big embedded image) so the page is never "
                             "rendered above the image's NATIVE pixel resolution. "
                             "Upscaling a scan/image page adds no detail but multiplies "
                             "tiles + merge variance (a 3509px table at 500 DPI → 63 tiles). "
                             "Vector pages are untouched. Off by default (ADR-040).")
    parser.add_argument("--tile-rerun", action="store_true",
                        help="Tile detect-and-rerun: when a single tile's output shows a "
                             "failure signature (model error, empty, a flood of "
                             "«(не читается)», or a repetition loop), re-run THAT tile at "
                             "higher DPI and keep whichever output scores better (a re-run "
                             "can never regress a tile). Deterministic signature→action "
                             "policy, budget-capped per page. No-op on clean pages. Off by "
                             "default; the qwen wrapper enables it (ADR-039).")
    parser.add_argument("--merge-provider", default=None,
                        choices=sorted(PROVIDERS.keys()),
                        help="Use a SEPARATE model for the tile-merge (stitching) step. "
                             "Tiles stay on --provider (vision); only the merge calls go "
                             "here. Stitching is a language task (reorganize the text the "
                             "vision model already produced), so a strong text/reasoning "
                             "model — e.g. qwen3-32b-text (dense, local-deployable) or "
                             "claude-sonnet (extended thinking) — does it better than the "
                             "small VLM. Text-only merge models get no image. Default: merge "
                             "uses --provider (current behaviour). See ADR-037.")
    args = parser.parse_args()

    # Apply tile-geometry overrides BEFORE anything reads the constants.
    if args.drawing_dpi:
        DPI_DRAWING = args.drawing_dpi
        print(f"override: DPI_DRAWING = {DPI_DRAWING}")
    if args.tile_max_pixel:
        TILE_MAX_PIXEL = args.tile_max_pixel
        print(f"override: TILE_MAX_PIXEL = {TILE_MAX_PIXEL}")
    if args.tile_overlap is not None:
        TILE_OVERLAP_PCT = args.tile_overlap
        print(f"override: TILE_OVERLAP_PCT = {TILE_OVERLAP_PCT}")
    if args.merge_max_chars:
        MERGE_MAX_INPUT_CHARS = args.merge_max_chars
        print(f"override: MERGE_MAX_INPUT_CHARS = {MERGE_MAX_INPUT_CHARS}")
    if args.merge_max_pixel:
        MERGE_MAX_PIXEL = args.merge_max_pixel
        print(f"override: MERGE_MAX_PIXEL = {MERGE_MAX_PIXEL}")
    if args.ocr_contribute:
        if not _OCR_CONTRIB_AVAILABLE:
            print("WARNING: --ocr-contribute requested but ocr_contributor import failed; skipping")
        else:
            OCR_CONTRIBUTE = True
            print("override: OCR_CONTRIBUTE = True (Tesseract штамп/code recovery appended per page)")
    if args.synthesize:
        if not _OCR_CONTRIB_AVAILABLE:
            print("WARNING: --synthesize requested but ocr_contributor import failed; skipping")
        else:
            SYNTH_CALL_FN = make_provider_call_fn(args.synthesize)
            SYNTH_ALL = args.synthesize_all
            scope = "ALL pages" if SYNTH_ALL else "drawing pages only"
            print(f"override: SYNTHESIZE = {args.synthesize} ({scope}); fuse draft + tiled OCR")
    if args.data_first:
        SYSTEM_PROMPT = DATA_FIRST_SYSTEM_PROMPT
        PROMPT_TEXT_RICH = DATA_FIRST_PROMPT_TEXT_RICH
        PROMPT_DRAWING = DATA_FIRST_PROMPT_DRAWING
        PROMPT_TILE = DATA_FIRST_PROMPT_TILE
        PROMPT_TILE_MERGE = DATA_FIRST_PROMPT_TILE_MERGE
        PROMPT_TILE_MERGE_PARTIAL = DATA_FIRST_PROMPT_TILE_MERGE_PARTIAL
        PROMPT_TILE_MERGE_TEXT = DATA_FIRST_PROMPT_TILE_MERGE_TEXT
        PROMPT_VERIFY = DATA_FIRST_PROMPT_VERIFY
        print("override: data-first prompt set (plain text / CSV tables, no <TABLE> JSON)")
    if args.freq_penalty:
        FREQUENCY_PENALTY = args.freq_penalty
        print(f"override: frequency_penalty = {FREQUENCY_PENALTY}")
    if args.deromanize_mixed:
        DEROMANIZE_MIXED = True
        print("override: deromanize mixed-script tokens (Latin homoglyphs → Cyrillic)")
    if args.tile_rerun:
        TILE_RERUN_ENABLED = True
        print(f"override: tile detect-and-rerun ON (budget {TILE_RERUN_BUDGET}/page, "
              f"hi-DPI re-render ≤{RERUN_TILE_MAX_PIXEL}px)")
    if args.cap_native_dpi:
        RASTER_NATIVE_DPI_CAP = True
        print("override: cap render DPI to native resolution on raster-dominated pages")
    if args.merge_provider:
        MERGE_CALL_FN = make_provider_call_fn(args.merge_provider)
        MERGE_VISION = PROVIDERS[args.merge_provider].get("vision", True)
        print(f"override: merge-provider = {args.merge_provider} "
              f"(vision={MERGE_VISION}); tiles stay on {args.provider}")

    if not os.path.exists(args.pdf):
        print(f"File not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)

    pdf_name = os.path.splitext(os.path.basename(args.pdf))[0]
    output_path = args.output or f"{pdf_name}.md"
    progress_path = output_path + ".progress.txt"

    doc = fitz.open(args.pdf)
    total_pages = len(doc)
    doc.close()

    requested_indices = parse_page_range(args.pages, total_pages)

    # Resume: skip pages already present in the output file.
    completed = set() if args.no_resume else find_completed_pages(output_path)
    if completed:
        page_indices = [idx for idx in requested_indices if (idx + 1) not in completed]
        print(f"Resume: {len(completed)} pages already in {output_path}, processing {len(page_indices)} remaining")
    else:
        page_indices = requested_indices
        print(f"{pdf_name}: {total_pages} pages total, processing {len(page_indices)}")

    if not page_indices:
        print("Nothing to process. Use --no-resume to redo from scratch.")
        return

    print("Extracting text and rendering pages...")
    page_data = extract_text_and_images(args.pdf, page_indices)

    if args.no_tile:
        for p in page_data:
            p["needs_tiling"] = False

    for p in page_data:
        mode = "TEXT" if p["is_text_rich"] else "DRAWING"
        tile = " +TILE" if p["needs_tiling"] else ""
        print(f"  Page {p['page_num']}: {mode}{tile} ({len(p['text'])} chars, {p['dpi']} DPI, {p['width']}x{p['height']}px)")

    try:
        call_fn = make_provider_call_fn(args.provider)
    except (RuntimeError, ValueError) as e:
        print(f"Provider init failed: {e}", file=sys.stderr)
        sys.exit(2)

    # Auto-fallback to gemini-pro on hallucination cascades. Pro doesn't share
    # the dense-drawing repetition-loop trigger that Flash hits, so it's the
    # natural rescue for cascade pages. Skip if the primary already IS pro, or
    # if Pro env is missing (best-effort, don't fail the whole run).
    fallback_call_fn = None
    fallback_provider = None
    if args.provider != "gemini-pro" and not args.no_auto_fallback:
        try:
            fallback_call_fn = make_provider_call_fn("gemini-pro")
            fallback_provider = "gemini-pro"
            print(f"  auto-fallback: gemini-pro will retry any page where {args.provider} hits a hallucination cascade")
        except (RuntimeError, ValueError) as e:
            print(f"  auto-fallback disabled ({e}); cascade pages will get [Warning: ...] markers only", file=sys.stderr)

    verify = not args.no_verify

    # OCR completeness check (defense layer 2). Disabled if the import failed
    # (audit_ocr or tesseract binary missing) or the user opted out.
    ocr_check = not args.no_ocr_check and _OCR_AVAILABLE
    if args.no_ocr_check:
        print("  ocr-check disabled by --no-ocr-check")
    elif not _OCR_AVAILABLE:
        print(f"  ocr-check disabled: audit_ocr import failed ({_OCR_IMPORT_ERR})", file=sys.stderr)
    else:
        print(f"  ocr-check: Tesseract will compare numeric tokens between OCR and MD; "
              f"HARD FAIL (ratio<{OCR_HARD_COUNT_LOW} or >{OCR_HARD_COUNT_HIGH} or intersect<{OCR_HARD_INTERSECT}) triggers gemini-pro fallback")

    model = PROVIDERS[args.provider]["model"]
    print(f"Processing {len(page_data)} pages via {args.provider} ({model}) "
          f"(verify={'on' if verify else 'off'}, timeout={GEMINI_CALL_TIMEOUT}s/call)...")

    # Initialize file with title if starting fresh; otherwise append to existing content.
    if not completed:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# {pdf_name} — {total_pages} pages\n")

    t0 = time.time()
    page_concurrency = max(1, args.page_concurrency)
    if page_concurrency > 1:
        print(f"  --page-concurrency={page_concurrency}: pages will be processed in parallel.")

    # Lock guards shared mutables: file appends + done_count + progress file.
    write_lock = threading.Lock()
    done_count = [0]
    total = len(page_data)

    def _process_one(idx_and_page):
        i, p = idx_and_page
        page_t0 = time.time()
        print(f"  [start {i+1}/{total}] Page {p['page_num']}...", flush=True)
        result = process_page(call_fn, p, args.pdf, verify=verify, provider=args.provider,
                              fallback_call_fn=fallback_call_fn, fallback_provider=fallback_provider,
                              ocr_check=ocr_check)
        dt = time.time() - page_t0
        with write_lock:
            with open(output_path, "a", encoding="utf-8") as f:
                f.write(f"\n## Страница {p['page_num']}\n\n{result}\n")
            done_count[0] += 1
            done = done_count[0]
            total_dt = time.time() - t0
            remaining = total - done
            if remaining:
                eta_s = (total_dt / done) * remaining
                print(f"    [done {done}/{total}] Page {p['page_num']} in {dt:.0f}s | total {total_dt/60:.1f}min | ETA {eta_s/60:.0f}min", flush=True)
                with open(progress_path, "w") as f:
                    f.write(f"{done}/{total} done, ETA {eta_s/60:.0f}min, last page {dt:.0f}s\n")
            else:
                print(f"    [done {done}/{total}] Page {p['page_num']} in {dt:.0f}s | total {total_dt/60:.1f}min", flush=True)

    if page_concurrency == 1:
        for item in enumerate(page_data):
            _process_one(item)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=page_concurrency) as ex:
            futs = [ex.submit(_process_one, item) for item in enumerate(page_data)]
            for fut in concurrent.futures.as_completed(futs):
                # Surface exceptions; _process_one shouldn't raise but guard anyway.
                fut.result()

    # Parallel writes land out-of-order in the file. Re-sort sections by page number
    # so the final markdown reads top-to-bottom in page order. Safe to run unconditionally:
    # for sequential runs sections are already in order, sort is a no-op.
    _sort_output_by_page(output_path)

    # Defense 4 picks up BOTH `[Warning: OCR check: ...]` (from Defense 2) and
    # `[Warning: TABLE json parse: ...]` (from Defense 2.5). So it should run
    # whenever either of those defenses is active — not just when ocr_check is on.
    defense4_eligible = (ocr_check or _RENDER_TABLES_AVAILABLE) and not args.no_highdpi_repair
    if defense4_eligible:
        _run_highdpi_repair_post(output_path, args.pdf, dpi=args.highdpi_repair_dpi)

    # Final stage: convert <TABLE> JSON blocks → real GFM markdown tables.
    # Must run AFTER Defense 4 so re-extracted pages get their fresh JSON
    # blocks rendered too. Backed up to .pre-render.bak.
    if _RENDER_TABLES_AVAILABLE and not args.no_table_render:
        _run_table_render_post(output_path)

    _strip_llm_markdown_fences(output_path)

    print(f"Done: {output_path}")


def _strip_llm_markdown_fences(output_path):
    """Some LLM passes wrap their whole page output in ```markdown ... ```.
    Markdown viewers then render the page as one giant code block, hiding
    tables and headings. Strip paired fences in a single pass."""
    if not os.path.exists(output_path):
        return
    with open(output_path, encoding="utf-8") as f:
        lines = f.read().splitlines(keepends=True)
    out = []
    in_fence = False
    stripped = 0
    for line in lines:
        s = line.rstrip("\r\n")
        if not in_fence and s == "```markdown":
            in_fence = True
            stripped += 1
            continue
        if in_fence and s == "```":
            in_fence = False
            stripped += 1
            continue
        out.append(line)
    if stripped:
        with open(output_path, "w", encoding="utf-8") as f:
            f.writelines(out)
        print(f"  Stripped {stripped} LLM ```markdown fence line(s)")


_DEFENSE4_WARN_RE = r"^\[Warning: (?:OCR check: |TABLE json parse: )"


def _run_highdpi_repair_post(output_path, pdf_path, dpi=600):
    """Defense 4: subprocess tools/PDF_в_маркдаун/repair_pages_highdpi.py to
    re-extract any page still flagged by [Warning: OCR check: ...] OR
    [Warning: TABLE json parse: ...] at higher DPI with gemini-pro. In-place
    edit with auto .pre-highdpi.bak. Logs an errors+warnings audit before and
    after.

    The repair script's own WARNING_RE matches both marker classes, so both
    Defense-2 (OCR) and Defense-2.5 (TABLE) failures flow through the same
    high-DPI repair path."""
    repair_script = Path(__file__).resolve().parent / "PDF_в_маркдаун" / "repair_pages_highdpi.py"
    if not repair_script.exists():
        print(f"  Defense 4 skipped: repair script not found at {repair_script}", file=sys.stderr)
        return
    if not os.path.exists(output_path):
        return
    with open(output_path, encoding="utf-8") as f:
        text = f.read()
    n_warn = len(re.findall(_DEFENSE4_WARN_RE, text, flags=re.MULTILINE))
    n_err = len(re.findall(r"^\[Error", text, flags=re.MULTILINE))
    print(f"\nFinal audit before Defense 4: errors={n_err}, warnings={n_warn}")
    if n_warn == 0:
        print("Defense 4: no OCR/TABLE warnings — high-DPI repair skipped.")
        return
    print(f"Defense 4: re-extracting {n_warn} warning page(s) on {dpi} DPI + gemini-pro (in-place)...")
    cmd = [
        sys.executable, str(repair_script),
        "--pdf", str(pdf_path),
        "--md", str(output_path),
        "--in-place",
        "--dpi", str(dpi),
    ]
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print(f"  Defense 4: repair script exited {rc} — final state may still contain warnings", file=sys.stderr)
    with open(output_path, encoding="utf-8") as f:
        text2 = f.read()
    n_warn2 = len(re.findall(_DEFENSE4_WARN_RE, text2, flags=re.MULTILINE))
    n_err2 = len(re.findall(r"^\[Error", text2, flags=re.MULTILINE))
    print(f"Final audit after Defense 4: errors={n_err2}, warnings={n_warn2} (was warnings={n_warn})")


def _run_table_render_post(output_path):
    """Final stage: replace every <TABLE id="..." title="...">{JSON}</TABLE> block
    in the output with a real GFM markdown table + Цветовая разметка block.

    Runs AFTER Defense 4 so any pages re-extracted at high DPI have their
    fresh JSON blocks rendered too. On unparseable JSON: leaves an inline
    `[Warning: TABLE json parse: ...]` marker — visible in the file, but at
    this stage it's terminal (no more repair passes). Defense 4 should have
    already had a chance to fix anything fixable.

    Always backs up to <output>.pre-render.bak before writing (cheap insurance
    given the repo has no git history)."""
    if not _RENDER_TABLES_AVAILABLE:
        print(f"  table-render skipped: render_tables import failed ({_RENDER_TABLES_IMPORT_ERR})", file=sys.stderr)
        return
    if not os.path.exists(output_path):
        return
    with open(output_path, encoding="utf-8") as f:
        text = f.read()
    new_text, n_replaced, n_failed = _render_tables_text(text)
    if n_replaced == 0:
        print("Table render: no <TABLE> blocks found — skipped.")
        return
    backup = output_path + ".pre-render.bak"
    try:
        with open(backup, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        print(f"  table-render backup failed ({e}); not writing changes", file=sys.stderr)
        return
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(new_text)
    print(f"Table render: {n_replaced} table(s) rendered, {n_failed} parse failure(s); backup at {backup}")


def _sort_output_by_page(output_path):
    """Read the output markdown, split into (header_block, page_sections), sort
    sections by their page number from `## Страница N`, rewrite. Idempotent."""
    if not os.path.exists(output_path):
        return
    with open(output_path, encoding="utf-8") as f:
        content = f.read()
    # Split: keep everything before the first `## Страница N` as preamble.
    parts = re.split(r"(?=^## Страница \d+)", content, flags=re.MULTILINE)
    if len(parts) <= 1:
        return
    preamble = parts[0]
    sections = parts[1:]
    def _pageno(sec):
        m = re.match(r"## Страница (\d+)", sec)
        return int(m.group(1)) if m else 10**9
    sections.sort(key=_pageno)
    rewritten = preamble + "".join(sections)
    if rewritten != content:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(rewritten)


if __name__ == "__main__":
    main()
