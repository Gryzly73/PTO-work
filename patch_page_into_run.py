#!/usr/bin/env python3
"""Подменяет страницу N в out.md полного прогона свежим прогоном одной страницы."""
from __future__ import annotations

import argparse
import re
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", type=Path, required=True, help="out.md полного прогона")
    ap.add_argument("--patch", type=Path, required=True, help="out.md одностраничного прогона")
    ap.add_argument("--page", type=int, required=True)
    args = ap.parse_args()

    text = args.full.read_text(encoding="utf-8")
    patch = args.patch.read_text(encoding="utf-8").strip() + "\n\n"
    n = args.page
    pat = re.compile(
        rf"(?ms)^## Страница {n}\n.*?(?=^## Страница {n + 1}\n|\Z)"
    )
    m = pat.search(text)
    if not m:
        raise SystemExit(f"страница {n} не найдена в {args.full}")
    bak = args.full.with_suffix(f".md.bak_before_p{n}")
    if not bak.exists():
        bak.write_text(text, encoding="utf-8")
        print(f"backup -> {bak}")
    text2 = pat.sub(patch, text, count=1)
    args.full.write_text(text2, encoding="utf-8")
    print(f"patched page {n}: {m.end() - m.start()} -> {len(patch)} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
