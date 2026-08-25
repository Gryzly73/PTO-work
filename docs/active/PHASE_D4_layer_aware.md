# PHASE D4: layer-aware смотрит починенный слой

**Basket:** [P01_HARNESS.md](./P01_HARNESS.md) · **D4**  
**Источник:** P01 · ТЗ §3.3 · Ask 2026-08-22  
**Статус:** цель реализована в текущем `main` другой архитектурой; единый порог 400 и `doc_context` неактуальны
**Зависит от:** D3  
**Код при составлении scope:** не менялся

> Действующий код использует `page_layer_text_fixed` / `page_layer_is_usable`: для `plan`, `scheme`, `mixed`, `table` требуется не менее 1500 символов и 200 слов. Возврат к плоскому порогу 400 создаст ложный `layer_ok` на штампах.

---

## Цель

`--layer-aware` / `PTO_LAYER_AWARE` пропускает PASS-B, если слой после deglyph читаемый и длинный (≥400). Сырой битый ToUnicode больше не держит тайлы.

## Решения Ask

1. `doc_context.page_layer(page, ctx=None)`: decode → normalize → garbled/пусто → `""`. Без таблиц.
2. `layer_ok = len(page_layer(...)) >= LAYER_MIN_CHARS` (400, не крутить).
3. Длина после `normalize`.
4. `run_vlm` в начале: `ensure(pdf, out_dir)` — кэш `doc_context.json` или `build`.
5. `convert.page_layer_and_tables` на том же `page_layer`. `PAGE_SCHEMA` не менять.
6. Mock: слой с `ctx` из кэша, если файл есть.
7. Дефолт CLI `--layer-aware` не менять.
8. PASS-A / PASS-T / паспорт / промпты / zone-hints не трогать.

## In scope

- `doc_context.py` (`page_layer`, `ensure`)
- `hf_api_bench.py` (блок layer-aware + кэш)
- тонко `service/convert.py`, `service/pipeline.py`, `service/worker.py`
- `tests/test_layer_aware.py`
- этот файл + статус в P01

## Out of scope

- `sheet_aware.py`, PASS-T, `--table-pages`
- промпты, `ZONE_HINTS`, HF
- полный `find_tables` по ИОС2
- смена дефолтов CLI

## Acceptance

- [x] Garbled без map → слой `""` → PASS-B не пропускается
- [x] Тот же лист + map, ≥400 после починки → `layer_ok`
- [x] Читаемый слой &lt;400 → тайлы остаются
- [x] `ctx=None` как D1 (сырой слой)
- [x] Второй `ensure` не зовёт `build`
- [x] D3-замки зелёные; ИОС2 не в обязательном pytest

## Verify

```powershell
cd PTO-work
py -3 -m pytest -q
```

## Checkpoint

| Поле | Значение |
|------|----------|
| Done | да, 2026-08-22 |
| Verify | `py -3 -m pytest -q` → 28 passed, 1 skipped (полный ИОС2) |
| PR | нет |
| Basket P01 | D4 → done |
| Долг / replan | Паспорт `large→scheme` — D5. Авто-PASS-T — D6. Дефолт CLI `--layer-aware` не меняли. |
