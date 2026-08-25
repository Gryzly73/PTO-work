# PHASE S4: Known template instances

**Basket:** [P02_SYMBOLS_HARNESS.md](./P02_SYMBOLS_HARNESS.md) · **S4**  
**Источник:** план P02, S4  
**Статус:** done (2026-08-24)  
**Зависит от:** S3  
**Код при составлении scope:** S4 не реализован

---

## Цель

Находить экземпляры известных в документе шаблонов с multi-scale matching и dedupe.

## In scope

- `template_matcher.py`, `dedupe.py`;
- только шаблоны текущего документа, multi-scale, dedupe, unmatched;
- PyMuPDF/Pillow first; OpenCV только при измеренном A/B выигрыше;
- PB full path.

## Out of scope

- open set и глобальная библиотека символов;
- автоматическое инженерное связывание.

## Acceptance

- [x] known templates дают bbox matches и сохраняют unmatched;
- [x] дубли удаляются детерминированно;
- [x] OpenCV не добавлен без A/B evidence.

## Verify

```powershell
python -m pytest -q
```

## Harness

**Ask:** определить scales/IoU thresholds и A/B gate.  
**Agent:** реализуй только known-template path S4.

## Checkpoint

| Поле | Значение |
|---|---|
| Done | `symbols/template_matcher.py`, `symbols/dedupe.py`, `tests/test_symbols_s4.py`; document-only templates, scales `0.75/1.0/1.25`, global deterministic NMS IoU `0.5`, explicit unmatched sidecar |
| Verify | `py -m pytest -q` → `53 passed, 4 skipped`; `py -m symbols.validate_gt --self-check` → `1 fixture`; S4 synthetic: 2/2 known, 0 unknown bindings, 0 duplicates; PB opt-in skipped без локальной fixture |
| PR | |
| Basket P02 | S4 → done; следующий разрешённый scope S5 |
| Долг / replan | Pillow/PyMuPDF gate достигнут, поэтому OpenCV не добавлен; customer PB quality остаётся opt-in gate |

