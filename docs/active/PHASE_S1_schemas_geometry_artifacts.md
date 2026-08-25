# PHASE S1: Schemas, geometry, artifacts

**Basket:** [P02_SYMBOLS_HARNESS.md](./P02_SYMBOLS_HARNESS.md) · **S1**  
**Источник:** план P02, S1  
**Статус:** done (2026-08-24)  
**Зависит от:** S0  
**Код при составлении scope:** S1 не реализован

---

## Цель

Ввести runtime-сущности и устойчивые sidecar-контракты без подключения pipeline.

## In scope

- `schema.py`, `geometry.py`, `artifacts.py`, версионированные entities;
- канонические координаты PDF points и явные преобразования;
- атомарная запись sidecars, round-trip/geometry/atomicity tests.

## Out of scope

- поиск легенды и символов; изменение runtime и public JSON;
- координаты конкретных страниц.

## Acceptance

- [x] versioned entities валидируются и round-trip стабилен;
- [x] геометрия использует PDF points;
- [x] sidecar пишется атомарно; runtime неизменен.

## Verify

```powershell
python -m pytest -q
```

## Harness

**Ask:** согласовать versioning и atomic-write semantics.  
**Agent:** реализуй только scope S1; не подключай pipeline и сеть.

## Checkpoint

| Поле | Значение |
|---|---|
| Done | да, 2026-08-24 |
| Файлы | `symbols/schema.py`, `symbols/geometry.py`, `symbols/artifacts.py`, `symbols/__init__.py`, `tests/test_symbols_s1.py`, этот checkpoint |
| Verify | `py -m pytest -q` → `36 passed, 1 skipped in 10.20s`; IDE lint → ошибок нет |
| Контракты | schema version 1; canonical bbox в page-relative PDF points; rotation 0/90/180/270 соответствует `Pillow.rotate(-rot, expand=True)` из `hf_api_bench.py`; JSON/crops заменяются через temp + fsync + `os.replace` |
| PR | нет |
| Basket P02 | S1 → done; S2 → pending |
| Долг / replan | multi-file sidecars атомарны пофайлово, не транзакционно как набор; подключение к runtime отложено до S8 |

