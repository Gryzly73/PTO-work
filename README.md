# PDF → Markdown для русских строительных чертежей

Конвейер извлечения контента из PDF проектной документации (текст, таблицы, чертежи)
в Markdown — для последующего Q&A другой моделью. Стратегическая цель — заменить
дорогой Gemini Flash/Pro на VLM, которую можно развернуть локально на сервере
**4×A16 (~61 GB VRAM, TP=4)** — см. `SERVER_HARDWARE.md`.

Приоритет — **корректный контекст** (текст + словесное описание листа), а не
pixel-perfect таблицы. Принцип: **пропуск лучше галлюцинации** — выдуманную марку
оборудования не отличить глазами от настоящей.

Текущая основная модель — `Qwen/Qwen3-VL-32B-Instruct` через HF Inference Providers
(featherless-ai), запасная — `Qwen3-VL-30B-A3B` (novita).

## Установка

```powershell
pip install -r requirements.txt
copy .env.example .env   # вписать HF_TOKEN
```

Требования:
- Python 3.10+, зависимости из `requirements.txt` (PyMuPDF, Pillow, pytesseract, huggingface_hub);
- Tesseract OCR с языками `rus+eng` (путь резолвится автоматически в `local_ocr.py`);
- `HF_PROVIDER=auto` в `.env` можно не трогать — раннер сам подставит провайдера из каталога.

**Windows:** в скриптах много кириллицы, консоль по умолчанию cp1251 → запускать
`python -X utf8 ...` либо перенаправлять вывод в файл.

## Быстрый старт

```powershell
# каталог моделей и флагов
python hf_api_bench.py --list

# прогон по страницам (two-pass включён по умолчанию)
python hf_api_bench.py --model qwen3vl-32b --pages 1,3,5

# sheet-aware режим: классификация листа → адаптивные промпты/DPI/зоны
python hf_api_bench.py --model qwen3vl-32b --pages 5,35,45 --sheet-aware `
  --pdf "path\to\document.pdf" --no-zone-hints --no-compare

# скоринг результата против эталона
python compare_to_etalon.py hf_runs/<stamp>_<model>_<tag>/out.md --pages 1,3,5

# сборка читаемого итогового Markdown по прогону
python build_ios2_md.py --run hf_runs/<run_dir> --pdf "document.pdf" -o итог.md

# HTML-вьюер «PDF слева / markdown справа» для проверки глазами
python build_quality_viewer.py --pdf "document.pdf" --md итог.md -o viewer/index.html
```

Полный документ ~49 страниц в two-pass занимает ~1.5–2 часа и ~$0.05–0.15 по API.

## Архитектура конвейера

Сердце проекта — `hf_api_bench.py`. Одна страница проходит:

1. **PASS-0** (опц., `--sheet-aware`) — `sheet_aware.py` строит «паспорт листа» из
   геометрии PDF и текстового слоя: тип листа (`text/table/plan/scheme/mixed`),
   CAD-метки, диаметры, заголовки. Паспорт идёт в промпт как якоря «не выдумывай
   сверх этого» и в итоговый md.
2. **PASS-A** — один вызов VLM на весь лист: подробное словесное описание
   (главный артефакт для Q&A). DPI подбирается по типу листа. На выходе —
   quality-gate (слишком коротко / петля повторов / потерянные якоря) с retry.
3. **PASS-B** — лист режется на тайлы (≤6–8) плюс семантические зоны под тип листа
   (легенда, основная таблица, штамп, правая полоса). Каждый фрагмент — отдельный вызов.
4. **Anti-loop чистка** — схлопывание петель повторов, дедуп строк, срез
   «числовых галлюцинаций» (VLM на плотных чертежах уходит в циклы).
5. **`--runs N`** — N независимых прогонов Pass-B с объединением уникальных фактов
   (борьба с недетерминизмом, повышает recall).

Артефакты: `hf_runs/<UTC_stamp>_<model>_<tag>/` — `meta.json` (конфиг + usage),
`out.md`, `pages/page_NNNN.md`.

**Контракт разбиения:** все скрипты режут markdown по `## Страница N`, внутри —
по `### PASS-0 / PASS-A / PASS-B`. Эти заголовки — API между инструментами, не менять.

