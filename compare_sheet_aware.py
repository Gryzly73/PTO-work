#!/usr/bin/env python3
"""Сводка сравнения baseline vs sheet-aware прогонов."""
from __future__ import annotations

import argparse
import re
from pathlib import Path


def split_pages(md: str) -> dict[int, str]:
    parts = re.split(r"(?m)^##\s+Страница\s+(\d+)\s*$", md)
    out: dict[int, str] = {}
    for i in range(1, len(parts), 2):
        out[int(parts[i])] = parts[i + 1] if i + 1 < len(parts) else ""
    return out


def extract_pass_a(body: str) -> str:
    m = re.search(r"###\s*PASS-A[^\n]*\n(.*?)(?=\n###\s*PASS-B|\Z)", body, re.S)
    return m.group(1).strip() if m else ""


def score_desc(desc: str) -> dict:
    low = desc.lower()
    sections = sum(
        1
        for k in ("что изображено", "из чего состоит", "где расположен", "как связан")
        if k in low
    )
    return {
        "chars": len(desc),
        "sections": sections,
        "has_repeat": "[truncated-repeat]" in desc,
        "has_zone_retry": "уточнение по зонам" in low,
        "mentions_v0v1": bool(re.search(r"\bв[0-3]\b", low)),
        "mentions_plan": "план" in low,
        "garbage_hint": desc.count("подземные") >= 6 or sections == 0 and len(desc) < 900,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--sheetaware", type=Path, required=True)
    ap.add_argument("-o", type=Path, default=None)
    args = ap.parse_args()

    b = split_pages(args.baseline.read_text(encoding="utf-8"))
    s = split_pages(args.sheetaware.read_text(encoding="utf-8"))
    pages = sorted(set(b) & set(s))
    lines = ["# Сравнение baseline vs sheet-aware\n"]
    for n in pages:
        ba = extract_pass_a(b[n])
        sa = extract_pass_a(s[n])
        sb, ss = score_desc(ba), score_desc(sa)
        lines.append(f"## Страница {n}\n")
        lines.append("| метрика | baseline | sheet-aware |")
        lines.append("|---|---:|---:|")
        for k in sb:
            lines.append(f"| {k} | {sb[k]} | {ss[k]} |")
        lines.append("\n### baseline Pass-A (head)\n")
        lines.append("```\n" + ba[:700] + "\n```\n")
        lines.append("\n### sheet-aware Pass-A (head)\n")
        lines.append("```\n" + sa[:700] + "\n```\n")
    out = args.o or Path("hf_runs/sheet_aware_compare.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
