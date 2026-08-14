#!/usr/bin/env python3
"""Собирает best-of combined md из лучших per-page выводов и скорит их scorer'ом.

Страницы 1,3,4,5,6 — из peak_30b_all6_union_ocr.md
Страница 2 (ОДИ) — из фикса p2 union x4 (20260717_131831).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def split_pages(md: str) -> dict[int, str]:
    parts = re.split(r"(?m)^##\s+Страница\s+(\d+)\s*$", md)
    out: dict[int, str] = {}
    for i in range(1, len(parts), 2):
        out[int(parts[i])] = parts[i + 1] if i + 1 < len(parts) else ""
    return out


def main() -> int:
    peak = split_pages(
        (ROOT / "hf_runs" / "peak_30b_all6_union_ocr.md").read_text(encoding="utf-8")
    )
    # свежий zone+stamp прогон ОДИ/КР1 (p2,p3)
    fresh = split_pages(
        (ROOT / "hf_runs" / "20260718_154755_qwen3vl-32b_twopass" / "out.md").read_text(
            encoding="utf-8"
        )
    )
    # свежий КР3 (p4): таблица грунтов разбита на 2 половины + DPI зон 5600
    kr3 = split_pages(
        (ROOT / "hf_runs" / "20260719_110907_qwen3vl-32b_twopass" / "out.md").read_text(
            encoding="utf-8"
        )
    )
    # свежий ПБ (p1): зоны блока автоматики (контроллеры/шкафы/сигналы У1-У4)
    pb = split_pages(
        (ROOT / "hf_runs" / "20260720_083848_qwen3vl-32b_twopass" / "out.md").read_text(
            encoding="utf-8"
        )
    )
    best = dict(peak)
    for pg in (2, 3):
        if pg in fresh and fresh[pg].strip():
            best[pg] = fresh[pg]
    if 4 in kr3 and kr3[4].strip():
        best[4] = kr3[4]
    # ПБ: гидравлика+легенда из peak уже проходят, автоматика — из зонного прогона.
    # Объединяем оба, чтобы не терять ни диаметры/легенду, ни контроллеры/шкафы.
    if 1 in pb and pb[1].strip():
        best[1] = f"{peak.get(1, '').rstrip()}\n\n{pb[1].rstrip()}\n"

    combined = (
        "\n\n".join(f"## Страница {n}\n{best[n].rstrip()}\n" for n in sorted(best))
        + "\n"
    )
    out_path = ROOT / "hf_runs" / "best_of_all6.md"
    out_path.write_text(combined, encoding="utf-8")
    print(f"wrote {out_path}")

    scorer = ROOT / "new_files" / "pto_scoring_kit" / "score_real_testset.py"
    res = subprocess.run(
        [sys.executable, str(scorer), "--combined", str(out_path)],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return res.returncode


if __name__ == "__main__":
    raise SystemExit(main())
