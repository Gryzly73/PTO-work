# PHASE S2: Vertical slice заданной легенды

**Basket:** [P02_SYMBOLS_HARNESS.md](./P02_SYMBOLS_HARNESS.md) · **S2**  
**Источник:** план P02, S2  
**Статус:** done (2026-08-24)  
**Зависит от:** S1  
**Код при составлении scope:** S2 не реализован

---

## Цель

Извлекать строки и crops из переданной области легенды воспроизводимо и офлайн.

## In scope

- `legend_rows.py`, `crop_normalizer.py`, интерфейс `LegendTextReader`;
- fixture bbox только как CLI-ввод, PDF-text first, тестовые fakes;
- CLI, rows/crops artifacts, GT rows и PB smoke.

## Out of scope

- автоматический поиск области; page-specific bbox в runtime;
- обязательные OCR/VLM/HF вызовы.

## Acceptance

- [x] заданная область даёт детерминированные rows/crops;
- [x] reader подменяется fake и предпочитает PDF text;
- [x] synthetic gate проходит; PB smoke является opt-in и явно skip без локальной fixture.

## Verify

```powershell
python -m pytest -q
```

## Harness

**Ask:** проверить row boundaries и reader contract.  
**Agent:** реализуй S2 без auto-layout и сетевых вызовов.

## Checkpoint

| Поле | Значение |
|---|---|
| Done | да, 2026-08-24 |
| Файлы | `symbols/legend_rows.py`, `symbols/crop_normalizer.py`, `tests/test_symbols_s2.py`, этот checkpoint, `P02_SYMBOLS_HARNESS.md` |
| Verify | `py -m pytest -q` → `41 passed, 2 skipped in 27.37s`; `py -m symbols.validate_gt --self-check` → `GT self-check passed: 1 fixture(s)`; IDE lint → ошибок нет |
| Контракты | region поступает только через `--region` или fixture `--gt`; canonical bbox остаётся в PDF points; `PdfTextReader` — первый offline adapter; fake reader не зависит от OCR/VLM; каждая строка сохраняет raw и 96×96 normalized crop |
| Метрики | synthetic legend row recall при IoU ≥ 0.5: 2/2; byte-stable повторный прогон; PB local smoke: skip, fixture отсутствует |
| PR | |
| Basket P02 | S2 → done; S3 → pending |
| Долг / replan | S2 не ищет legend region; borderless unreadable rows без геометрических разделителей не могут быть восстановлены до S3/layout evidence |

