#!/usr/bin/env python
"""Измеритель раздутости и достоверности вывода конвейера.

Зачем. Существующие метрики (compare_to_etalon.py, score_real_testset.py)
отвечают на вопрос «сколько из эталона мы нашли». Они ничего не говорят о том,
сколько текста мы на это потратили и сколько в выводе выдумано. А именно это
сейчас болит: один лист-чертёж выходит на 120 тыс. символов, где один и тот же
факт пересказан четырьмя проходами.

Считаем на лист:

  chars          объём вывода
  facts          уникальные «якоря»: шифры, марки, диаметры, отметки, метки сетей
  chars_per_fact плотность. Чем меньше, тем меньше воды на единицу информации
  dup_lines      доля строк, встретившихся не первый раз
  hedges         маркеры домысла («вероятно», «возможно») — запрещены регламентом
  norms          ссылки на ГОСТ/СП/СНиП — модель не должна пояснять лист своими знаниями
  echo           строки промпта, попавшие в ответ (протечка шаблона)
  unsupported    доля фактов, которых НЕТ в текстовом слое PDF (кандидаты в выдумку)

Последняя метрика работает только там, где слой читается (при необходимости
чинится deglyph). Это не приговор факту: на скане слоя нет вовсе, а на чертеже
часть подписей лежит картинкой. Поэтому unsupported — сигнал, а не оценка.

Использование:
    python -X utf8 measure_output.py <run_dir|md-файл> [--pdf FILE] [--json OUT]
    python -X utf8 measure_output.py hf_runs/A hf_runs/B --pdf new_files/x.pdf
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# --- что считаем «фактом» ---------------------------------------------------
# Только то, что инженер ПТО будет сверять с ТЗ: шифры, марки, размеры, отметки.
# Обычные слова не считаем — иначе метрика превратится в счётчик прозы.
FACT_PATTERNS = [
    r"\b\d{2}-[А-ЯA-Z]{2,4}-[\d/]+(?:-[А-ЯA-Z0-9]+)?\b",     # 28-ХСА-1/25-ИОС2
    r"[∅ØøφΦ]\s?\d{1,4}(?:[х×]\d{1,3})?",                    # ∅80, ∅108х4
    r"\bDN\s?\d{1,4}\b|\bDy\s?\d{1,4}\b",                    # DN100
    r"\b[А-Я]{1,4}[а-я]?[-–]?\d{1,3}(?:[/-]\d{1,3})?\b",     # ВСХд-15, ИГЭ-12, УП-3
    r"\b[ВТКW]\d\b",                                          # В1, Т3, К1, W1
    r"\bСкв\.?\s?\d{1,4}\b",                                  # Скв. 353
    r"\b[-+]?\d{1,3}[.,]\d{3}\b",                             # отметки 0.000, -2.350
    r"\bМ\s?1\s?:\s?\d{1,4}\b",                               # М1:500
    r"\b\d{1,3}[.,]\d{1,2}\s?(?:м|мм|м3/сут|л/с|кПа|МПа|кг/м3|°|%)\b",
]
FACT_RE = re.compile("|".join(FACT_PATTERNS))

HEDGE_RE = re.compile(
    r"\b(?:вероятно|возможно|по-видимому|скорее всего|предположительно|"
    r"судя по всему|может быть|похоже,)\b",
    re.I,
)
NORM_RE = re.compile(r"\b(?:ГОСТ|СП|СНиП|СанПиН|ТУ)\s?[\d.\-–]{2,}", re.I)

# Куски шаблонов промптов, которые модель иногда печатает как часть ответа.
ECHO_MARKERS = (
    "ПОЛНЫЙ ЧИТАЕМЫЙ ТЕКСТ (дополнительно)",
    "структура + ПОЛНЫЙ текст разделов",
    "не структура «о чём таблица»",
    "эталон для Q&A",
    "ЗАПРЕЩЕНО: таблица из голых номеров",
    "уникальные; повторы — сводкой",
    "ПОЛНЫЙ markdown со всеми строками",
)

PAGE_SPLIT_RE = re.compile(r"(?m)^## Страница (\d+)\s*$")


def normalize_fact(raw: str) -> str:
    """Схлопывает написания одного и того же факта.

    «Скв. 353» = «Скв.353», «0,001» = «0.001» (модель произвольно меняет
    десятичный разделитель), «Ø80» = «∅80».
    """
    key = re.sub(r"\s+", "", raw).replace("Ø", "∅").replace("O", "∅")
    return key.replace(",", ".").lower()


def facts_of(text: str) -> set[str]:
    return {normalize_fact(m.group(0)) for m in FACT_RE.finditer(text)}


def dup_line_ratio(text: str, min_len: int = 12) -> float:
    """Доля строк, встретившихся не первый раз (без пустых и разметки)."""
    seen: set[str] = set()
    total = dup = 0
    for line in text.splitlines():
        key = re.sub(r"\s+", " ", line.strip())
        if len(key) < min_len or key.startswith(("---", "|", "```", "#")):
            continue
        total += 1
        if key in seen:
            dup += 1
        seen.add(key)
    return round(dup / total, 3) if total else 0.0


def pdf_layer_facts(pdf: Path) -> dict[int, set[str]]:
    """Факты, подтверждённые текстовым слоем PDF (с починкой ToUnicode).

    Пустой словарь — значит слоя нет (скан) и сверять не с чем.
    """
    try:
        import fitz
    except ImportError:
        return {}
    try:
        doc = fitz.open(pdf)
    except Exception:
        return {}

    mapping: dict[str, str] = {}
    try:
        from deglyph import build_mapping

        mapping = build_mapping(doc)
    except Exception:
        mapping = {}

    out: dict[int, set[str]] = {}
    for i in range(doc.page_count):
        raw = doc[i].get_text("text") or ""
        if mapping:
            from deglyph import decode

            raw = decode(raw, mapping)
        if len(raw.strip()) < 50:
            continue
        out[i + 1] = facts_of(raw)
    doc.close()
    return out


def measure_text(text: str, layer_facts: set[str] | None = None) -> dict:
    facts = facts_of(text)
    chars = len(text)
    row = {
        "chars": chars,
        "facts": len(facts),
        "chars_per_fact": round(chars / len(facts), 1) if facts else None,
        "dup_lines": dup_line_ratio(text),
        "hedges": len(HEDGE_RE.findall(text)),
        "norms": len(NORM_RE.findall(text)),
        "echo": sum(text.count(m) for m in ECHO_MARKERS),
    }
    if layer_facts:
        missing = facts - layer_facts
        row["unsupported"] = round(len(missing) / len(facts), 3) if facts else None
        row["unsupported_examples"] = sorted(missing)[:8]
    return row


def split_pages(md: str) -> dict[int, str]:
    """Режет out.md по контракту «## Страница N»."""
    parts = PAGE_SPLIT_RE.split(md)
    if len(parts) < 3:
        return {}
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts), 2)}


