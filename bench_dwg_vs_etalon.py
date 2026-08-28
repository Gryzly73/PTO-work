"""Листы DWG против ручного эталона — по основной надписи.

Эталон (`ЭТАЛОН — оригинал PDF + идеальный Markdown`) размечен по страницам
альбома PDF, а альбома в комплекте DWG нет. Но лист в альбоме и лист в DWG
— один и тот же лист с тем же шифром и номером, поэтому пару находим по
штампу из паспорта листа (`bench_dwg_text.py` кладёт его в run1.json) и
сверяем текст чертежа с эталоном теми же метриками, что и текстовый путь.

Полнота здесь — доля слов и чисел ЭТАЛОНА, которые есть в тексте DWG:
то есть сколько из того, что человек прочитал на листе, чертёж отдал как
данные. Достоверность чисел — доля чисел DWG, которых в эталоне нет: на
чертеже это чаще всего размеры, которые человек в эталон не переписывал,
а не выдумка, поэтому смотреть на неё надо вместе со списком лишних чисел.

Запуск после `bench_dwg_text.py`:

    python bench_dwg_vs_etalon.py --run bench_dwg/run1.json --out bench_dwg/etalon.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# Консоль Windows в cp1251 падает на «→» и части кириллицы имён файлов.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from score_vs_pdftext import score_page  # noqa: E402

ETALON_DIR = ROOT / "ЭТАЛОН — оригинал PDF + идеальный Markdown"

# Страница альбома → (раздел в шифре, номер листа в штампе). Номер листа —
# из первой строки эталона («## Страница 53 … 52»); у ПБ и ОДИ чертежей в
# комплекте нет.
ETALON_SHEETS = {
    "ИДЕАЛ_08_KR1_p53.ref.md": ("КР1", "52"),
    "ИДЕАЛ_10_KR3_p59.ref.md": ("КР3", "58"),
    "ИДЕАЛ_11_KR4_p59.ref.md": ("КР4", "58"),
}

_PASS_B = "### PASS-B"


def _pass_b(md: str) -> str:
    i = md.find(_PASS_B)
    return md[i:].split("\n", 1)[1] if i >= 0 and "\n" in md[i:] else ""


def _clean_ref(text: str) -> str:
    # Служебные пометки разметчика и LaTeX-остатки в эталон не входят.
    text = re.sub(r"\[Warning:[^\]]*\]", " ", text)
    text = re.sub(r"\\multi(?:column|row)\{[^}]*\}\{[^}]*\}", " ", text)
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", default="bench_dwg/run1.json")
    ap.add_argument("--out", default="bench_dwg/etalon.json")
    args = ap.parse_args()
    run = json.loads(Path(args.run).read_text(encoding="utf-8"))
    run_dir = Path(args.run).parent / "run1"

    results = []
    for name, (section, sheet) in ETALON_SHEETS.items():
        ref_path = ETALON_DIR / name
        if not ref_path.exists():
            continue
        ref = _clean_ref(ref_path.read_text(encoding="utf-8"))
        # Номер листа в эталоне — по альбому (сквозная нумерация тома), а в
        # штампе DWG — внутри документа, поэтому по номеру пары не найти.
        # Берём все листы раздела с текстом и выбираем тот, где эталон
        # совпал лучше всего; если и лучший совпал слабо — пары нет.
        hits = []
        for rel, r in run.items():
            for p in r.get("pages", []):
                code = p.get("code") or ""
                if code.endswith(section) and p.get("chars", 0) > 200:
                    hits.append((rel, p))
        entry = {"etalon": name, "section": section, "sheet": sheet, "candidates": []}
        for rel, p in hits:
            md_path = run_dir / Path(rel).with_suffix("") / f"page_{p['index']:04d}.md"
            dwg_text = _pass_b(md_path.read_text(encoding="utf-8")) if md_path.exists() else ""
            s = score_page(ref, dwg_text)
            entry["candidates"].append(
                {
                    "dwg": rel,
                    "dwg_page": p["index"],
                    "title": p.get("title"),
                    "kind": p.get("kind"),
                    "token_recall": s["token_recall"],
                    "num_recall": s["num_recall"],
                    "num_precision": s["num_precision"],
                    "code_recall": s["code_recall"],
                    "ref_words": s["ref_words"],
                    "ref_nums": s["ref_nums"],
                    "dwg_nums": s["hyp_nums"],
                    "missed_words": s["missed_words"],
                    "missed_nums": s["missed_nums"],
                    "extra_nums": s["hallucinated_nums"],
                }
            )
        entry["candidates"].sort(key=lambda c: -(c["token_recall"] or 0))
        entry["candidates"] = entry["candidates"][:5]
        results.append(entry)
        best = entry["candidates"][0] if entry["candidates"] else None
        if best:
            print(
                f"{name}: {section} лист {sheet} → {Path(best['dwg']).name} л.{best['dwg_page']} "
                f"«{(best['title'] or '')[:40]}»: полнота слов {best['token_recall']} %, "
                f"чисел {best['num_recall']} % (эталон: {best['ref_words']} слов, "
                f"{best['ref_nums']} чисел); лишних чисел в DWG: "
                f"{best['dwg_nums'] - round((best['num_precision'] or 0) * best['dwg_nums'] / 100)}",
                flush=True,
            )
        else:
            print(f"{name}: {section} лист {sheet} — в DWG не найден", flush=True)
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
