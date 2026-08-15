# Кандидаты под сервер 4×A16 (~61 GB VRAM) + HF API тесты

## Волна 2026-08-14 — новые мультимодальные модели (Qwen3.6 / GLM-V / Kimi / gemma-4)

Прогон: страницы 1,3,5 эталона, two-pass по умолчанию, метрика rough
(`compare_to_etalon.py`). Baseline: **qwen3vl-32b = 43.4% recall / 61.4% key**.
⚠️ По 1 прогону на модель (кроме лидера — см. ниже) — ранжирование, не вердикт.

| id | recall | key | $/3 стр. | вердикт |
|---|---|---|---|---|
| `kimi-k3` | **59.0%** | **74.7%** | $0.74 | лучший, но ~100× дороже Qwen; многословный (p5 ×6.5 длиннее эталона); только как потолок/эталон |
| `qwen3vl-235b` | 49.5% | 64.0% | $0.03 | выше baseline, дёшев; API-only (на 4×A16 не влезет); 504-е у deepinfra |
| `gemma4-26b-a4b` | 49.2% | 65.3% | $0.006 | ❌ ДИСКВАЛИФИКАЦИЯ: галлюцинирует штамп («ООО КИРПРОЕКТПРОЕКТ» вместо КУРСКРЕГИОНПРОЕКТ), латинизирует шифры — родовая травма gemma |
| `qwen36-35b-a3b` | 48.4% | 72.9% | $0.02 | ★ главный кандидат: MoE A3B = серверный класс, лучший key среди доступных |

Подтверждение лидера (easy 1,3,5): run2 = 44.9/73.6, run3 (p1,p3) = 41.7/74.3 —
key-hit стабильно 73–74% (baseline 61.4%), recall 45–48%. Прогон 4 остановлен.
⚠️ Hard pages (2,4, 1 прогон): qwen36-35b-a3b = 22.1/27.0 — ХУЖЕ qwen3vl-32b
(30.2/49.5 в той же конфигурации). Паттерн: новинка сильнее на текстах/таблицах,
слабее на плотной графике → возможен роутинг по типу листа (sheet-aware уже
классифицирует): text/table → qwen36-35b-a3b, plan/scheme → qwen3vl-32b.
| `qwen36-27b` | 45.8% | 62.7% | $0.11 | выше baseline; dense, дороже 35B-A3B по $/токен на deepinfra |
| `glm-45v` | 38.1% | 59.2% | $0.11 | ниже baseline |
| `glm-46v-flash` | 17.9% | 34.4% | $0.04 | искажает кириллицу («золотой переборочной…») — тупик |

**Межмодельный union (2026-08-14, `build_model_union.py`)** — лучший замер проекта:
qwen36-35b-a3b (база) + qwen3vl-32b (дополнение), свежие прогоны 1–6, метрика rough:
easy **55.6 / 73.4** (соло: 47.3/71.5 и 44.8/62.4), hard **39.7 / 48.8** без зон
(соло: 28.1/24.4 и 29.5/32.5). Модели дополняют друг друга; recall частично растёт
за счёт объёма — проверка глазами через quality_viewer_union. В прогоне также впервые
активен `collapse_inline_repetition` (внутристрочный anti-loop, кейс стр.35 ИОС2).

Механика: **Qwen3.6 — thinking-гибриды**; без
`chat_template_kwargs={"enable_thinking": false}` весь max_tokens уходит в
рассуждения, content пустой. Раннер отключает это автоматически
(`NO_THINK_HF_IDS` в `hf_api_bench.py`).

Цель: через Hugging Face API отобрать **VLM (черновик с чертежа)** и **text-model (synthesis/fuse)**, которые потом встанут на сервер из `SERVER_HARDWARE.md`.

## Железо (ограничение выбора)

| | |
|--|--|
| GPU | 4× NVIDIA A16 ≈ **15.3 GB** каждая |
| Пул | TP=4 → **~61 GB** суммарно |
| Уже крутится | `Qwen3.5-35B-A3B-GPTQ-Int4` (почти весь VRAM) |
| Реализм | BF16 32B+ — на грани/нет; **AWQ/GPTQ Int4** — основной путь для ≥27B |

**Две модели в проде:** одновременно в VRAM не держат два «толстых» веса. Схема:

1. **Sequential (рекомендуем для тестов→прода):** VLM → unload → text-synth (как на Ollama).
2. **Split GPU:** VLM на 2×A16, text на 2×A16 — только если обе ≤~27–30 GB с квантом.

