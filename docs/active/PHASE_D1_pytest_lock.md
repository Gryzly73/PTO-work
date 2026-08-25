# PHASE D1: Pytest-замок на текущее поведение

**Basket:** [P01_HARNESS.md](./P01_HARNESS.md) · **D1**  
**Источник:** P01 · ТЗ §5.2–5.3 (зафиксировать *как есть*, не чинить)  
**Статус:** локально выполнялась 2026-08-22; `tests/`, `pytest.ini` и зависимость pytest в текущем `main` отсутствуют
**Зависит от:** D0  
**Блокирует:** D2 — без зелёного pytest в D2 не идём  
**Код при составлении scope:** не менялся (2026-08-22)

> Это историческая спецификация, а не действующий gate. Перед восстановлением тестов нужно заново зафиксировать schema 5 и актуальные интерфейсы `service/convert.py`.

---

## Цель

Появился `pytest`, который фиксирует сегодняшние контракты `deglyph`, `pdf_tables`, `convert`, `build_passport`. D2–D6 можно будет отличить от «так и было». Поведение конвейера **не менять**.

## In scope

- каталог тестов (решение на Ask, см. ниже): `PTO-work/tests/` **или** тесты в корне
- `conftest.py`: UTF-8, путь к корню `PTO-work` в `sys.path`
- тесты без смены логики:
  - `deglyph`: короткое слово без двух свидетелей не даёт пару; пустой map, если словарь бедный (`MIN_VOCAB`)
  - `pdf_tables`: слово относится к ячейке по центру глифа; `frame_signatures` пуст при `< 3` страницах
  - `convert.page_layer_text`: garbled → `""`
  - `convert.build_page_markdown`: есть строка `**Файл:**`
  - `sheet_aware.build_passport`: на пустом большом листе сейчас `scheme` — **ожидать текущий kind**, не «правильный»
- фикстуры: крошечный PDF в `tests/fixtures/` **или** skip, если нет `new_files/…`
- не коммитить эталонные комплекты и `hf_runs/`

## Out of scope

- `doc_context.py`, проводка в `worker` / `run_vlm`
- смена `kind`, `USE_ZONE_HINTS`, промптов
- `service.smoke_test` как замена unit-тестов
- вызов HF / Tesseract в обязательном verify (healthcheck OCR — не часть D1)

## Решения Ask (2026-08-22)

1. Тесты в `PTO-work/tests/` + `pytest.ini` (`testpaths = tests`).
2. `new_files/` с диска; нет файла — skip. Обязательные кейсы — PDF в памяти (PyMuPDF).
3. `pytest>=8.0.0` в корневом `requirements.txt`. `service/requirements.txt` не трогали.

## Acceptance

- [x] `python -m pytest -q` из `PTO-work` зелёный (11 passed; `01.pdf` на этой машине есть)
- [x] Есть тест на `**Файл:**`
- [x] Есть тест: garbled слой → пустой `page_layer_text`
- [x] Есть тест-якорь: большой неширокий лист без текста → `kind == "scheme"`
- [x] Runtime конвейера и сервиса не менялся (кроме `requirements.txt`)

## Verify

```powershell
cd PTO-work
python -m pytest -q
git diff --stat
# в diff — tests/, возможно requirements; не hf_api_bench.py / sheet_aware.py / service/*.py
```

## Harness

**Ask:** куда тесты, чтение `new_files/`, pytest в каких requirements.  
**Agent:** реализуй по этому файлу — только in scope; не коммитить без запроса; не звать HF.

## Checkpoint

| Поле | Значение |
|------|----------|
| Done | да, 2026-08-22 |
| Verify | `py -3 -m pytest -q` → 11 passed in 6.17s |
| PR | нет |
| Basket P01 | D1 → done |
| Долг / replan | якорь «пустой большой лист» — неширокий (1200×1000). Широкий (1600×900) сейчас `plan` из‑за `is_wide`. |
