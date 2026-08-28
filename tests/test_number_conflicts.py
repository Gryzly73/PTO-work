"""Сверка источников по числам: где чертёж и альбом разошлись цифрой.

Для ПТО это главное в сведении комплекта. Наименование листа расходится
редко, а вот отметка, количество или марка в альбоме, отпечатанном с более
старой редакции чертежа, — обычное дело, и именно это проверяющий обязан
заметить.

Правило простое и объяснимое: строка, которая в обоих источниках написана
теми же словами, но с другими числами. Всё остальное — разница чтения, её
считает `fuse.compare()`, а не эта сверка.
"""
from __future__ import annotations

import bundle
import fuse
import stamp as stamp_mod


def _body(lines: list[str]) -> str:
    return "### PASS-B Текст листа\n\n" + "\n".join(lines) + "\n"


def test_finds_changed_number() -> None:
    dwg = _body(["Отметка низа фундамента -2.050", "Бетон B25"])
    pdf = _body(["Отметка низа фундамента -2.150", "Бетон B25"])
    found = fuse.number_conflicts(dwg, pdf)
    assert len(found) == 1
    phrase, left, right = found[0]
    assert "отметка низа" in phrase
    assert "-2.050" in left and "-2.150" in right


def test_finds_changed_quantity_and_mark() -> None:
    dwg = _body(["Фундамент Ф-1, количество 12 шт.", "Свая С-80.30, 44 шт."])
    pdf = _body(["Фундамент Ф-2, количество 12 шт.", "Свая С-80.30, 46 шт."])
    found = fuse.number_conflicts(dwg, pdf)
    assert len(found) == 2, found


def test_identical_lines_are_not_conflicts() -> None:
    same = _body(["Отметка низа -2.050", "Бетон B25, арматура А500С", "Длина 145,5 м"])
    assert fuse.number_conflicts(same, same) == []


def test_comma_and_dot_are_the_same_number() -> None:
    dwg = _body(["Общая длина свай 145,5 м"])
    pdf = _body(["Общая длина свай 145.5 м"])
    assert fuse.number_conflicts(dwg, pdf) == []


def test_trailing_zeros_are_the_same_number() -> None:
    dwg = _body(["Толщина плиты 200 мм при отметке -2.500"])
    pdf = _body(["Толщина плиты 200 мм при отметке -2.5"])
    assert fuse.number_conflicts(dwg, pdf) == []


def test_line_only_in_one_source_is_not_a_conflict() -> None:
    """Строка, которой нет во втором источнике, — разница чтения, не редакции."""
    dwg = _body(["Отметка низа -2.050", "Узел 3 по ГОСТ 21.101 лист 12"])
    pdf = _body(["Отметка низа -2.050"])
    assert fuse.number_conflicts(dwg, pdf) == []


def test_repeated_phrase_is_skipped() -> None:
    """Одна формулировка столбцом таблицы: какое значение какому — неизвестно."""
    dwg = _body(["Отметка низа плиты 1.100", "Отметка низа плиты 2.200"])
    pdf = _body(["Отметка низа плиты 1.150", "Отметка низа плиты 2.250"])
    assert fuse.number_conflicts(dwg, pdf) == [], "пару выдавать наугад нельзя"


def test_short_and_wordless_lines_are_skipped() -> None:
    dwg = _body(["1 2 3", "12", "- 5 -"])
    pdf = _body(["4 5 6", "13", "- 7 -"])
    assert fuse.number_conflicts(dwg, pdf) == []


def test_table_rows_are_compared() -> None:
    dwg = _body(["| Фундамент Ф-1 | глубина заложения 2.050 |"])
    pdf = _body(["| Фундамент Ф-1 | глубина заложения 2.150 |"])
    found = fuse.number_conflicts(dwg, pdf)
    assert len(found) == 1 and "2.050" in found[0][1]


def test_service_lines_are_ignored() -> None:
    dwg = _body(["### PASS-A Карта листа", "_Сверка источников: 12 из 14_"])
    pdf = _body(["### PASS-A Карта листа", "_Сверка источников: 13 из 14_"])
    assert fuse.number_conflicts(dwg, pdf) == []