def collect(target: Path) -> dict[int, str]:
    """Страницы прогона: из каталога pages/, из out.md или из одиночного md."""
    if target.is_dir():
        pages_dir = target / "pages"
        if pages_dir.is_dir():
            out = {}
            for item in sorted(pages_dir.glob("page_*.md")):
                try:
                    out[int(item.stem.split("_")[1])] = item.read_text(encoding="utf-8")
                except (IndexError, ValueError):
                    continue
            if out:
                return out
        out_md = target / "out.md"
        if out_md.exists():
            return split_pages(out_md.read_text(encoding="utf-8"))
        return {}
    text = target.read_text(encoding="utf-8")
    return split_pages(text) or {0: text}


def measure_run(target: Path, layer: dict[int, set[str]]) -> dict:
    pages = collect(target)
    rows = {
        num: measure_text(text, layer.get(num))
        for num, text in sorted(pages.items())
    }
    totals = {
        "pages": len(rows),
        "chars": sum(r["chars"] for r in rows.values()),
        "facts": sum(r["facts"] for r in rows.values()),
        "hedges": sum(r["hedges"] for r in rows.values()),
        "norms": sum(r["norms"] for r in rows.values()),
        "echo": sum(r["echo"] for r in rows.values()),
    }
    totals["chars_per_fact"] = (
        round(totals["chars"] / totals["facts"], 1) if totals["facts"] else None
    )
    return {"target": str(target), "pages": rows, "totals": totals}


def print_report(result: dict) -> None:
    print(f"\n=== {result['target']}")
    head = f"{'лист':>5} {'символов':>9} {'фактов':>7} {'симв/факт':>10} " \
           f"{'дубли':>6} {'домысл':>7} {'нормы':>6} {'эхо':>4} {'бездок':>7}"
    print(head)
    print("-" * len(head))
    for num, row in result["pages"].items():
        print(
            f"{num:>5} {row['chars']:>9} {row['facts']:>7} "
            f"{str(row['chars_per_fact']):>10} {row['dup_lines']:>6} "
            f"{row['hedges']:>7} {row['norms']:>6} {row['echo']:>4} "
            f"{str(row.get('unsupported', '—')):>7}"
        )
    t = result["totals"]
    print("-" * len(head))
    print(
        f"{'ИТОГО':>5} {t['chars']:>9} {t['facts']:>7} "
        f"{str(t['chars_per_fact']):>10} {'':>6} {t['hedges']:>7} "
        f"{t['norms']:>6} {t['echo']:>4}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("targets", nargs="+", type=Path, help="каталоги прогонов или md")
    ap.add_argument("--pdf", type=Path, help="исходный PDF для сверки фактов со слоем")
    ap.add_argument("--json", type=Path, help="куда сложить машинный отчёт")
    args = ap.parse_args()

    layer: dict[int, set[str]] = {}
    if args.pdf:
        layer = pdf_layer_facts(args.pdf)
        covered = len(layer)
        print(
            f"Текстовый слой PDF: пригоден на {covered} листах"
            + (" — сверка фактов включена" if covered else " — сверки не будет")
        )

    results = [measure_run(t, layer) for t in args.targets]
    for result in results:
        print_report(result)

    if len(results) > 1:
        base, *rest = results
        print("\n=== сравнение с первым прогоном")
        for other in rest:
            b, o = base["totals"], other["totals"]
            def delta(key: str) -> str:
                if not b.get(key) or not o.get(key):
                    return "—"
                return f"{(o[key] - b[key]) / b[key] * 100:+.0f}%"
            print(
                f"{other['target']}: объём {delta('chars')}, "
                f"фактов {delta('facts')}, плотность {delta('chars_per_fact')}"
            )

    if args.json:
        args.json.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nОтчёт: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
