#!/usr/bin/env python3
"""Детерминированный union: VLM-черновик + OCR (без LLM-сжатия).

Добавляет строки/коды из OCR, которых ещё нет в draft (по нормализованным токенам).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from local_ocr import append_stamp_missing, extract_stamp_codes, ocr_stamp
from pdf_to_md_ollama import (
    assemble,
    dpi_for_page_budget,
    render_page_image,
    resume_dir_for,
    save_page,
)
import fitz

ROOT = Path(__file__).resolve().parent
TOKEN_RE = re.compile(
    r"[0-9]+(?:[.,][0-9]+)?|[A-Za-zА-Яа-яЁё]{2,}(?:[-/][A-Za-zА-Яа-яЁё0-9]+)*",
    re.UNICODE,
)


def split_pages_md(text: str) -> dict[int, str]:
    parts = re.split(r"(?m)^## Страница\s+(\d+)\s*$", text)
    out: dict[int, str] = {}
    if len(parts) < 3:
        out[1] = text.strip()
        return out
    i = 1
    while i + 1 < len(parts):
        out[int(parts[i])] = parts[i + 1].strip()
        i += 2
    return out


def norm_tokens(text: str) -> set[str]:
    return {t.upper() for t in TOKEN_RE.findall(text) if len(t) >= 2}


def useful_ocr_lines(ocr: str) -> list[str]:
    out: list[str] = []
    for ln in ocr.splitlines():
        s = ln.strip()
        if not s or s.startswith("---") or s == "(пусто)":
            continue
        if len(s) < 3:
            continue
        # отсечь совсем мусорные однобуквенные линии
        if len(norm_tokens(s)) == 0:
            continue
        out.append(s)
    return out


def union_page(draft: str, ocr: str) -> str:
    draft_tok = norm_tokens(draft)
    extra: list[str] = []
    seen: set[str] = set()
    for ln in useful_ocr_lines(ocr):
        key = ln.upper()
        if key in seen:
            continue
        toks = norm_tokens(ln)
        # строка полезна, если ≥1 «нового» токена длины≥3 или есть код-паттерн
        new = {t for t in toks if len(t) >= 3 and t not in draft_tok}
        if not new and not extract_stamp_codes(ln):
            continue
        # если все токены уже есть — пропуск
        if toks and toks.issubset(draft_tok):
            continue
        seen.add(key)
        extra.append(ln)
        draft_tok |= toks
    codes = extract_stamp_codes(ocr)
    body = draft.rstrip()
    if extra:
        body += "\n\n### OCR-добор\n\n" + "\n".join(f"- {x}" for x in extra[:80])
    return append_stamp_missing(body, ocr, codes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", type=Path, required=True)
    ap.add_argument("--ocr-dir", type=Path, required=True, help="*.pages с page_XXXX.md OCR")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--pages", default="1,3,5")
    ap.add_argument("--stamp-from-pdf", action="store_true", help="перечитать углы штампа с PDF")
    args = ap.parse_args()

    drafts = split_pages_md(args.draft.read_text(encoding="utf-8"))
    page_nums = [int(x) for x in args.pages.split(",") if x.strip()]
    rdir = resume_dir_for(args.output)
    pages: dict[int, str] = {}

    pdf = None
    doc = None
    if args.stamp_from_pdf:
        etalon = next(p for p in ROOT.iterdir() if p.is_dir() and p.name.startswith("ЭТАЛОН"))
        pdf = next(etalon.glob("*.pdf"))
        doc = fitz.open(pdf)

    for num in page_nums:
        draft = drafts.get(num, "")
        ocr_path = args.ocr_dir / f"page_{num:04d}.md"
        ocr = ocr_path.read_text(encoding="utf-8") if ocr_path.exists() else ""
        if doc is not None:
            page = doc[num - 1]
            im = render_page_image(page, dpi_for_page_budget(page, 400, 2800), 2800)
            stamp_raw, _ = ocr_stamp(im)
            ocr = (ocr + "\n\n" + stamp_raw).strip()
        merged = union_page(draft, ocr)
        save_page(rdir, num, merged)
        pages[num] = merged
        print(f"p{num}: draft={len(draft)} ocr={len(ocr)} merged={len(merged)}")

    if doc is not None:
        doc.close()

    args.output.write_text(assemble(pages), encoding="utf-8")
    print(f"Wrote {args.output}")
    subprocess.run(
        [sys.executable, str(ROOT / "compare_to_etalon.py"), str(args.output), "--pages", args.pages],
        check=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