Не тестируем через API то, что **заведомо не влезет** локально (405B, 235B dense BF16, Llama-90B Vision BF16 и т.п.).

---

## Роль A — VLM (PDF page/tile → текст)

| id | HF model id (ориентир) | Оценка на 4×A16 | Приоритет API |
|----|------------------------|-----------------|---------------|
| `qwen25vl-7b` | `Qwen/Qwen2.5-VL-7B-Instruct` | ✅ легко | smoke / baseline API |
| `qwen3vl-8b` | `Qwen/Qwen3-VL-8B-Instruct` | ✅ легко | high (наш локальный winner-класс) |
| `llama32-11b-vision` | `meta-llama/Llama-3.2-11B-Vision-Instruct` | ✅ легко | medium |
| `qwen25vl-32b` | `Qwen/Qwen2.5-VL-32B-Instruct` | ✅ только AWQ/GPTQ / tight BF16 | **high** |
| `gemma3-27b` | `google/gemma-3-27b-it` | ⚠️ AWQ или урезанный ctx | high (Cyrillic lineage) |
| `qwen3vl-30b-a3b` | `Qwen/Qwen3-VL-30B-A3B-Instruct` | ✅ MoE Int4/AWQ похож на текущий сервер | **high** |
| `qwen3vl-32b` | `Qwen/Qwen3-VL-32B-Instruct` | ✅ AWQ/GPTQ | **high** (dense VLM, featherless live) |
| `deepseek-ocr` | `deepseek-ai/DeepSeek-OCR` | ✅ компактный doc-OCR | **high** (novita live; preset prompts) |

Исключить из shortlist: VL 72B+ BF16, Llama-3.2-90B-Vision без агрессивного кванта.

---

## Роль B — Text synth (draft + OCR → финальный MD)

| id | HF model id (ориентир) | Оценка на 4×A16 | Приоритет API |
|----|------------------------|-----------------|---------------|
| `qwen35-4b` | `Qwen/Qwen3.5-4B` / локальный аналог | ✅ | low (уже пробовали — регресс) |
| `llama31-8b` | `meta-llama/Llama-3.1-8B-Instruct` | ✅ | medium |
| `qwen25-14b` | `Qwen/Qwen2.5-14B-Instruct` | ✅ | medium |
| `gemma2-27b` | `google/gemma-2-27b-it` | ✅ AWQ/BF16 с запасом | high |
| `qwen25-32b` | `Qwen/Qwen2.5-32B-Instruct` | ✅ AWQ / ⚠️ BF16 | **high** (исторический рычаг 46→64%) |
| `qwen35-35b-a3b` | `Qwen/Qwen3.5-35B-A3B` | ✅ уже на сервере GPTQ | **high** (бесплатный reuse железа) |
| `llama31-70b-awq` | `meta-llama/Llama-3.1-70B-Instruct` (+AWQ на Hub) | ⚠️ только Int4/AWQ, мало KV | medium |

Исключить: DeepSeek-V3/R1 full, Qwen-235B dense без MoE+кванта.

---

## Пайплайны для A/B на API

1. **VLM-only** — тайлы как в `pdf_to_md_ollama.py`, без synth.
2. **VLM + stamp OCR append** — без LLM-synth.
3. **VLM + text synth** — fuse draft+штамп через роль B (после VLM).

Метрика: `compare_to_etalon.py` на страницах **1,3,5** (smoke), затем полный набор 1–6.

Локальный ориентир (оставить):

| Run | Avg recall | Key hit |
|-----|------------|---------|
| `qwen3-vl:8b-instruct` Ollama 8GB | ~24.1% | ~23.6% |
| + stamp OCR | ~24.8% | ~23.6% |

Ожидание на серверных 27–32B VLM / 32B synth: заметно выше rough-score; целевой исторический потолок полного пайплайна ~**64%** checklist (не эта метрика 1-в-1).

---

## Как гонять

```bash
# 1) скопировать токен
copy .env.example .env
# вписать HF_TOKEN=hf_...

# 2) зависимости
pip install -r requirements.txt

# 3) список кандидатов
python hf_api_bench.py --list

# 4) smoke VLM (1 страница)
python hf_api_bench.py --role vlm --model qwen3vl-8b --pages 1 --force

# 5) полный smoke 1,3,5
python hf_api_bench.py --role vlm --model qwen25vl-32b --pages 1,3,5

# 6) synth поверх готового draft
python hf_api_bench.py --role synth --model qwen25-32b --draft hf_runs/.../out.md --pages 1,3,5
```

Выводы пишутся в `hf_runs/<timestamp>_<id>/`.
