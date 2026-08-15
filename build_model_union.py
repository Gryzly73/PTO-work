#!/usr/bin/env python
"""Детерминированный union выводов ДВУХ разных моделей по страницам.

База — вывод первой (основной) модели; строки второй, которых нет в базе
(по нормализованным токенам), добавляются в конец страницы отдельной секцией
«### Дополнение (union: <model>)». Контракт `## Страница N` сохраняется.

Пример:
  python build_model_union.py --base hf_runs/<run_a>/out.md \
      --extra hf_runs/<run_b>/out.md --extra-label qwen3vl-32b -o union.md
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from hf_api_bench import (
    assemble,
    collapse_inline_repetition,
    join_value_line_runs,
    split_pages_md,
)


def reclean(body: str) -> str:
    """Пост-обработка готового прогона ТОЛЬКО новыми безопасными фильтрами
    (полный clean_vlm_text на merge опасен — см. LOOP_LEDGER про re-collapse)."""
    return join_value_line_runs(collapse_inline_repetition(body))


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().lower())


def union_page(base: str, extra: str, extra_label: str) -> str:
    seen = {_norm(ln) for ln in base.splitlines() if _norm(ln)}
    additions: list[str] = []
    for ln in extra.splitlines():
        n = _norm(ln)
        if not n or n in seen:
            continue
        # служебные заголовки второй модели не тащим
        if n.startswith(("### pass-", "## страница", "#### ")):
            continue
        seen.add(n)
        additions.append(ln.rstrip())
    if not additions:
        return base
    return (
        base.rstrip()
        + f"\n\n### Дополнение (union: {extra_label})\n\n"
        + "\n".join(additions)
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="out.md основной модели")
    ap.add_argument("--extra", required=True, help="out.md второй модели")
    ap.add_argument("--extra-label", default="model-B")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument(
        "--reclean",
        action="store_true",
        help="применить новые фильтры чистки (inline-повторы, столбики значений) "
        "к обоим прогонам перед union",
    )
    args = ap.parse_args()

    base_pages = split_pages_md(Path(args.base).read_text(encoding="utf-8"))
    extra_pages = split_pages_md(Path(args.extra).read_text(encoding="utf-8"))
    if args.reclean:
        base_pages = {n: reclean(b) for n, b in base_pages.items()}
        extra_pages = {n: reclean(b) for n, b in extra_pages.items()}

    merged: dict[int, str] = {}
    for n in sorted(set(base_pages) | set(extra_pages)):
        b, e = base_pages.get(n), extra_pages.get(n)
        if b and e:
            merged[n] = union_page(b, e, args.extra_label)
        else:
            merged[n] = b or e or ""
        added = len(merged[n]) - len(b or "")
        print(f"p{n}: base {len(b or '')} ch, +union {max(0, added)} ch")

    Path(args.out).write_text(assemble(merged), encoding="utf-8")
    print(f"Saved: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
