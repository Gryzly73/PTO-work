"""Прогон комплекта DWG по тексту и таблицам: стабильность и точность.

Зачем. Сервисный путь для чертежа не зовёт модель — текст читается из данных.
Но между DWG и Markdown стоят конвертер `dwg2dxf` (недетерминирован, часть
прогонов даёт битый DXF), склейка разрезанных строк, привязка блоков к листам
и сборка таблиц. Каждый из этих шагов может тихо потерять текст, и по одному
чертежу этого не увидеть. Здесь весь комплект гоняется тем же кодом, что и
сервис (`dwg_sheets.sheet_markdown`), и по нему считаются три вещи:

  * **объём и состав.** Сколько листов, знаков текста, таблиц и табличных
    строк выходит из каждого файла; сколько листов чертёжных, сколько
    текстовых; сколько листов без текста и с мусором (`\\U+`, `####`, `%%`).
  * **стабильность.** Комплект конвертируется и разбирается дважды, с нуля,
    и выход двух прогонов сверяется лист к листу: то же число листов? тот же
    текст? Разница между прогонами — это то, что заказчик увидит как «один и
    тот же чертёж даёт разный результат».
  * **точность против альбома.** Там, где рядом с DWG лежит PDF того же
    раздела с текстовым слоем, листы сводятся по основной надписи (шифр +
    номер листа), и текст чертежа сверяется со слоем PDF теми же метриками,
    что и текстовый путь (`score_vs_pdftext`): полнота слов и чисел, доля
    чисел чертежа, которых в альбоме нет.

Запуск:

    python bench_dwg_text.py "1. Стадия П DWG" --out bench_dwg --runs 2

Результат: `bench_dwg/summary.json` (всё числами), `bench_dwg/report.md`
(таблицы для отчёта), `bench_dwg/run1/…/page_NNNN.md` — сам Markdown.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# Консоль Windows в cp1251 падает на части кириллицы в именах файлов.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import dwg_sheets  # noqa: E402
import stamp as stamp_mod  # noqa: E402
from score_vs_pdftext import score_page, tokens_of  # noqa: E402

_JUNK_RE = re.compile(r"\\U\+|####|%%[a-zA-Z]|\\[pP]\w|\\f[^;]*;")
_PASS_B = "### PASS-B"
_PASS_0_SHEET = re.compile(r"(?m)^-\s*лист:\s*(\d+)")
_PASS_0_CODE = re.compile(r"(?m)^-\s*обозначение:\s*(.+)$")


def _pass_b(md: str) -> str:
    i = md.find(_PASS_B)
    if i < 0:
        return ""
    body = md[i:].split("\n", 1)[1] if "\n" in md[i:] else ""
    return body


def _table_rows(text: str) -> int:
    rows = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
    # строки-разделители |---| не считаем
    return sum(1 for ln in rows if not re.fullmatch(r"\|[\s:|-]+\|", ln.strip()))


def render_file(path: Path, out_dir: Path) -> dict:
    """Все листы одного DWG тем же кодом, что сервис. Конвертация с нуля."""
    import ezdxf

    dwg_sheets._DOC_CACHE.clear()
    t0 = time.time()
    dxf, sheets = dwg_sheets.sheets_for(path)
    t_convert = time.time() - t0
    doc = ezdxf.readfile(str(dxf))
    msp = doc.modelspace()
    unit_code = int(doc.header.get("$INSUNITS", 0) or 0)
    unit_m = dwg_sheets._UNIT_TO_M.get(unit_code, 0.001)
    xrefs = dwg_sheets.xref_names(doc)
    pages = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for index, sheet in enumerate(sheets, start=1):
        layers, blocks = dwg_sheets.analyse_sheet(msp, sheet, unit_m)
        tables, ole = dwg_sheets.sheet_extras(msp, sheet)
        md = dwg_sheets.sheet_markdown(
            sheet, path.name, layers, blocks, xrefs, tables, ole
        )
        (out_dir / f"page_{index:04d}.md").write_text(md, encoding="utf-8")
        text = _pass_b(md)
        words, nums, codes = tokens_of(text)
        found = stamp_mod.from_dwg_sheet(sheet)
        pages.append(
            {
                "index": index,
                "kind": dwg_sheets.sheet_kind(layers),
                "code": found.code,
                "sheet": found.sheet,
                "title": (found.title or sheet.title or "")[:80],
                "chars": len(text),
                "words": sorted(words),
                "nums": sorted(nums),
                "codes": sorted(codes),
                "tables": len(tables or []),
                "table_rows": _table_rows(text),
                "junk": len(_JUNK_RE.findall(text)),
                "mock": "(mock)" in md.split(_PASS_B)[0],
                "map_present": "### PASS-A Карта листа" in md,
            }
        )
    result = {
        "file": str(path),
        "sheets": len(sheets),
        "seconds": round(time.time() - t0, 1),
        "convert_seconds": round(t_convert, 1),
        "xrefs": len(xrefs),
        "pages": pages,
    }
    # Слепок результата рядом с листами: прогон комплекта идёт около часа, и
    # оборванный прогон должен продолжаться с места обрыва, а не с нуля.
    (out_dir / "_file.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    return result


def _page_record_from_md(index: int, md: str) -> dict:
    """Запись листа по сохранённому markdown — для прогона без слепка."""
    text = _pass_b(md)
    words, nums, codes = tokens_of(text)
    head = md.split(_PASS_B)[0]
    code = _PASS_0_CODE.search(head)
    sheet = _PASS_0_SHEET.search(head)
    title = re.search(r"(?m)^-\s*наименование листа:\s*(.+)$", head)
    tables = len(re.findall(r"(?m)^\*\*Таблица \d+", text))
    return {
        "index": index,
        # По новому правилу карта есть ровно у чертежа.
        "kind": "plan" if "### PASS-A Карта листа" in md else "text",
        "code": code.group(1).strip() if code else "",
        "sheet": sheet.group(1) if sheet else "",
        "title": (title.group(1).strip() if title else "")[:80],
        "chars": len(text),
        "words": sorted(words),
        "nums": sorted(nums),
        "codes": sorted(codes),
        "tables": tables,
        "table_rows": _table_rows(text),
        "junk": len(_JUNK_RE.findall(text)),
        "mock": "(mock)" in head,
        "map_present": "### PASS-A Карта листа" in md,
    }


def rebuild_from_dir(path: Path, out_dir: Path) -> dict | None:
    """Результат по файлу из уже сохранённых листов оборванного прогона."""
    sidecar = out_dir / "_file.json"
    if sidecar.exists():
        return json.loads(sidecar.read_text(encoding="utf-8"))
    pages_md = sorted(out_dir.glob("page_*.md"))
    if not pages_md:
        return None
    pages = [
        _page_record_from_md(i, p.read_text(encoding="utf-8"))
        for i, p in enumerate(pages_md, start=1)
    ]
    return {
        "file": str(path),
        "sheets": len(pages),
        "seconds": None,
        "convert_seconds": None,
        "xrefs": None,
        "pages": pages,
        "rebuilt": True,
    }


def run_all(files: list[Path], out_dir: Path, base: Path, resume: bool = False) -> dict:
    results = {}
    for n, path in enumerate(files, start=1):
        rel = path.relative_to(base)
        target = out_dir / rel.with_suffix("")
        if resume:
            ready = rebuild_from_dir(path, target)
            if ready is not None:
                print(f"[{n}/{len(files)}] {rel} — из сохранённого", flush=True)
                results[str(rel)] = ready
                continue
        print(f"[{n}/{len(files)}] {rel}", flush=True)
        try:
            results[str(rel)] = render_file(path, target)
        except Exception as e:  # keep-going: файл падает, комплект идёт
            print(f"    ОШИБКА {type(e).__name__}: {e}", flush=True)
            results[str(rel)] = {
                "file": str(path),
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(limit=2),
                "sheets": 0,
                "pages": [],
            }
    return results


def _jaccard(a: list, b: list) -> float | None:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return None
    return round(100.0 * len(sa & sb) / len(sa | sb), 1)


def stability(run1: dict, run2: dict) -> dict:
    files = []
    sheets_same = sheets_total = identical = compared = 0
    jac = []
    for rel, r1 in run1.items():
        r2 = run2.get(rel, {})
        if "error" in r1 or "error" in r2:
            files.append({"file": rel, "error": r1.get("error") or r2.get("error")})
            continue
        same_count = r1["sheets"] == r2["sheets"]
        sheets_same += same_count
        sheets_total += 1
        per = []
        for p1, p2 in zip(r1["pages"], r2["pages"]):
            compared += 1
            jw = _jaccard(p1["words"], p2["words"])
            jn = _jaccard(p1["nums"], p2["nums"])
            same = p1["words"] == p2["words"] and p1["nums"] == p2["nums"]
            identical += same
            if jw is not None:
                jac.append(jw)
            if not same:
                per.append(
                    {
                        "sheet": p1["index"],
                        "words_jaccard": jw,
                        "nums_jaccard": jn,
                        "chars": [p1["chars"], p2["chars"]],
                    }
                )
        files.append(
            {
                "file": rel,
                "sheets": [r1["sheets"], r2["sheets"]],
                "same_sheet_count": same_count,
                "changed_sheets": per,
            }
        )
    return {
        "files_same_sheet_count": sheets_same,
        "files_compared": sheets_total,
        "sheets_compared": compared,
        "sheets_identical": identical,
        "sheets_identical_pct": round(100.0 * identical / compared, 1) if compared else None,
        "words_jaccard_mean": round(sum(jac) / len(jac), 1) if jac else None,
        "files": files,
    }


def accuracy_vs_pdf(run: dict, pdfs: list[Path], base: Path) -> dict:
    """Листы DWG против альбома PDF того же раздела, по основной надписи."""
    from bundle import read_source
    from service.convert import page_layer_text

    # Лист чертежа по ключу (шифр, номер)
    by_key: dict[tuple[str, str], tuple[str, dict]] = {}
    for rel, r in run.items():
        for p in r.get("pages", []):
            if p["code"] and p["sheet"]:
                by_key.setdefault((p["code"], p["sheet"]), (rel, p))
    out = {"pdfs": [], "pairs": []}
    for pdf in pdfs:
        variants = read_source(pdf)
        matched = 0
        for v in variants:
            key = (v.stamp.code, v.stamp.sheet)
            hit = by_key.get(key)
            if not hit:
                continue
            rel, p = hit
            layer = page_layer_text(pdf, v.index)
            if len(layer.strip()) < 40:
                continue
            md_path = Path(out_dir_global) / "run1" / Path(rel).with_suffix("") / f"page_{p['index']:04d}.md"
            dwg_text = _pass_b(md_path.read_text(encoding="utf-8")) if md_path.exists() else ""
            s = score_page(layer, dwg_text)
            # Обратная сторона: чего в альбоме нет, а в чертеже есть
            back = score_page(dwg_text, layer)
            matched += 1
            out["pairs"].append(
                {
                    "code": key[0],
                    "sheet": key[1],
                    "pdf": str(pdf.relative_to(base)),
                    "pdf_page": v.index,
                    "dwg": rel,
                    "dwg_page": p["index"],
                    "kind": p["kind"],
                    "token_recall": s["token_recall"],
                    "num_recall": s["num_recall"],
                    "num_precision": s["num_precision"],
                    "code_recall": s["code_recall"],
                    "ref_words": s["ref_words"],
                    "ref_nums": s["ref_nums"],
                    "missed_nums": s["missed_nums"],
                    "extra_nums": s["hallucinated_nums"],
                    "pdf_words_not_in_dwg": s["missed_words"],
                    "dwg_words_not_in_pdf": back["missed_words"],
                }
            )
        out["pdfs"].append(
            {"pdf": str(pdf.relative_to(base)), "pages": len(variants), "matched": matched}
        )
    return out


out_dir_global = ""


def _avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def summarize(run1: dict, stab: dict | None, acc: dict | None) -> dict:
    pages = [p for r in run1.values() for p in r.get("pages", [])]
    kinds = {"plan": 0, "text": 0}
    for p in pages:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    files_ok = [r for r in run1.values() if "error" not in r]
    per_file = [
        {
            "file": rel,
            "sheets": r.get("sheets", 0),
            "seconds": r.get("seconds"),
            "chars": sum(p["chars"] for p in r.get("pages", [])),
            "tables": sum(p["tables"] for p in r.get("pages", [])),
            "table_rows": sum(p["table_rows"] for p in r.get("pages", [])),
            "empty_sheets": sum(1 for p in r.get("pages", []) if p["chars"] < 40),
            "junk": sum(p["junk"] for p in r.get("pages", [])),
            "no_stamp": sum(1 for p in r.get("pages", []) if not p["code"]),
            "error": r.get("error"),
        }
        for rel, r in run1.items()
    ]
    summary = {
        "files": len(run1),
        "files_ok": len(files_ok),
        "files_error": [rel for rel, r in run1.items() if "error" in r],
        "sheets": len(pages),
        "sheets_by_kind": kinds,
        "chars": sum(p["chars"] for p in pages),
        "tables": sum(p["tables"] for p in pages),
        "table_rows": sum(p["table_rows"] for p in pages),
        "sheets_empty": sum(1 for p in pages if p["chars"] < 40),
        "sheets_mock": sum(1 for p in pages if p["mock"]),
        "sheets_with_junk": sum(1 for p in pages if p["junk"]),
        "junk_total": sum(p["junk"] for p in pages),
        "sheets_no_stamp": sum(1 for p in pages if not p["code"]),
        "sheets_map_on_text": sum(1 for p in pages if p["kind"] == "text" and p["map_present"]),
        "sheets_map_on_plan": sum(1 for p in pages if p["kind"] == "plan" and p["map_present"]),
        # У файлов, восстановленных из сохранённого, времени нет.
        "seconds_total": round(sum(r.get("seconds") or 0 for r in files_ok), 1),
        "seconds_per_sheet": round(
            sum(r.get("seconds") or 0 for r in files_ok)
            / max(1, sum(r["sheets"] for r in files_ok if r.get("seconds"))),
            2,
        ),
        "files_timed": sum(1 for r in files_ok if r.get("seconds")),
        "per_file": per_file,
    }
    if stab:
        summary["stability"] = {k: v for k, v in stab.items() if k != "files"}
        summary["stability"]["files_changed"] = [
            f for f in stab["files"] if f.get("changed_sheets") or not f.get("same_sheet_count", True)
        ]
    if acc:
        pairs = acc["pairs"]
        summary["accuracy_vs_pdf"] = {
            "pdfs": acc["pdfs"],
            "pairs": len(pairs),
            "token_recall": _avg([p["token_recall"] for p in pairs]),
            "num_recall": _avg([p["num_recall"] for p in pairs]),
            "num_precision": _avg([p["num_precision"] for p in pairs]),
            "code_recall": _avg([p["code_recall"] for p in pairs]),
            "by_kind": {
                kind: {
                    "pairs": len([p for p in pairs if p["kind"] == kind]),
                    "token_recall": _avg([p["token_recall"] for p in pairs if p["kind"] == kind]),
                    "num_recall": _avg([p["num_recall"] for p in pairs if p["kind"] == kind]),
                    "num_precision": _avg([p["num_precision"] for p in pairs if p["kind"] == kind]),
                }
                for kind in ("text", "plan")
            },
            "per_pair": pairs,
        }
    return summary


def report_md(s: dict) -> str:
    L = []
    L.append("# Прогон комплекта DWG: текст и таблицы\n")
    L.append(f"Файлов: {s['files']}, разобрано: {s['files_ok']}, с ошибкой: {len(s['files_error'])}.  ")
    L.append(f"Листов: {s['sheets']} (чертёжных {s['sheets_by_kind'].get('plan', 0)}, текстовых {s['sheets_by_kind'].get('text', 0)}).  ")
    L.append(f"Знаков текста: {s['chars']:,}. Таблиц: {s['tables']}, строк в них: {s['table_rows']}.  ".replace(",", " "))
    L.append(f"Листов без текста: {s['sheets_empty']}, с заглушкой mock: {s['sheets_mock']}, с мусором в тексте: {s['sheets_with_junk']} ({s['junk_total']} вхождений), без штампа: {s['sheets_no_stamp']}.  ")
    L.append(f"Карта листа на текстовых листах: {s['sheets_map_on_text']} (должно быть 0), на чертежах: {s['sheets_map_on_plan']}.  ")
    L.append(f"Время: {s['seconds_total']} с на комплект, {s['seconds_per_sheet']} с на лист.\n")
    if s.get("stability"):
        st = s["stability"]
        L.append("## Стабильность (два прогона с нуля)\n")
        L.append(f"Файлов с тем же числом листов: {st['files_same_sheet_count']} из {st['files_compared']}.  ")
        L.append(f"Листов с тем же текстом (слова и числа): {st['sheets_identical']} из {st['sheets_compared']} ({st['sheets_identical_pct']} %).  ")
        L.append(f"Средний Жаккар по словам между прогонами: {st['words_jaccard_mean']} %.\n")
        if st["files_changed"]:
            L.append("| Файл | Листов (1/2) | Листы с разным текстом |")
            L.append("|---|---|---|")
            for f in st["files_changed"][:40]:
                ch = ", ".join(f"{c['sheet']} ({c['words_jaccard']}%)" for c in f.get("changed_sheets", [])[:8])
                L.append(f"| {f['file']} | {f.get('sheets', ['?', '?'])[0]}/{f.get('sheets', ['?', '?'])[1]} | {ch or '—'} |")
            L.append("")
    if s.get("accuracy_vs_pdf"):
        a = s["accuracy_vs_pdf"]
        L.append("## Точность против альбома PDF (по основной надписи)\n")
        for p in a["pdfs"]:
            L.append(f"- `{p['pdf']}`: страниц {p['pages']}, сведено с DWG {p['matched']}")
        L.append("")
        L.append(f"Пар листов: {a['pairs']}. Полнота слов {a['token_recall']} %, полнота чисел {a['num_recall']} %, достоверность чисел {a['num_precision']} %, полнота кодов {a['code_recall']} %.\n")
        L.append("| Тип листа | Пар | Полнота слов | Полнота чисел | Достоверность чисел |")
        L.append("|---|---:|---:|---:|---:|")
        for kind, v in a["by_kind"].items():
            L.append(f"| {kind} | {v['pairs']} | {v['token_recall']} | {v['num_recall']} | {v['num_precision']} |")
        L.append("")
        L.append("| Шифр | Лист | Тип | PDF стр | DWG | Полнота слов | Полнота чисел | Достов. чисел | Пропущенные числа |")
        L.append("|---|---|---|---:|---|---:|---:|---:|---|")
        for p in sorted(a["per_pair"], key=lambda x: (x["token_recall"] or 0)):
            L.append(
                f"| {p['code']} | {p['sheet']} | {p['kind']} | {p['pdf_page']} | {Path(p['dwg']).name} л.{p['dwg_page']} | "
                f"{p['token_recall']} | {p['num_recall']} | {p['num_precision']} | {', '.join(p['missed_nums'][:6])} |"
            )
        L.append("")
    L.append("## По файлам\n")
    L.append("| Файл | Листов | Знаков | Таблиц | Строк | Пустых | Мусор | Без штампа | с |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for f in s["per_file"]:
        if f["error"]:
            L.append(f"| {f['file']} | ОШИБКА: {f['error'][:80]} | | | | | | | |")
        else:
            L.append(f"| {f['file']} | {f['sheets']} | {f['chars']} | {f['tables']} | {f['table_rows']} | {f['empty_sheets']} | {f['junk']} | {f['no_stamp']} | {f['seconds']} |")
    return "\n".join(L) + "\n"


def main() -> int:
    global out_dir_global
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root")
    ap.add_argument("--out", default="bench_dwg")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0, help="только первые N файлов")
    ap.add_argument(
        "--resume",
        action="store_true",
        help="продолжить оборванный прогон: готовые файлы взять из сохранённого",
    )
    args = ap.parse_args()
    base = Path(args.root)
    out = Path(args.out)
    out_dir_global = str(out)
    files = sorted(p for p in base.rglob("*.dwg") if p.suffix.lower() == ".dwg")
    if args.limit:
        files = files[: args.limit]
    pdfs = [
        p
        for p in sorted(base.rglob("*.pdf"))
        if not p.name.endswith(".dwg-0001.pdf")
    ]
    print(f"DWG: {len(files)}, PDF для сверки: {len(pdfs)}", flush=True)

    runs = []
    for r in range(1, args.runs + 1):
        print(f"=== прогон {r}", flush=True)
        res = run_all(files, out / f"run{r}", base, resume=args.resume)
        (out / f"run{r}.json").write_text(
            json.dumps(res, ensure_ascii=False), encoding="utf-8"
        )
        runs.append(res)
    stab = stability(runs[0], runs[1]) if len(runs) > 1 else None
    print("=== сверка с PDF", flush=True)
    try:
        acc = accuracy_vs_pdf(runs[0], pdfs, base) if pdfs else None
    except Exception as e:
        print(f"сверка с PDF не удалась: {e}", flush=True)
        traceback.print_exc()
        acc = None
    summary = summarize(runs[0], stab, acc)
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (out / "report.md").write_text(report_md(summary), encoding="utf-8")
    print(f"готово: {out / 'report.md'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
