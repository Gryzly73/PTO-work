"""Проверка сведения листа из двух источников — без сервиса и без файлов.

Запуск из корня backend:
  python test_fuse.py

Проверяется то, ради чего модуль и написан: из PDF доезжает всё, чего нет в
чертеже (строки, таблицы, описание), и не доезжает то, что там уже есть.
Иначе сведённый лист либо теряет данные, либо дублирует их вдвое, а модель,
которая по нему отвечает, видит один и тот же факт дважды и считает это
двумя разными.
"""
from __future__ import annotations

import fuse

VECTOR = """### PASS-0 Паспорт листа

- обозначение: 28-ХСА-1/25-КР1
- лист: 22

### PASS-B Текст листа (из DWG)

Схема расположения фундаментов
Ф-1 отметка -2.050
Бетон В25 W6 F150
"""

RASTER = """### PASS-0 Паспорт листа

- обозначение: 28-ХСА-1/25-КР1

### PASS-A Описание листа

На листе показан план фундаментов с осями 1-15.

### PASS-B Тайлы / текст

Схема расположения фундаментов
Ф-1 отметка -2.050
Армирование сеткой С-1 по ГОСТ 23279

| Марка | Количество |
|---|---:|
| Ф-1 | 12 |
| Ф-2 | 8 |
"""


def case_new_lines_added() -> tuple[bool, str]:
    """Строка, которой нет в чертеже, попадает в добор."""
    out = fuse.fuse(VECTOR, RASTER)
    if "ГОСТ 23279" not in out:
        return False, "строка из PDF потеряна"
    return True, "недостающая строка добрана"


def case_known_lines_not_repeated() -> tuple[bool, str]:
    """Строка, которая есть в чертеже, второй раз не печатается."""
    out = fuse.fuse(VECTOR, RASTER)
    if out.count("Бетон В25 W6 F150") != 1:
        return False, "строка чертежа продублирована"
    # «Схема расположения фундаментов» есть в обоих источниках: в доборе её
    # быть не должно.
    after = out.split("Добор из")[-1] if "Добор из" in out else ""
    if "Схема расположения фундаментов" in after.split("#####")[0]:
        return False, "общая строка попала в добор"
    return True, "общее не задвоено"


def case_table_carried() -> tuple[bool, str]:
    """Таблица, которой нет в чертеже, переносится целиком."""
    out = fuse.fuse(VECTOR, RASTER)
    if "| Ф-2 | 8 |" not in out:
        return False, "таблица из PDF потеряна"
    if "Таблицы, найденные только" not in out:
        return False, "таблица не помечена как добранная"
    return True, "таблица перенесена и помечена"


def case_echo_table_dropped() -> tuple[bool, str]:
    """Таблица, пересобранная моделью из того, что и так есть в чертеже.

    У листа-текста модель иногда рисует сетку на пустом месте и раскладывает
    по ней уже известные строки. Переносить такую — значит показать один факт
    дважды, вторым разом в виде таблицы, которой на листе нет.
    """
    echo = RASTER + (
        "\n| | |\n|---|---|\n"
        "| Схема расположения фундаментов | |\n"
        "| Ф-1 отметка -2.050 | |\n"
    )
    out = fuse.fuse(VECTOR, echo)
    tables = out.split("Таблицы, найденные только")
    if len(tables) > 1 and "Схема расположения фундаментов | |" in tables[1]:
        return False, "пересказанная таблица перенесена"
    return True, "пересказанная таблица отброшена"


def case_reread_dropped() -> tuple[bool, str]:
    """Известная строка, прочитанная моделью с ошибкой, в добор не идёт.

    Это опаснее любого мусора: «ООО "ХОЛДИНГ СТРОИТЕЛЬНЫЙ КОМПЛЕКС-1"» вместо
    «АЛЬЯНС-1» читается как ещё один заказчик, а не как ошибка узнавания.
    """
    raster = RASTER + "\nСхема расположения фундаМентов\nФ-1 отметка -2.O5O\n"
    out = fuse.fuse(VECTOR, raster)
    after = out.split("Добор из")[-1].split("#####")[0] if "Добор из" in out else ""
    if "фундаМентов" in after:
        return False, "перечитанная строка попала в добор"
    return True, "перечтение отброшено"


def case_description_kept() -> tuple[bool, str]:
    """Описание листа из PDF идёт отдельно и помечено как работа модели."""
    out = fuse.fuse(VECTOR, RASTER)
    if "план фундаментов с осями 1-15" not in out:
        return False, "описание потеряно"
    if "читала модель" not in out:
        return False, "описание не помечено как пересказ модели"
    return True, "описание отдельным блоком"


def case_counts_reported() -> tuple[bool, str]:
    """Счётная сверка источников есть и считает пересечение."""
    counts = fuse.compare(VECTOR, RASTER)
    if counts["общих"] < 3:
        return False, f"пересечение не найдено: {counts}"
    if counts["только в PDF"] < 1:
        return False, f"новое из PDF не найдено: {counts}"
    out = fuse.fuse(VECTOR, RASTER)
    if "Сверка источников" not in out:
        return False, "сверки нет в отчёте"
    return True, f"сверка считает: {counts}"


def case_single_source_untouched() -> tuple[bool, str]:
    """Один источник — лист остаётся ровно таким, каким был."""
    if fuse.fuse(VECTOR, "") != VECTOR:
        return False, "лист изменён без второго источника"
    if fuse.fuse("", RASTER) != RASTER:
        return False, "лист PDF изменён без чертежа"
    return True, "одиночный источник не трогается"


def case_bold_survives() -> tuple[bool, str]:
    """Жирный заголовок из PDF не теряет разметку в доборе.

    Маркер списка снимается, а звёздочки жирного шрифта — нет: иначе в отчёт
    едет «Цветовая разметка:**» с висящими звёздочками.
    """
    raster = RASTER + "\n- **Цветовая разметка таблицы:** зелёный\n"
    out = fuse.fuse(VECTOR, raster)
    if "**Цветовая разметка таблицы:**" not in out:
        return False, "разметка жирного испорчена"
    return True, "жирный шрифт цел"


def main() -> int:
    cases = [
        ("добор недостающих строк", case_new_lines_added),
        ("общее не дублируется", case_known_lines_not_repeated),
        ("таблица из PDF", case_table_carried),
        ("пересказанная таблица", case_echo_table_dropped),
        ("перечитанная строка", case_reread_dropped),
        ("описание листа", case_description_kept),
        ("счётная сверка", case_counts_reported),
        ("один источник", case_single_source_untouched),
        ("жирный шрифт в доборе", case_bold_survives),
    ]
    failed = 0
    for title, case in cases:
        ok, message = case()
        print(f"[{'OK' if ok else 'FAIL'}] {title}: {message}")
        if not ok:
            failed += 1
    if failed:
        print(f"\nПровалено: {failed}")
        return 1
    print("\nВсе проверки прошли.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
