# PHASE S5: Open-set candidates

**Basket:** [P02_SYMBOLS_HARNESS.md](./P02_SYMBOLS_HARNESS.md) · **S5**  
**Источник:** план P02, S5  
**Статус:** done (2026-08-24)  
**Зависит от:** S4  
**Код при составлении scope:** S5 не реализован

---

## Цель

Находить независимые неизвестные кандидаты и повторяющиеся визуальные сигнатуры.

## In scope

- `candidate_detector.py`, `visual_signature.py`, `cluster.py`;
- vector/components/circles/line-embedded/repeats;
- exclusion masks; repeated unknown остаётся `unclassified`;
- метрики negatives и opt-in KR5.

## Out of scope

- угадывание типа неизвестного знака;
- resolver и инженерные связи.

## Acceptance

- [x] повтор неизвестного обнаруживается без ложного type binding;
- [x] text/table/line-intersection masks измеряются;
- [x] miss/duplicate/negative metrics покрыты тестами.

## Verify

```powershell
python -m pytest -q
```

## Harness

**Ask:** выбрать candidate evidence и negative budget.  
**Agent:** реализуй open set S5, неизвестное не классифицируй.

## Checkpoint

| Поле | Значение |
|---|---|
| Done | `symbols/candidate_detector.py`, `visual_signature.py`, `cluster.py`; candidate NMS в `dedupe.py`; negative metrics в `score_gt.py`; `tests/test_symbols_s5.py` |
| Verify | `py -m pytest -q` → 58 passed, 5 skipped; `py -m symbols.validate_gt --self-check` → passed |
| PR | не создавался |
| Basket P02 | S5 → done |
| Долг / replan | Локальный KR5 отсутствует и корректно skipped; synthetic: 2/2 repeated unknown, один unclassified cluster, 0 negative hits. Runtime/PASS-контракты не менялись. |

