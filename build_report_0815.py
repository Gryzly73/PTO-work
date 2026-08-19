"""Сборка HTML-отчёта по прогонам VLM (время / модели / качество / сравнение).

Источники (всё локальное, без сети):
  hf_runs/<run>/meta.json          — модель, страницы, время, токены, флаги
  hf_runs/<run>/out.compare.json   — метрика против ЭТАЛОНА (rough recall/key)
  hf_runs/<run>/pdftext.json       — метрика против ТЕКСТОВОГО СЛОЯ PDF
  report_log.json                  — журнал изменений алгоритма (ведётся руками)

Пересобирается сколько угодно раз: python build_report_0815.py --all
Перерисовать готовый отчёт новым шаблоном, не трогая цифры:
  python build_report_0815.py --from-html ОТЧЁТ_2026-08-15.html -o ОТЧЁТ_2026-08-15.html

Разметку строит report_render.py: страница статическая, JavaScript нужен только
для вкладок и сортировки — без него виден весь отчёт одним полотном.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import report_render

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "hf_runs"

# $/1M токенов (суммарно in+out, блендед). Выведено из замеренных стоимостей
# волны 14.08: rate = стоимость прогона / total_tokens этого прогона.
# Метод проверен на моделях с опубликованным прайсом: qwen3vl-235b даёт 0.56
# против 0.59 расчётных, kimi-k3 — 9.59 против 9.58. Погрешность ±10%.
RATES: dict[str, float | None] = {
    "kimi-k3": 9.59,
    "qwen36-27b": 1.90,
    "glm-45v": 1.39,
    "glm-46v-flash": 0.69,
    "qwen3vl-235b": 0.56,
    "qwen36-35b-a3b": 0.50,
    "gemma4-26b-a4b": 0.20,
    "qwen3vl-32b": 0.10,  # featherless: грубая оценка из «49 стр ≈ $0.05–0.15»
    "qwen3vl-30b-a3b": 0.30,
    "qwen3vl-8b": 0.05,
}

# Влезает ли на потенциальный сервер 4×A16 (~61 GB VRAM, TP=4)
SERVER_FIT = {
    "qwen3vl-32b": True,
    "qwen36-35b-a3b": True,
    "qwen36-27b": True,
    "qwen3vl-30b-a3b": True,
    "qwen3vl-8b": True,
    "gemma4-26b-a4b": True,
    "qwen3vl-235b": False,
    "glm-45v": False,
    "kimi-k3": False,
    "glm-46v-flash": True,
}


def collect_runs(since: str | None) -> list[dict]:
    out: list[dict] = []
    for d in sorted(RUNS.glob("*")):
        mp = d / "meta.json"
        if not mp.exists():
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        stamp = m.get("started_utc", d.name[:15])
        if since and stamp < since:
            continue
        u = m.get("usage", {}) or {}
        model = (m.get("spec") or {}).get("id", "?")
        tot = u.get("total_tokens", 0)
        rate = RATES.get(model)
        rec: dict = {
            "run": d.name,
            "stamp": stamp,
            "model": model,
            "pages": str(m.get("pages", "")),
            "elapsed": round(m.get("elapsed_sec", 0) or 0, 1),
            "in": u.get("prompt_tokens", 0),
            "out": u.get("completion_tokens", 0),
            "total": tot,
            "calls": u.get("calls", 0),
            "retries": u.get("retries", 0),
            "failed": u.get("failed_tiles", 0),
            "cost": round(tot / 1e6 * rate, 4) if rate else None,
            "two_pass": m.get("two_pass"),
            "sheet_aware": m.get("sheet_aware"),
            "table_pages": m.get("table_pages") or m.get("tag") or "",
            "fit": SERVER_FIT.get(model),
        }
        n_pages = max(1, len(_expand_pages(rec["pages"])))
        rec["n_pages"] = n_pages
        rec["sec_per_page"] = round(rec["elapsed"] / n_pages, 1)
        rec["cost_per_page"] = (
            round(rec["cost"] / n_pages, 5) if rec["cost"] is not None else None
        )

        cmp_p = d / "out.compare.json"
        if cmp_p.exists():
            try:
                rows = json.loads(cmp_p.read_text(encoding="utf-8"))
                if rows:
                    rec["etalon"] = {
                        "recall": round(
                            sum(r["token_recall"] for r in rows) / len(rows), 1
                        ),
                        "key": round(
                            sum(r["key_phrase_hit"] for r in rows) / len(rows), 1
                        ),
                        "pages": [r["page"] for r in rows],
                    }
            except Exception:
                pass

        pt = d / "pdftext.json"
        if pt.exists():
            try:
                j = json.loads(pt.read_text(encoding="utf-8"))
                rec["pdftext"] = {
                    "avg": j.get("avg", {}),
                    "n": len(j.get("pages", [])),
                    "label": j.get("label"),
                    # подетально — для теплокарты «страница × метрика»
                    "pages": [
                        {
                            "page": r["page"],
                            "w": r["token_recall"],
                            "num": r["num_recall"],
                            "prec": r["num_precision"],
                            "code": r["code_recall"],
                            "partial": r.get("partial", False),
                            "halluc": r.get("hallucinated_nums", [])[:8],
                        }
                        for r in j.get("pages", [])
                    ],
                }
            except Exception:
                pass
        out.append(rec)
    return out


def _expand_pages(spec: str) -> list[int]:
    res: list[int] = []
    for ch in str(spec).split(","):
        ch = ch.strip()
        if not ch:
            continue
        try:
            if "-" in ch:
                a, b = ch.split("-", 1)
                res.extend(range(int(a), int(b) + 1))
            else:
                res.append(int(ch))
        except ValueError:
            continue
    return res


def extract_data(html_path: Path) -> dict:
    """Достать данные из уже собранного отчёта.

    Нужно, чтобы перерисовать старый отчёт новым шаблоном, не сдвигая его цифры
    (дату сборки, «израсходовано сегодня») и не пересобирая из hf_runs.
    """
    src = html_path.read_text(encoding="utf-8-sig")
    m = re.search(
        r'<script id="reportdata" type="application/json">(.*?)</script>', src, re.S
    )
    if not m:
        raise SystemExit(f"в {html_path} нет блока с данными")
    return json.loads(m.group(1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=str(ROOT / "ОТЧЁТ_2026-08-15.html"))
    ap.add_argument("--since", default="20260815", help="UTC-префикс, от какого прогона")
    ap.add_argument("--all", action="store_true", help="включить все прогоны")
    ap.add_argument(
        "--from-html",
        metavar="FILE",
        help="перерисовать существующий отчёт из его же данных (цифры не меняются)",
    )
    args = ap.parse_args()

    if args.from_html:
        data = extract_data(Path(args.from_html))
        html = report_render.render(data)
        Path(args.out).write_text(html, encoding="utf-8-sig", newline="\n")
        print(f"wrote {args.out} (перерисован из {args.from_html})")
        return 0

    runs = collect_runs(None if args.all else args.since)
    log_p = ROOT / "report_log.json"
    log = json.loads(log_p.read_text(encoding="utf-8")) if log_p.exists() else []
    # итог сверки итогового документа с исходником (build_match_viewer.py --json)
    ready_p = ROOT / "readiness.json"
    readiness = None
    if ready_p.exists():
        try:
            readiness = json.loads(ready_p.read_text(encoding="utf-8"))
        except Exception:
            readiness = None

    data = {
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "runs": runs,
        "log": log,
        "readiness": readiness,
    }
    html = report_render.render(data)
    # BOM: вьюеры, которые игнорируют <meta charset> (предпросмотр вложений,
    # файловые менеджеры), иначе гадают кодировку и показывают кракозябры.
    Path(args.out).write_text(html, encoding="utf-8-sig", newline="\n")
    print(f"wrote {args.out} ({len(runs)} прогонов, {len(log)} записей журнала)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
