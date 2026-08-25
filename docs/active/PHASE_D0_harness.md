# PHASE D0: Operational harness P01

**Basket:** [P01_HARNESS.md](./P01_HARNESS.md) · **D0**  
**Источник:** ревью 2026-08-22 + план P0/P1 по методологии harness  
**Статус:** исторический checkpoint (2026-08-22); документация сохранена в `main`
**Код при составлении / исполнении:** не менялся  
**Блокирует:** D1 только порядком (жёсткой связи по файлам нет)

> Файл фиксирует ход локального basket 2026-08-22. Статусы следующих фаз сверять с актуальной таблицей в [P01_HARNESS.md](./P01_HARNESS.md).

---

## Цель

Зафиксировать очередь D0–D6, границы basket, решения R1–R4 и фикстуры, чтобы следующий виток не начинался с нуля в чате.

## In scope

- `docs/active/P01_HARNESS.md`
- этот файл
- `docs/active/PHASE_TEMPLATE.md`
- `docs/active/PHASE_D1_pytest_lock.md` (scope следующего витка, без тестов)

## Out of scope

- любой `.py`
- PHASE_D2…D6 (писать перед стартом того витка)
- правки `CLAUDE.md` / `README.md`
- commit (только по явной просьбе)

## Acceptance

- [x] В P01 есть очередь, out of scope всего basket, R1–R4
- [x] Записан факт: в репо нет pytest; целевая команда после D1 — `python -m pytest -q`
- [x] Фикстуры названы: ИОС2, `01.pdf` 1–5, прогон `20260819_132438_qwen3vl-32b_sheetaware`
- [x] `git diff` только docs

## Verify

```powershell
cd PTO-work
git status --short docs/active
# ожидается только docs/active/*.md
```

## Harness

**Ask:** не требовался (исключение: docs-only).  
**Agent:** запиши harness, код не трогай.

## Checkpoint

| Поле | Значение |
|------|----------|
| Done | да, 2026-08-22 |
| Verify | четыре файла в `docs/active/`; runtime не менялся |
| PR | нет |
| Basket P01 | D0 → done |
| Следующий | D1 — сначала Ask по [PHASE_D1_pytest_lock.md](./PHASE_D1_pytest_lock.md) |
