# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это за проект

Конвейер **PDF русских строительных чертежей → Markdown** для последующего Q&A другой моделью.
Ключевая цель: заменить дорогой Gemini Flash/Pro на модель, которую реально поставить локально
на сервер **4×A16 (~61 GB VRAM суммарно, TP=4)** — см. `SERVER_HARDWARE.md`.

Приоритет вывода — **корректный контекст** (текст + словесное описание чертежа), а не
pixel-perfect таблицы. Пропуск лучше галлюцинации: выдуманную марку оборудования не видно глазами.

Текущий основной VLM — `Qwen/Qwen3-VL-32B-Instruct` через HF Inference Providers
(provider `featherless-ai`), запасной — `Qwen3-VL-30B-A3B` (novita).

Репозиторий под git; тестовые PDF, эталоны, `hf_runs/` и сгенерированные отчёты
не коммитятся (см. `.gitignore`).

## Setup

```powershell
pip install -r requirements.txt
copy .env.example .env   # вписать HF_TOKEN
```

`.env` читается самописным `load_dotenv()` в `hf_api_bench.py` (не python-dotenv).
`HF_PROVIDER=auto` **ломается** (`model_not_supported`) — раннер подставляет
`preferred_provider` из `CATALOG`. Локальный OCR требует Tesseract (`rus+eng`),
путь резолвится в `local_ocr.py:resolve_tesseract_cmd()`.

## Основные команды

```powershell
# каталог моделей + список флагов качества
python hf_api_bench.py --list

# прогон VLM по эталонным hard-страницам (two-pass ON по умолчанию)
python hf_api_bench.py --model qwen3vl-32b --pages 1,3,5

# sheet-aware режим (классификация листа → адаптивные промпты/DPI/зоны)
python hf_api_bench.py --model qwen3vl-32b --pages 5,35,45 --sheet-aware `
  --pdf "new_files/Раздел ПД №5 Подраздел №2 (ИОС2).pdf" --no-zone-hints --no-compare

# hard pages с зонами и высоким DPI
python hf_api_bench.py --model qwen3vl-32b --pages 2,4 --high-dpi --zone-crop --runs 2

# лист-таблица: PASS-T «перенеси таблицу как в исходнике» вместо тайлов
# (авторотация повёрнутых листов через Tesseract OSD работает всегда)
python hf_api_bench.py --model qwen3vl-32b --pages 5 --table-pages 5

# union выводов двух разных моделей (лучший замер: easy 56-57% / 77%)
python build_model_union.py --base <runA>/out.md --extra <runB>/out.md `
  --extra-label qwen3vl-32b --reclean -o union.md

# one-shot: VLM → union с OCR → скоринг
python hf_api_bench.py --model qwen3vl-32b --pipeline --pages 1,3,5

# скоринг любого md против эталона
python compare_to_etalon.py hf_runs/<stamp>_<model>_<tag>/out.md --pages 1,3,5

# сборка читаемого итога для произвольного PDF
python build_ios2_md.py --run hf_runs/<stamp>_qwen3vl-32b_twopass `
  --pdf "new_files/Раздел ПД №5 Подраздел №2 (ИОС2).pdf" -o ИОС2_итог.md

# HTML-вьюер «страница PDF слева / markdown справа» для ручной проверки
python build_quality_viewer.py --pdf "<file>.pdf" --md ИОС2_итог.md -o quality_viewer_ios2/index.html

# точечная замена одной страницы в готовом прогоне
python patch_page_into_run.py --run <full_run_dir> --page-run <single_page_run_dir> --page 35

# детерминированный union VLM+OCR, без LLM
python merge_vlm_ocr.py --draft <run>/out.md --ocr-dir bench_ocr_135.md.pages -o union.md

# sanity-check локального Tesseract
python -c "import local_ocr; print(local_ocr.ocr_healthcheck())"
```

Прогон **одной страницы** = `--pages N`. Диапазоны и списки: `--pages 35-41,44-49`.
Полный документ (49 страниц, two-pass) занимает ~1.5–2 часа и ~$0.05–0.15.

**Windows-гоча:** в скриптах много кириллицы, консоль по умолчанию cp1251 →
`UnicodeEncodeError`. Запускать `python -X utf8 ...` или перенаправлять вывод в файл.
Логи в `hf_runs/_cmp_*.log` сохранены в UTF-16 (PowerShell `Tee-Object`).

## Архитектура

### Конвейер извлечения (`hf_api_bench.py`, ~2000 строк — сердце проекта)

Одна страница проходит:

1. **PASS-0 (опц., `--sheet-aware`)** — `sheet_aware.build_passport()` строит «паспорт листа»
   из геометрии PDF + текстового слоя: `kind ∈ {text, table, plan, scheme, mixed}`,
   уникальные CAD-метки, диаметры, заголовки. Паспорт попадает и в промпт (как якоря
   «не выдумывай сверх этого»), и в итоговый md.
2. **PASS-A** — один вызов на **весь лист** целиком: подробное словесное описание.
   Это главный артефакт для Q&A. DPI/`page_max` выбираются по `kind`
   (`sheet_aware.render_budget()`: plan → до 3200px, text → 1800–2000px).
   На выходе — quality-gate `pass_a_quality_ok()` (too_short / repetition_loop /
   пропущенные якоря); при fail — retry зональным промптом.
3. **PASS-B** — лист режется на тайлы (`tile_grid()`, ≤6–8 тайлов) + при `--sheet-aware`
   добавляются **семантические зоны** под тип листа (`generic_describe_zones()`:
   `zone_NW/NE/SW/SE`, `zone_right_strip`, `zone_legend_bottom`, `zone_main_table`,
   `zone_stamp`). Каждый фрагмент — отдельный вызов. Мусорные тайлы
   (голые «1..N») отбрасываются `is_garbage_tile()`.
4. **Anti-loop чистка** — `collapse_repetition` / `dedupe_lines` /
   `collapse_numeric_list` / `collapse_numbered_hallucination`. VLM на плотных
   чертежах уходит в петли; без этого выхлоп бесполезен.
5. **`--runs N`** — N независимых прогонов Pass-B, `union_texts()` объединяет
   уникальные факты (борьба с недетерминизмом, повышает recall).

Артефакты прогона: `hf_runs/<UTC_stamp>_<model_id>_<tag>/` где tag ∈
`vlm|synth|twopass|sheetaware|pipeline`; внутри `meta.json` (полный конфиг + usage),
`out.md`, `pages/page_NNNN.md`.

### Контракт разбиения

**Все** скрипты режут markdown по `^## Страница N$`, внутри страницы — по
`### PASS-0 / ### PASS-A / ### PASS-B`. Ломать эти заголовки нельзя: на них завязаны
`compare_to_etalon.py`, `build_ios2_md.py`, `merge_vlm_ocr.py`, `build_best_of.py`,
`build_quality_viewer.py`, `patch_page_into_run.py`.