### Слои компенсации вокруг VLM

| Скрипт | Роль |
|---|---|
| `local_ocr.py` | Tesseract по углам штампа — вытаскивает `doc_code`, которые VLM теряет |
| `merge_vlm_ocr.py` | детерминированный union VLM+OCR по нормализованным токенам (без LLM — проверено, что LLM-synth хуже) |
| `build_ocr_vlm_tables.py` | таблицы: VLM даёт структуру, OCR — ячейки, плюс текстовый слой PDF |
| `sheet_aware.py` | классификация листа, паспорт, бюджеты рендера, зоны |
| `build_ios2_md.py` | сборка итога; детект «кракозябренного» текстового слоя CAD-PDF |
| `compare_to_etalon.py` | rough recall / key-phrase скоринг против эталонных страниц |
| `build_quality_viewer.py` | HTML-вьюер для проверки глазами |
| `patch_page_into_run.py` | точечная замена одной страницы в готовом прогоне |

## Метрики — их две, не смешивать

| Метрика | Инструмент | Что считает |
|---|---|---|
| rough recall / key | `compare_to_etalon.py` | token_recall + key_phrase_hit против эталонного md |
| checklist % | `new_files/pto_scoring_kit/score_real_testset.py` | исторический чек-лист токенов, потолок ~64% |

Модели недетерминированны (разброс до 55 пунктов между прогонами) → выводы только
по медиане из ≥4 прогонов, отчитываться медианой **и** худшим. Перед доверием скору —
проверить вывод на `[Error` и подозрительно короткий текст.

## Состояние (2026-07)

- Лучший замер: easy pages rough recall **43.4%** / key **61.4%**; hard pages **33.5% / 52.9%**.
- Продовый выхлоп ИОС2 (49 стр.) собран, проверен глазами через quality viewer.
- Журнал итераций с отрицательными результатами — `LOOP_LEDGER.md` (читать перед
  предложением новой идеи: тупики задокументированы).
- Тупики, не повторять: DeepSeek-OCR через HF API, qwen2.5-vl-72b (латинизация),
  gemma3-27b и llama4-scout (галлюцинации), full-page Tesseract, LLM-synth поверх union.

## Структура репозитория

```
hf_api_bench.py        — основной раннер (каталог моделей, two-pass, sheet-aware, pipeline)
sheet_aware.py         — паспорт листа, классификация, зоны, бюджеты DPI
local_ocr.py           — Tesseract-OCR штампа
merge_vlm_ocr.py       — детерминированный union VLM+OCR
build_ios2_md.py       — сборка итогового Markdown
build_quality_viewer.py— HTML-вьюер для ручной проверки
compare_to_etalon.py   — скоринг против эталона (page_map.json — маппинг страниц)
build_ocr_vlm_tables.py— гибридное извлечение таблиц
patch_page_into_run.py — замена страницы в прогоне
build_best_of.py, build_compare_html.py — вспомогательная сборка/сравнение
pdf_to_markdown.py     — (справочно) исходный Gemini-движок, источник идей
pdf_to_md_ollama.py    — (справочно) локальный путь через Ollama
test_local_vlm_candidates.py, loop_bench.py — (справочно) бенчи прошлой фазы
LOOP_LEDGER.md         — журнал итераций и отрицательных результатов
SERVER_HARDWARE.md     — целевой сервер для локального развёртывания
HF_MODEL_CANDIDATES.md, NEXT_PHASE_research.md — история отбора моделей и планы
```

Тестовые PDF, эталоны, прогоны (`hf_runs/`) и сгенерированные отчёты в репозиторий
не входят — см. `.gitignore`.
