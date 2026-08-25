# PHASE D2: Модуль документного pre-pass

**Basket:** [P01_HARNESS.md](./P01_HARNESS.md) · **D2**  
**Источник:** P01 R1–R4 · ТЗ §5.2–5.3 · Ask 2026-08-22  
**Статус:** историческая локальная реализация; в текущем `main` `doc_context.py` отсутствует и заменён связкой `deglyph.py` / `pdf_tables.py` / `service/convert.py`
**Зависит от:** D1  
**Код при составлении scope:** не менялся

> Не восстанавливать эту фазу буквально: JSON-кэш и гейты 90/85 не соответствуют действующему runtime. Нужен отдельный replan только при доказанной нехватке текущих документных кэшей.

---

## Цель

Один вход `doc_context.build(pdf)` собирает глиф-map (гейты 90/85), рамки штампа и шифр комплекта. В выдачу и `run_vlm` ещё не вшито.

## Решения Ask

1. `dataclass DocContext` + `build(path)` + `build_from_doc(doc)` для PDF в памяти.
2. JSON: `to_dict` / `from_dict`; `frames` — список списков.
3. `save` / `load` / `is_fresh` (mtime_ns + size). Вызов из воркера — D3.
4. `build_ios2_md.py` не переводить на модуль в этом витке.
5. ИОС2 в тестах — skip, если файла нет. `extra_text` по умолчанию пустой (VLM ещё нет).

Пороги и правило шифра — копия из `build_ios2_md.py`, не крутить.

## In scope

- `doc_context.py`
- `tests/test_doc_context.py`
- фикстура пути ИОС2 в `tests/conftest.py`
- этот файл + статус в P01

## Out of scope

- `service/worker.py`, `service/convert.py`, `hf_api_bench.py`, `sheet_aware.py`
- правка `build_ios2_md.py`
- HF, Tesseract, смена `kind`

## Acceptance

- [x] Пустой/короткий PDF → `glyph_map == {}`, без исключения
- [x] `01.pdf` (skip если нет) → `glyph_map == {}`
- [x] Гейт ниже порога → map в контексте `{}`, `reject_reason == "gate_below"`
- [x] Три одинаковых шифра → `main_code`; два разных поровну → `None`
- [x] 2 страницы → `frames == []`
- [x] save/load круг; после смены size — не fresh
- [x] D1-тесты зелёные; diff без `run_vlm` / `convert` / `worker`

## Verify

```powershell
cd PTO-work
py -3 -m pytest -q
```

## Harness

**Ask:** закрыт (рекомендации приняты).  
**Agent:** только in scope; не коммитить без запроса; не звать HF.

## Checkpoint

| Поле | Значение |
|------|----------|
| Done | да, 2026-08-22 |
| Verify | `py -3 -m pytest -q` → 17 passed, 1 skipped (полный ИОС2) |
| PR | нет |
| Basket P01 | D2 → done |
| Долг / replan | `test_ios2_does_not_crash` skip: `find_tables` на ~49 стр. не в обязательный verify. Вшивать pre-pass в воркер — D3. |
