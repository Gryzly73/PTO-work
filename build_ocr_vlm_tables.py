#!/usr/bin/env python3
"""Склеивает VLM-описание (пространственное) + DeepSeek-OCR (таблицы) + PDF-текст.

Для страниц-таблиц: OCR даёт ячейки, VLM — что это за лист и как устроен.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import fitz


def split_pages(md: str) -> dict[int, str]:
    parts = re.split(r"(?m)^##\s+Страница\s+(\d+)\s*$", md)
    out: dict[int, str] = {}
    for i in range(1, len(parts), 2):
        out[int(parts[i])] = parts[i + 1] if i + 1 < len(parts) else ""
    return out


def extract_pass_a(body: str) -> str:
    m = re.search(r"###\s*PASS-A[^\n]*\n(.*?)(?=\n###\s*PASS-B|\Z)", body, re.S)
    return m.group(1).strip() if m else ""


def extract_ocr_body(body: str) -> str:
    """DeepSeek без Pass-A: весь текст страницы или PASS-B."""
    m = re.search(r"###\s*PASS-B[^\n]*\n(.*)\Z", body, re.S)
    if m:
        return m.group(1).strip()
    # без маркеров — весь body
    return body.strip()


def clean_tile_markers(text: str) -> str:
    """Убирает --- r1c1 --- и grounding-теги DeepSeek (text[[x,y..]])."""
    text = re.sub(r"(?m)^---\s*r\d+c\d+\s*---\s*$", "\n", text)
    # DeepSeek grounding: text[[...]] / table[[...]] / sub_title[[...]]
    text = re.sub(
        r"(?m)^(text|table|sub_title|figure|image)\s*\[\[[^\]]*\]\]\s*$",
        "",
        text,
    )
    # HTML-таблицы без нормальных </td> — хоть чуть читаемее
    text = text.replace("<table>", "\n").replace("</table>", "\n")
    text = re.sub(r"<t[dh][^>]*>", " | ", text)
    text = re.sub(r"</t[dh]>", "", text)
    text = re.sub(r"<br\s*/?>", " / ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+\|[ \t]+", " | ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_pdf_text(raw: str) -> str:
    lines = [ln.rstrip() for ln in raw.splitlines()]
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if not ln.strip():
            blanks += 1
            if blanks <= 1:
                out.append("")
            continue
        blanks = 0
        out.append(ln)
    return "\n".join(out).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vlm-run", type=Path, required=True, help="папка/файл VLM out.md")
    ap.add_argument("--ocr-run", type=Path, required=True, help="папка/файл DeepSeek out.md")
    ap.add_argument("--pdf", type=Path, required=True)
    ap.add_argument("--pages", default="45-49")
    ap.add_argument("-o", type=Path, required=True)
    args = ap.parse_args()

    def load_out(p: Path) -> str:
        f = p / "out.md" if p.is_dir() else p
        return f.read_text(encoding="utf-8")

    vlm = split_pages(load_out(args.vlm_run))
    ocr = split_pages(load_out(args.ocr_run))

    # parse pages
    page_nums: list[int] = []
    for part in args.pages.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            page_nums.extend(range(int(a), int(b) + 1))
        else:
            page_nums.append(int(part))

    doc = fitz.open(str(args.pdf))
    blocks = [
        "# ИОС2 · таблицы (стр. 45–49): PDF + DeepSeek-OCR + VLM\n\n"
        "Слои:\n"
        "1. **Текст из PDF** — авторитетные ячейки/числа (цифровой расчёт «Умная вода»)\n"
        "2. **DeepSeek-OCR** — OCR с картинки (заголовки/шапки; плотные числа хуже PDF)\n"
        "3. **Qwen3-VL** — пространственное описание: что за лист, блоки, как устроен\n"
    ]

    for n in page_nums:
        pass_a = extract_pass_a(vlm.get(n, ""))
        ocr_raw = extract_ocr_body(ocr.get(n, ""))
        ocr_clean = clean_tile_markers(ocr_raw)
        pdf_text = normalize_pdf_text(doc[n - 1].get_text("text"))

        # Порядок как в эталоне: факты (PDF/OCR) → описание (VLM)
        chunk = [f"## Страница {n}\n"]
        if pdf_text:
            chunk.append("### Текст / таблица (из PDF — авторитетный слой)\n")
            chunk.append(pdf_text + "\n")
        if ocr_clean:
            chunk.append("### OCR DeepSeek (с изображения)\n")
            chunk.append(ocr_clean + "\n")
        if pass_a:
            chunk.append("### Описание листа (VLM — пространственное)\n")
            chunk.append(pass_a + "\n")
        blocks.append("\n".join(chunk))
        print(f"p{n}: vlm={len(pass_a)} ocr={len(ocr_clean)} pdf={len(pdf_text)}")

    doc.close()
    out = "\n".join(blocks).rstrip() + "\n"
    args.o.write_text(out, encoding="utf-8")
    print(f"wrote {args.o} ({len(out)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
