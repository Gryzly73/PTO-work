# PHASE S0: Operational harness и GT

**Basket:** [P02_SYMBOLS_HARNESS.md](./P02_SYMBOLS_HARNESS.md) · **S0**  
**Источник:** план P02, S0  
**Статус:** done (2026-08-24)  
**Зависит от:** нет  
**Runtime:** не меняется

---

## Цель

Создать полностью офлайн и версионированный контур GT, синтетических фикстур,
валидации и bbox-метрик до реализации алгоритма распознавания.

## In scope

- `symbols/`: GT schema v1, stdlib validator, IoU scorer и CLI self-check;
- сущности legend region/rows, instances, negative components, expected statuses;
- synthetic SVG, GT, perfect/bad predictions и manifests;
- pytest current-contract и S0 tests, dev-only pinned pytest;
- gitignore только для локального customer manifest и crops;
- scope S1–S8 и отдельный basket P02.

## Out of scope

- runtime dataclasses, geometry/artifacts и pipeline S1+;
- HTTP, HF, Tesseract, network и customer data;
- правки P01, production requirements и публичного page JSON;
- commit.

## Acceptance

- [x] schemaVersion отвергается явно, ошибки имеют `ValidationError`;
- [x] legend region — объект `id`/`bbox`/`status`, согласованный со статусом страницы;
- [x] row и instance outcomes разделены; type/legend references проверяются;
- [x] метрики считают legend row recall, bbox IoU, false binding, duplicate и miss;
- [x] perfect и bad predictions доказаны тестами;
- [x] локальная customer fixture opt-in и skip при отсутствии;
- [x] schema 5 и `**Файл:**` закреплены current-contract тестами;
- [x] оба gate проходят без внешних сервисов.

## Verify

```powershell
python -m pytest -q
python -m symbols.validate_gt --self-check
```

## Harness

**Ask:** риски/A-B без правок.  
**Agent:** реализуй только S0; не менять P01/runtime; не звать HF/Tesseract/network.

## Checkpoint

| Поле | Значение |
|---|---|
| Done | да, 2026-08-24 |
| Файлы | S0 review: `symbols/gt_schema.py`, три `symbols/fixtures/synthetic*.json`, `tests/test_symbols_harness.py`, этот checkpoint |
| Verify | `python -m pytest -q` → `20 passed, 1 skipped in 5.77s`; `python -m symbols.validate_gt --self-check` → `GT self-check passed: 1 fixture(s)` |
| Score sanity | perfect: row recall/instance recall/binding precision `1.0`, false binding/miss/duplicate `0`; bad: row recall `0.5`, false binding `1`, miss `2`, duplicate `1` |
| PR | нет |
| Basket P02 | S0 → done; S1 → pending |
| Долг / replan | локальный customer manifest отсутствует штатно; соответствующий тест skipped |

