# PHASE S7: Catalog and review

**Basket:** [P02_SYMBOLS_HARNESS.md](./P02_SYMBOLS_HARNESS.md) · **S7**  
**Источник:** план P02, S7  
**Статус:** done (2026-08-24)  
**Зависит от:** S6  
**Код при составлении scope:** S7 не реализован

---

## Цель

Строить детерминированный каталог из sidecars и сохранять аудируемые review decisions.

## In scope

- `catalog.py`, `review.py`;
- confirmed document entries only и полная provenance;
- решения mapping/split/merge/not_a_symbol/unresolved без VLM;
- byte-stable, order-independent output и audit trail.

## Out of scope

- недетерминированное обогащение и VLM;
- включение unconfirmed entries в confirmed catalog.

## Acceptance

- [x] одинаковые sidecars дают byte-identical catalog независимо от порядка;
- [x] каждое review decision аудируемо;
- [x] unresolved не теряются.

## Verify

```powershell
python -m pytest -q
```

## Harness

**Ask:** согласовать review decision schema.  
**Agent:** реализуй deterministic post-sidecar S7 без VLM.

## Checkpoint

| Поле | Значение |
|---|---|
| Done | `symbols/catalog.py`, `symbols/review.py`, `tests/test_symbols_s7.py`; versioned document catalog и append-only review log |
| Verify | `py -m pytest -q` → 66 passed, 5 skipped; `py -m symbols.validate_gt --self-check` → passed |
| PR | не создавался |
| Basket P02 | S7 → done |
| Долг / replan | Catalog v1 группирует одинаковые документные normalized names, хранит page/legend/type/instance/crop/evidence provenance и не меняет page sidecars. Human decisions имеют приоритет как детерминированная проекция; физическое копирование variant crops и runtime-finalizer остаются S8. |

