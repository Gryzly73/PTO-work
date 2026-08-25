# PHASE S6: Document-only resolver

**Basket:** [P02_SYMBOLS_HARNESS.md](./P02_SYMBOLS_HARNESS.md) · **S6**  
**Источник:** план P02, S6  
**Статус:** done (2026-08-24)  
**Зависит от:** S5  
**Код при составлении scope:** S6 не реализован

---

## Цель

Разрешать тип экземпляра только по свидетельствам текущего документа.

## In scope

- `matcher.py`, `resolver.py`;
- visual + position + nearby labels + DN/NO/NC + line context;
- confirmed только document/human;
- сохранение unclassified/unmatched и отдельных conflicts;
- false-binding/miss metrics.

## Out of scope

- автоматические инженерные связи и внешняя база знаний;
- замалчивание конфликтов.

## Acceptance

- [x] provenance объясняет каждый confirmed binding;
- [x] conflicts, unclassified и unmatched различимы;
- [x] false binding и miss входят в gate.

## Verify

```powershell
python -m pytest -q
```

## Harness

**Ask:** согласовать evidence weights и conflict policy.  
**Agent:** реализуй resolver S6 без инженерных догадок.

## Checkpoint

| Поле | Значение |
|---|---|
| Done | `symbols/matcher.py`, `symbols/resolver.py`; `ClassificationEvidence` и symbols sidecar schema 2; `tests/test_symbols_s6.py` |
| Verify | `py -m pytest -q` → 62 passed, 5 skipped; `py -m symbols.validate_gt --self-check` → passed |
| PR | не создавался |
| Basket P02 | S6 → done |
| Долг / replan | Synthetic: 2 confirmed с document-template provenance, 2 unknown остаются unclassified, 0 conflicts/unmatched; отдельные tests фиксируют ambiguous conflict, position tie-break, false binding и miss. Локальный PB не установлен; runtime/PASS-контракты не менялись. Старые symbols sidecars schema 1 требуют регенерации. |