### Слои компенсации (вокруг VLM)

- `local_ocr.py` — Tesseract по углам штампа, вытаскивает `doc_code`, которые VLM теряет.
- `merge_vlm_ocr.py` — **детерминированный** union по нормализованным токенам.
  Проверено: LLM-synth поверх draft+OCR **не лучше** union, чаще сжимает. Не «перепиши красиво».
- `build_ocr_vlm_tables.py` — VLM (структура) + DeepSeek-OCR (ячейки) + текстовый слой PDF.

### Текстовый слой PDF

`build_ios2_md.py:is_garbled_pdf_text()` — у CAD-PDF часто битый ToUnicode: слой
выглядит кракозябрами и модели вреден. Такие страницы помечаются, опора идёт на VLM.
`normalize_pdf_text()` схлопывает спам одинаковых меток (одна метка `В1` на каждом
сегменте трубы → сотни повторов).

### Метрики — их ДВЕ, не смешивать

| Метрика | Где | Что считает |
|---|---|---|
| rough recall/key | `compare_to_etalon.py` | token_recall + key_phrase_hit против `ЭТАЛОН — …/ИДЕАЛ_*.ref.md`, маппинг страниц в `page_map.json`. Грубая, но быстрая. |
| checklist % | `new_files/pto_scoring_kit/score_real_testset.py` | исторический чек-лист токенов (`ground_truth/*.meta.json`), матчеры `contains/any_contains/all_contain/and_any/not_contains`. Исторический потолок ~64%. |

Цифра «43%» (rough) и «64%» (checklist) — **разные шкалы**. В отчётах всегда указывать какая.
`score_real_testset.py --self-check` должен давать ~100% — иначе сломан матчер.

### Дисциплина измерений

Модели недетерминированны на hard-страницах: разброс до **55 пунктов** между прогонами
(`LOOP_LEDGER.md`). Правила, выведенные болью:

- никогда не делать вывод по 1–2 прогонам; медиана из ≥4, отчитываться медианой И худшим;
- перед тем как верить скору — проверить, нет ли в выводе `[Error` или подозрительно
  короткого текста (был случай: 3/6 страниц = `[Error 413]` → фальшивые 6%);
- каждое изменение должно иметь механизм, работающий на **невиданном** чертеже;
  не подглядывать в `*.meta.json`, чтобы подогнать промпт.

`LOOP_LEDGER.md` — журнал итераций с отрицательными результатами (per-tile rerun,
native-DPI cap, LLM-synth). Читать перед тем, как предлагать идею оттуда повторно.

## Известное состояние качества (на 2026-07-23)

- Лучший замер против эталона: easy pages (1,3,5) rough recall **43.4%** / key **61.4%**;
  hard pages (2,4) two-pass+zone **33.5% / 52.9%**.
- Продовый выхлоп ИОС2 (49 стр.) собран, но **не отскорен** — эталона для этого PDF нет,
  проверка только глазами через `quality_viewer_ios2/index.html`.
- В `ИОС2_итог.md` на стр. 35 виден **непойманный repetition-loop внутри одной строки**
  заголовка («подземные инженерные сети, подземные инженерные коммуникации, …» ×N):
  `dedupe_lines` работает построчно и такой случай пропускает.
- Тупики, не повторять: DeepSeek-OCR через HF API (петли, греческий мусор),
  qwen2.5-vl-72b (тотальная латинизация кириллицы), gemma3-27b (выдумывает таблицы/штампы),
  llama4-scout (выдумывает марки оборудования), full-page Tesseract, LLM-synth поверх union.

## Устаревшее / справочное

- `pdf_to_markdown.py` (113 KB) — исходный Gemini-движок «от Владимира Михайловича»
  с авто-тайлингом и verification-pass. Источник идей, в текущем конвейере не используется.
- `pdf_to_md_ollama.py` — локальный путь через Ollama на RTX 4060 8GB
  (вывод: 8b на целом чертеже залипает в thinking, работает только на кропах ~512–640px).
- `test_local_vlm_candidates.py`, `loop_bench.py` — бенчи прошлой фазы (3 синтетические
  фикстуры, near-ceiling, прогресс уже не меряют).
- `HF_MODEL_CANDIDATES.md`, `NEXT_PHASE_research.md`, `REPORT_2026-07-16.md`,
  `RESULTS_all_models_vs_ground_truth.md` — история отбора моделей и планы фаз.
