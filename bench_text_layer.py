"""Сколько текстового слоя PDF доезжает до листа — без модели.

Текстовый путь конвейера не читает картинку: текст и таблицы листа
собираются из слоя PDF (`service/flow.py`, `pdf_tables.py`, `deglyph.py`).
Ошибиться в цифрах он не может, но может ПОТЕРЯТЬ: блок, не попавший в
поток; таблицу, не собравшуюся в сетку; слово, не восстановленное из битого
ToUnicode. Здесь каждая страница документа собирается тем же кодом, что в
сервисе (`page_to_frontend` без вывода модели), и выход сверяется с сырым
слоем той же страницы теми же метриками, что и `score_vs_pdftext`:

  * полнота слов и чисел — сколько из слоя дошло до листа;
  * достоверность чисел — доля чисел листа, которых в слое нет (у текстового
    пути должна быть ~100 %: цифрам неоткуда взяться, кроме слоя);
  * страницы без пригодного слоя — отдельным списком: их этот путь не
    покрывает, они уходят в модель.

Запуск:

    python bench_text_layer.py "путь/к/документу.pdf" [--out bench_text]

Результат: `<out>/<имя>.json` и строка-итог в stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from score_vs_pdftext import score_page, tokens_of  # noqa: E402
from service.convert import page_to_frontend  # noqa: E402


def _avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def bench(pdf: Path) -> dict:
    with fitz.open(pdf) as doc:
        total = doc.page_count
        raw_layers = [doc[i].get_text() for i in range(total)]
    pages = []
    for number in range(1, total + 1):
        page = page_to_frontend(
            page_number=number, file_name=pdf.name, raw_page_md="", pdf_path=pdf
        )
        layer = page["extractedText"] or ""
        # Сверяем содержимое листа (поток + таблицы), а не всю страницу с
        # шапкой «Информация о листе».
        md = page["markdown"]
        body = md.split("## Информация о листе")[0]
        # Заголовок «# Лист N» — наш, а не документа: его номер считался
        # «числом, которого нет в слое», и достоверность на текстовых
        # страницах показывала 80–97 % при нуле настоящих расхождений.
        body = "\n".join(
            ln for ln in body.splitlines() if not ln.startswith("# Лист ")
        )
        raw = raw_layers[number - 1]
        rw, rn, _ = tokens_of(raw)
        usable = len(layer.strip()) >= 40 or len(raw.strip()) >= 40
        scored = score_page(layer if layer.strip() else raw, body) if usable else None
        pages.append(
            {
                "page": number,
                "kind": page["kind"],
                "trust": page["trust"]["level"],
                "raw_chars": len(raw),
                "layer_chars": len(layer),
                "body_chars": len(body),
                "tables": len(page["tables"]),
                "raw_words": len(rw),
                "raw_nums": len(rn),
                "token_recall": scored and scored["token_recall"],
                "num_recall": scored and scored["num_recall"],
                "num_precision": scored and scored["num_precision"],
                "missed_words": scored["missed_words"][:8] if scored else [],
                "missed_nums": scored["missed_nums"][:8] if scored else [],
            }
        )
    usable = [p for p in pages if p["token_recall"] is not None]
    summary = {
        "pdf": str(pdf),
        "pages": total,
        "pages_with_layer": len(usable),
        "pages_without_layer": [p["page"] for p in pages if p["token_recall"] is None],
        "token_recall": _avg([p["token_recall"] for p in usable]),
        "num_recall": _avg([p["num_recall"] for p in usable]),
        "num_precision": _avg([p["num_precision"] for p in usable]),
        "pages_full": sum(
            1 for p in usable if (p["token_recall"] or 0) >= 99.5 and (p["num_recall"] or 100) >= 99.5
        ),
        "pages_below_90": [
            p["page"] for p in usable if (p["token_recall"] or 0) < 90 or (p["num_recall"] or 100) < 90
        ],
        "tables": sum(p["tables"] for p in pages),
        "kinds": {k: sum(1 for p in pages if p["kind"] == k) for k in ("text", "table", "drawing", "mixed")},
        "per_page": pages,
    }
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pdf")
    ap.add_argument("--out", default="bench_text")
    args = ap.parse_args()
    pdf = Path(args.pdf)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    s = bench(pdf)
    (out / (pdf.stem + ".json")).write_text(
        json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(
        f"{pdf.name}: страниц {s['pages']}, со слоем {s['pages_with_layer']}, "
        f"полнота слов {s['token_recall']} %, чисел {s['num_recall']} %, "
        f"достоверность чисел {s['num_precision']} %, полностью совпали "
        f"{s['pages_full']}, ниже 90 %: {s['pages_below_90']}, без слоя: "
        f"{s['pages_without_layer']}, таблиц {s['tables']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
