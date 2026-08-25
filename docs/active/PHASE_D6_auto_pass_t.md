# PHASE D6: Авто-PASS-T при kind==table и мёртвом слое

**Basket:** [P01_HARNESS.md](./P01_HARNESS.md) · **D6**  
**Источник:** P01 R3 · ТЗ §5.3 · Ask 2026-08-22  
**Статус:** историческая локальная реализация; в текущем `main` авто-PASS-T отсутствует
**Зависит от:** D5 (kind) + D4 (layer_ok)  
**Код при составлении scope:** не менялся

> Сейчас PASS-T включается только вручную через `--table-pages`; HTTP-профиль этот параметр не передаёт. Реализовывать авто-роутинг следует заново после актуального D5/raster signal.

---

## Цель

Ведомость без пригодного слоя идёт в PASS-T без `--table-pages`. Живой слой не перепечатывается. Ручной `--table-pages` остаётся override (в т.ч. растровый КР5).

## Решения Ask

1. Предикат `should_run_pass_t`. Тело PASS-T не трогать.
2. `want = manual or (auto and not layer_ok)`.
3. `layer_ok` как D4, считать один раз.
4. Порядок: PASS-0 → PASS-A → PASS-T | layer-aware | тайлы.
5. HTTP: без `PTO_TABLE_PAGES`.
6. `01.pdf` стр. 5 (`scheme`) авто не включает — только `--table-pages 5`.

## In scope

- `hf_api_bench.py` (предикат + условие)
- `tests/test_pass_t_route.py`
- этот файл + статус P01

## Out of scope

- промпты / score PASS-T
- `sheet_aware` пороги, `convert.py`, zone-hints
- HF в обязательном verify
- «угадать table» на скане

## Acceptance

- [x] `--table-pages` → True при любом kind
- [x] `kind==table` + мёртвый слой + sheet_aware → True
- [x] `kind==table` + живой слой → False
- [x] `scheme`/`plan` + мёртвый слой без manual → False
- [x] `sheet_aware=False` + table + мёртвый слой без manual → False
- [x] D1–D5 зелёные

## Verify

```powershell
cd PTO-work
py -3 -m pytest -q
```

## Checkpoint

| Поле | Значение |
|------|----------|
| Done | да, 2026-08-22 |
| Verify | `py -3 -m pytest -q` → 36 passed, 1 skipped |
| PR | нет |
| Basket P01 | D6 → done; basket P01 закрыт по коду |
| Долг / replan | North star п.2 на `01.pdf` КР5 частичный: авто не видит `table` (растр). Ручной `--table-pages 5` жив. Реальный PASS-T — opt-in, в verify не входил. |
