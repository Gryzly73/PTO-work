"""Проверка сведения источников в один отчёт — без сервиса и без файлов.

Запуск из корня backend:
  python test_bundle.py

Проверяется то, ради чего модуль и написан: лист, пришедший и из чертежа, и из
PDF, попадает в отчёт один раз и берётся из DWG; листы текстовой части не
смешиваются с листами графической, хотя номера у них совпадают; лист без
номера не теряется.
"""
from __future__ import annotations

import bundle
import stamp as stamp_mod


def variant(
    source: str,
    kind: str,
    index: int,
    code: str,
    sheet: str,
    title: str = "",
    stage: str = "П",
) -> bundle.Variant:
    return bundle.Variant(
        source_name=source,
        source_kind=kind,
        index=index,
        stamp=stamp_mod.Stamp(
            code=code, sheet=sheet, title=title, stage=stage, source=kind
        ),
    )


def case_one_sheet_two_sources() -> tuple[bool, str]:
    """Один лист из DWG и PDF: в отчёте он один, истина — чертёж."""
    sections = bundle.build(
        [
            variant("Фундаменты.dwg", "DWG", 1, "28-ХСА-1/25-КР1", "19", "Схема Ф"),
            variant("Альбом.pdf", "PDF", 19, "28-ХСА-1/25-КР1", "19", "Схема Ф"),
        ]
    )
    document = sections[0].documents[0]
    if len(document.sheets) != 1:
        return False, f"листов {len(document.sheets)}, ждали 1"
    entry = document.sheets[0]
    if entry.main.source_kind != "DWG":
        return False, f"истина взята из {entry.main.source_kind}, ждали DWG"
    if len(entry.others) != 1:
        return False, "второй источник потерян"
    return True, "лист сведён, истина — DWG"


def case_text_and_graphic_parts() -> tuple[bool, str]:
    """Лист 19 записки и лист 19 чертежей — разные листы разных документов."""
    sections = bundle.build(
        [
            variant("Фундаменты.dwg", "DWG", 1, "28-ХСА-1/25-КР1", "19", "Схема Ф"),
            variant("Записка.pdf", "PDF", 19, "28-ХСА-1/25-КР1.ПЗ", "19"),
        ]
    )
    if len(sections) != 1:
        return False, f"разделов {len(sections)}, ждали 1"
    section = sections[0]
    if len(section.documents) != 2:
        return False, f"документов {len(section.documents)}, ждали 2"
    graphic, text = section.documents
    if graphic.is_text_part or not text.is_text_part:
        return False, "графическая и текстовая части перепутаны местами"
    if len(graphic.sheets) != 1 or len(text.sheets) != 1:
        return False, "листы разных документов слиты в один"
    return True, "текстовая и графическая части разделены"


def case_conflict_reported() -> tuple[bool, str]:
    """Разные наименования одного листа попадают в расхождения."""
    sections = bundle.build(
        [
            variant("Планы.dwg", "DWG", 1, "28-ХСА-1/25-КР1", "3", "План на отм. 0.000"),
            variant("Альбом.pdf", "PDF", 3, "28-ХСА-1/25-КР1", "3", "План этажа"),
        ]
    )
    entry = sections[0].documents[0].sheets[0]
    conflicts = entry.conflicts()
    if not conflicts:
        return False, "расхождение наименований не замечено"
    label, accepted, other, _ = conflicts[0]
    if accepted != "План на отм. 0.000":
        return False, f"принято {accepted!r}, ждали версию из DWG"
    if not bundle.conflicts_markdown(sections[0].documents[0]):
        return False, "расхождение не попало в отчёт"
    return True, f"расхождение найдено: {label}"


def case_same_title_not_a_conflict() -> tuple[bool, str]:
    """Различия в регистре и кавычках — не расхождение."""
    sections = bundle.build(
        [
            variant("Планы.dwg", "DWG", 1, "28-ХСА-1/25-КР1", "3", "План «А»"),
            variant("Альбом.pdf", "PDF", 3, "28-ХСА-1/25-КР1", "3", "план А "),
        ]
    )
    entry = sections[0].documents[0].sheets[0]
    if entry.conflicts():
        return False, "разный регистр принят за расхождение"
    return True, "регистр и кавычки не считаются расхождением"


def case_unnumbered_kept() -> tuple[bool, str]:
    """Лист без прочитанного номера остаётся в отчёте."""
    sections = bundle.build(
        [
            variant("Планы.dwg", "DWG", 1, "28-ХСА-1/25-КР1", "3", "План"),
            variant("Планы.dwg", "DWG", 2, "28-ХСА-1/25-КР1", "", "Узлы"),
        ]
    )
    document = sections[0].documents[0]
    if len(document.unnumbered) != 1:
        return False, "лист без номера потерян"
    if "номер листа не прочитан" not in bundle.contents_markdown(document) and (
        "Узлы" not in bundle.contents_markdown(document)
    ):
        return False, "лист без номера не показан в составе"
    return True, "лист без номера сохранён"


def case_sheet_order() -> tuple[bool, str]:
    """Листы идут по номеру комплекта, а не по порядку страниц в файле."""
    sections = bundle.build(
        [
            variant("Второй.dwg", "DWG", 1, "28-ХСА-1/25-КР1", "22", "Ф-1"),
            variant("Первый.dwg", "DWG", 1, "28-ХСА-1/25-КР1", "3", "План"),
            variant("Третий.dwg", "DWG", 1, "28-ХСА-1/25-КР1", "19", "Схема"),
        ]
    )
    numbers = [e.number for e in sections[0].documents[0].sheets]
    if numbers != ["3", "19", "22"]:
        return False, f"порядок листов {numbers}"
    return True, "листы в порядке комплекта"


def case_code_from_processed_body() -> tuple[bool, str]:
    """У растрового PDF шифр берётся из разобранного листа.

    У печатного альбома текстового слоя нет вовсе — у ПЗ комплекта
    «Жуковский» это ноль знаков на всех восьми страницах. Без этого запасного
    пути лист остаётся без шифра, не связывается с двойником из чертежа, и
    сведение источников не происходит именно там, где нужнее всего.
    """
    body = "\n".join(
        [
            "### PASS-0 Паспорт листа",
            "",
            "- лист: 3",
            "",
            "### PASS-B Текст листа",
            "",
            "Шифр 28-ХСА-1/25-КР1",
            "Сварка по ГОСТ 14098-2014, бетон В25 W6 F150",
        ]
    )
    code, number = bundle.code_from_body(body)
    if code != "28-ХСА-1/25-КР1":
        return False, f"шифр прочитан как {code!r}"
    if number != "3":
        return False, f"номер листа прочитан как {number!r}"
    if bundle.code_from_body("Сварка по ГОСТ 14098-2014")[0]:
        return False, "ссылка на ГОСТ принята за шифр"
    return True, "шифр и номер взяты из разбора, ГОСТ отвергнут"


def main() -> int:
    cases = [
        ("лист из двух источников", case_one_sheet_two_sources),
        ("текстовая и графическая части", case_text_and_graphic_parts),
        ("расхождение наименований", case_conflict_reported),
        ("регистр — не расхождение", case_same_title_not_a_conflict),
        ("лист без номера", case_unnumbered_kept),
        ("порядок листов", case_sheet_order),
        ("шифр из разбора листа", case_code_from_processed_body),
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
