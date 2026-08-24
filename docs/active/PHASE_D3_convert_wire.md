# PHASE D3: Слой и таблицы в клиентский лист

**Basket:** [P01_HARNESS.md](./P01_HARNESS.md) · **D3**  
**Источник:** P01 · ТЗ §3.2–3.3, §5.2–5.3 · Ask 2026-08-22  
**Статус:** done (2026-08-22)  
**Зависит от:** D2  
**Код при составлении scope:** не менялся

---

## Цель

HTTP-лист отдаёт починенный текстовый слой и таблицы по сетке. Сырой `page_NNNN.md` (PASS-0/A/B) не меняется. `run_vlm` не трогаем.

## Решения Ask

1. Контекст — в начале `_run_job`, до пула листов; кэш `run_dir/doc_context.json`.
2. `ctx=None` в convert: поведение как D1.
3. `frames` из JSON → `set[tuple]` для `page_tables_md`.
4. `PAGE_SCHEMA` 2 → 3.
5. CLI / `build_ios2_md` не переключать.
6. ИОС2 в pytest не гонять.

## In scope

- `service/convert.py`, `service/worker.py`
- тесты convert + кэш воркера
- этот файл + статус в P01

## Out of scope

- `hf_api_bench.py` / layer-aware (D4)
- `sheet_aware.py` (D5), PASS-T (D6)
- промпты, zone-hints, HF

## Acceptance

- [x] Garbled без map → `extractedText == ""`; есть `**Файл:**`
- [x] Decode с map снимает garbled, если после починки слой читаемый
- [x] Таблицы в markdown **перед** «Текст листа»
- [x] Пустой слой + `main_code` → `source_note`; уже есть в PASS-A — не дублировать
- [x] Replay `01.pdf` + прогон 20260819 (skip если нет) → `extractedText == ""`
- [x] `PAGE_SCHEMA == 3`; кэш `doc_context.json` не зовёт `build` повторно
- [x] Diff без `hf_api_bench.py` / `sheet_aware.py`

## Verify

```powershell
cd PTO-work
py -3 -m pytest -q
```

## Checkpoint

| Поле | Значение |
|------|----------|
| Done | да, 2026-08-22 |
| Verify | `py -3 -m pytest -q` → 23 passed, 1 skipped (полный ИОС2) |
| PR | нет |
| Basket P01 | D3 → done |
| Долг / replan | `run_vlm` / layer-aware всё ещё без ctx — это D4. CLI не переключали. |