def test_limit_holds() -> None:
    # Формулировки разные, иначе все строки схлопнутся в один скелет и
    # сравнение их пропустит — см. test_repeated_phrase_is_skipped.
    names = ["низа", "верха", "обреза", "подошвы", "оси", "площадки", "пола"]
    dwg = _body([f"Отметка {n} фундамента равна {i}.000" for i, n in enumerate(names)])
    pdf = _body([f"Отметка {n} фундамента равна {i}.500" for i, n in enumerate(names)])
    assert len(fuse.number_conflicts(dwg, pdf)) == len(names)
    assert len(fuse.number_conflicts(dwg, pdf, limit=3)) == 3


# ── сведение листа целиком ───────────────────────────────────────────────────


def _variant(tmp_path, name, kind, index, body, *, title="Фундаменты"):
    run = tmp_path / f"run-{name}"
    (run / "pages").mkdir(parents=True, exist_ok=True)
    (run / "pages" / f"page_{index:04d}.md").write_text(body, encoding="utf-8")
    return bundle.Variant(
        source_name=name,
        source_kind=kind,
        index=index,
        stamp=stamp_mod.Stamp(
            code="28-ХСА-1/25-КР1", sheet="22", title=title, stage="П", source=kind
        ),
        run_dir=str(run),
    )


def test_sheet_reports_number_conflict(tmp_path) -> None:
    dwg = _variant(tmp_path, "фундаменты.dwg", "DWG", 1,
                   _body(["Отметка низа фундамента -2.050"]))
    pdf = _variant(tmp_path, "альбом.pdf", "PDF", 22,
                   _body(["Отметка низа фундамента -2.150"]))
    entry = bundle.SheetEntry(number="22", variants=[dwg, pdf])
    found = entry.conflicts()
    assert len(found) == 1
    label, left, right, source = found[0]
    assert label.startswith("числа в строке")
    assert "-2.050" in left and "-2.150" in right
    assert source == "альбом.pdf"
    # Расхождение видно и в самом листе, и в сводке по документу.
    assert "Расхождения между источниками" in entry.fused_body()
    document = bundle.Document(code="28-ХСА-1/25-КР1", sheets=[entry])
    assert "-2.150" in bundle.conflicts_markdown(document)


def test_two_readings_of_one_drawing_are_not_compared(tmp_path) -> None:
    """Два DWG одного листа расходиться редакцией не могут — только чтением."""
    first = _variant(tmp_path, "а.dwg", "DWG", 1, _body(["Отметка низа -2.050"]))
    second = _variant(tmp_path, "б.dwg", "DWG", 1, _body(["Отметка низа -2.150"]))
    entry = bundle.SheetEntry(number="22", variants=[first, second])
    assert [c for c in entry.conflicts() if c[0].startswith("числа")] == []


def test_stamp_conflicts_still_work(tmp_path) -> None:
    dwg = _variant(tmp_path, "фундаменты.dwg", "DWG", 1, _body(["Отметка -2.050"]),
                   title="Фундаменты Ф-1")
    pdf = _variant(tmp_path, "альбом.pdf", "PDF", 22, _body(["Отметка -2.050"]),
                   title="Фундаменты Ф-2")
    entry = bundle.SheetEntry(number="22", variants=[dwg, pdf])
    labels = [c[0] for c in entry.conflicts()]
    assert "наименование листа" in labels


def test_conflicts_are_computed_once(tmp_path, monkeypatch) -> None:
    dwg = _variant(tmp_path, "ф.dwg", "DWG", 1, _body(["Отметка низа -2.050"]))
    pdf = _variant(tmp_path, "а.pdf", "PDF", 22, _body(["Отметка низа -2.150"]))
    entry = bundle.SheetEntry(number="22", variants=[dwg, pdf])
    calls = {"n": 0}
    original = fuse.number_conflicts

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(fuse, "number_conflicts", counted)
    entry.conflicts()
    entry.conflicts()
    assert calls["n"] == 1, "сверка не должна пересчитываться на каждый вызов"
