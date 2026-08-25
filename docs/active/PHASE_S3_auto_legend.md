# PHASE S3: Automatic legend layout

**Basket:** [P02_SYMBOLS_HARNESS.md](./P02_SYMBOLS_HARNESS.md) · **S3**  
**Источник:** план P02, S3  
**Статус:** done (2026-08-24)  
**Зависит от:** S2  
**Код при составлении scope:** S3 не реализован

---

## Цель

Находить легенду и её строки автоматически, возвращая `not_found` при слабом сигнале.

## In scope

- `legend_layout.py`: headings, PDF words, row layout, whitespace и frames;
- raster evidence как дополнительный сигнал, confidence/not_found;
- synthetic + PB; KR4 только holdout.

## Out of scope

- runtime bbox конкретной страницы; принудительный результат при low confidence;
- template matching экземпляров.

## Acceptance

- [x] область/строки находятся без fixture coordinates;
- [x] low confidence даёт `not_found`;
- [x] KR4 не используется для настройки.

## Verify

```powershell
python -m pytest -q
```

## Harness

**Ask:** согласовать confidence evidence и holdout protocol.  
**Agent:** реализуй S3, сохрани explicit not_found.

## Checkpoint

| Поле | Значение |
|---|---|
| Done | `symbols/legend_layout.py`: heading/PDF-word, vector-frame, row-pattern, graphic-left/text-right, whitespace и raster-frame evidence; explicit confidence/`not_found`; auto-orchestrator и CLI. `score_gt.py`: region precision/recall. `tests/test_symbols_s3.py`: synthetic, blank negative, borderless heading, raster+injected OCR, CLI/scorer, opt-in PB. |
| Verify | `py -m pytest -q` → `47 passed, 3 skipped`; `py -m symbols.validate_gt --self-check` → `GT self-check passed: 1 fixture(s)`; IDE lint изменённых Python-файлов → без ошибок. |
| PR | Не создавался; commit/PR только по отдельному запросу. |
| Basket P02 | S3 → done (2026-08-24) |
| Метрики | Synthetic: region IoU `1.0`, region precision/recall `1.0/1.0`, rows `2/2`; blank negative → `not_found`. PB smoke пропущен: локальный manifest не установлен. |
| Долг / replan | Raster-only frame остаётся evidence, а не основанием для forced match; scanned legend требует подключаемого OCR reader. KR4 не запускался и сохраняется hold-out. |

