#!/usr/bin/env python3
"""Генератор HTML quality-viewer: PDF-страница + Markdown модели рядом.

Чистый HTML + document.pdf рядом. Открывается двойным кликом (file://),
без HTTP-сервера. PDF показывает встроенный просмотрщик браузера
(зум с улучшением качества как в обычном PDF).

  python build_quality_viewer.py \\
    --pdf "new_files/Раздел ПД №5 Подраздел №2 (ИОС2).pdf" \\
    --md archive/измерения/ИОС2_итог.md \\
    -o quality_viewer_ios2/index.html
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from pathlib import Path

import fitz  # PyMuPDF — только для числа страниц / валидации
import markdown as md_lib

ROOT = Path(__file__).resolve().parent


def split_md_pages(text: str) -> dict[int, str]:
    parts = re.split(r"(?m)^##\s+Страница\s+(\d+)\s*$", text)
    out: dict[int, str] = {}
    for i in range(1, len(parts), 2):
        out[int(parts[i])] = parts[i + 1].strip() if i + 1 < len(parts) else ""
    return out


def md_to_html(text: str) -> str:
    return md_lib.markdown(
        text,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
        output_format="html5",
    )


def parse_pages_arg(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    wanted: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            wanted.extend(range(int(a), int(b) + 1))
        else:
            wanted.append(int(part))
    return wanted


def build_html(
    *,
    title: str,
    pages_meta: list[dict],
    pdf_rel: str,
) -> str:
    pages_json = json.dumps(
        [{"num": p["num"], "chars": p["chars"]} for p in pages_meta],
        ensure_ascii=False,
    )
    articles = []
    for p in pages_meta:
        articles.append(
            f'<article class="page-panel" data-page="{p["num"]}" hidden>\n'
            f'  <div class="md-body">{p["html_body"]}</div>\n'
            f"</article>"
        )
    articles_html = "\n".join(articles)
    nav_items = "\n".join(
        (
            f'<button type="button" class="nav-item" data-page="{p["num"]}">'
            f'<span class="n">{p["num"]}</span>'
            f'<span class="meta">{p["chars"]:,} зн.</span></button>'
        ).replace(",", " ")
        for p in pages_meta
    )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(title)} · Quality Viewer</title>
<style>
:root {{
  --bg: #0e1117;
  --panel: #161b22;
  --border: #30363d;
  --text: #e6edf3;
  --muted: #8b949e;
  --accent: #58a6ff;
  --accent-dim: #1f6feb;
  --nav-w: 200px;
}}
* {{ box-sizing: border-box; }}
html, body {{ height: 100%; margin: 0; }}
body {{
  font-family: "Segoe UI", system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}}
header {{
  display: flex; align-items: center; gap: 16px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--panel);
  flex-shrink: 0;
}}
header h1 {{
  font-size: 15px; font-weight: 600; margin: 0;
  flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
header .hint {{ font-size: 12px; color: var(--muted); }}
.toolbar {{ display: flex; gap: 8px; align-items: center; }}
.toolbar button {{
  background: #21262d; color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 13px;
}}
.toolbar button:hover {{ border-color: var(--accent); }}
.toolbar button:disabled {{ opacity: 0.4; cursor: default; }}
#pageLabel {{ min-width: 90px; text-align: center; font-variant-numeric: tabular-nums; }}

.layout {{
  display: grid;
  grid-template-columns: var(--nav-w) 1fr 1fr;
  flex: 1; min-height: 0;
}}
nav.pages {{
  border-right: 1px solid var(--border);
  overflow-y: auto; background: var(--panel);
  padding: 8px;
}}
.nav-item {{
  display: flex; width: 100%; justify-content: space-between; align-items: center;
  background: transparent; border: 1px solid transparent; color: var(--text);
  border-radius: 6px; padding: 8px 10px; margin-bottom: 4px; cursor: pointer;
  font-size: 13px; text-align: left;
}}
.nav-item:hover {{ background: #21262d; }}
.nav-item.active {{
  background: rgba(31, 111, 235, 0.25);
  border-color: var(--accent-dim);
}}
.nav-item .n {{ font-weight: 600; }}
.nav-item .meta {{ color: var(--muted); font-size: 11px; }}

.viewer, .md-pane {{
  min-width: 0; min-height: 0; display: flex; flex-direction: column;
  border-right: 1px solid var(--border);
}}
.md-pane {{ border-right: none; }}
.pane-head {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 12px; border-bottom: 1px solid var(--border);
  background: #0d1117; font-size: 12px; color: var(--muted); flex-shrink: 0;
  gap: 8px;
}}
.pane-head strong {{ color: var(--text); font-size: 13px; }}

.pdf-frame-wrap {{
  flex: 1; min-height: 0; background: #010409;
}}
#pdfFrame {{
  width: 100%; height: 100%; border: 0; background: #525659;
}}

.md-scroll {{
  flex: 1; overflow: auto; padding: 16px 20px 40px;
  background: var(--bg);
}}
.md-body {{
  max-width: 720px; line-height: 1.55; font-size: 14px;
}}
.md-body h1, .md-body h2, .md-body h3, .md-body h4 {{
  margin: 1.2em 0 0.4em; line-height: 1.25;
}}
.md-body h1 {{ font-size: 1.35rem; }}
.md-body h2 {{ font-size: 1.2rem; border-bottom: 1px solid var(--border); padding-bottom: 4px; }}
.md-body h3 {{ font-size: 1.05rem; color: var(--accent); }}
.md-body p {{ margin: 0.5em 0; }}
.md-body ul, .md-body ol {{ padding-left: 1.4em; }}
.md-body li {{ margin: 0.25em 0; }}
.md-body table {{
  border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 12px;
}}
.md-body th, .md-body td {{
  border: 1px solid var(--border); padding: 6px 8px; text-align: left;
}}
.md-body th {{ background: #21262d; }}
.md-body code {{
  background: #21262d; padding: 1px 5px; border-radius: 4px; font-size: 0.9em;
}}
.md-body pre {{
  background: #21262d; padding: 12px; border-radius: 8px; overflow-x: auto;
}}
.md-body strong {{ color: #ffa657; }}
.page-panel[hidden] {{ display: none; }}

@media (max-width: 1100px) {{
  .layout {{ grid-template-columns: 160px 1fr; grid-template-rows: 45vh 1fr; }}
  .viewer {{ grid-column: 2; grid-row: 1; border-bottom: 1px solid var(--border); }}
  .md-pane {{ grid-column: 1 / -1; grid-row: 2; }}
  nav.pages {{ grid-row: 1 / 3; }}
}}
</style>
</head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <span class="hint">зум PDF — внутри панели слева · ← → — страницы</span>
  <div class="toolbar">
    <button type="button" id="btnPrev" title="Предыдущая (←)">←</button>
    <span id="pageLabel">1 / 1</span>
    <button type="button" id="btnNext" title="Следующая (→)">→</button>
  </div>
</header>

<div class="layout">
  <nav class="pages" id="nav" aria-label="Страницы">
{nav_items}
  </nav>

  <section class="viewer">
    <div class="pane-head">
      <strong>PDF</strong>
      <span>встроенный просмотрщик браузера</span>
    </div>
    <div class="pdf-frame-wrap">
      <iframe id="pdfFrame" title="PDF page"></iframe>
    </div>
  </section>

  <section class="md-pane">
    <div class="pane-head">
      <strong>Распознавание модели (.md)</strong>
      <span id="mdMeta"></span>
    </div>
    <div class="md-scroll" id="mdScroll">
{articles_html}
    </div>
  </section>
</div>

<script>
const PAGES = {pages_json};
const PDF_URL = {json.dumps(pdf_rel)};
let idx = 0;

const frame = document.getElementById("pdfFrame");
const pageLabel = document.getElementById("pageLabel");
const mdMeta = document.getElementById("mdMeta");
const btnPrev = document.getElementById("btnPrev");
const btnNext = document.getElementById("btnNext");

function pdfSrc(pageNum) {{
  // Chrome/Edge: #page=N; view=FitH — ширина страницы
  return PDF_URL + "#page=" + pageNum + "&view=FitH";
}}

function show(i) {{
  if (i < 0 || i >= PAGES.length) return;
  idx = i;
  const p = PAGES[idx];
  document.querySelectorAll(".nav-item").forEach((el) => {{
    el.classList.toggle("active", Number(el.dataset.page) === p.num);
  }});
  document.querySelectorAll(".page-panel").forEach((el) => {{
    el.hidden = Number(el.dataset.page) !== p.num;
  }});
  pageLabel.textContent = p.num + " / " + PAGES[PAGES.length - 1].num;
  mdMeta.textContent = p.chars.toLocaleString("ru-RU") + " знаков";
  btnPrev.disabled = idx === 0;
  btnNext.disabled = idx === PAGES.length - 1;
  frame.src = pdfSrc(p.num);
  document.getElementById("mdScroll").scrollTop = 0;
  const navBtn = document.querySelector(`.nav-item[data-page="${{p.num}}"]`);
  if (navBtn) navBtn.scrollIntoView({{ block: "nearest" }});
}}

btnPrev.onclick = () => show(idx - 1);
btnNext.onclick = () => show(idx + 1);

document.querySelectorAll(".nav-item").forEach((el) => {{
  el.addEventListener("click", () => {{
    const n = Number(el.dataset.page);
    const i = PAGES.findIndex((p) => p.num === n);
    if (i >= 0) show(i);
  }});
}});

window.addEventListener("keydown", (e) => {{
  if (e.key === "ArrowLeft") {{ e.preventDefault(); show(idx - 1); }}
  if (e.key === "ArrowRight") {{ e.preventDefault(); show(idx + 1); }}
}});

show(0);
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="PDF + MD quality viewer (чистый HTML, без сервера)")
    ap.add_argument("--pdf", type=Path, required=True)
    ap.add_argument("--md", type=Path, required=True, help="итог .md (## Страница N)")
    ap.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="путь к index.html (по умолчанию quality_viewer_<stem>/index.html)",
    )
    ap.add_argument(
        "--pages",
        default=None,
        help="опционально: 1-10,34,45-49 — иначе все из MD∩PDF",
    )
    args = ap.parse_args()

    pdf_path = args.pdf.resolve()
    md_path = args.md.resolve()
    if not pdf_path.exists():
        raise SystemExit(f"нет PDF: {pdf_path}")
    if not md_path.exists():
        raise SystemExit(f"нет MD: {md_path}")

    out_html = args.out or (ROOT / f"quality_viewer_{pdf_path.stem[:40]}" / "index.html")
    out_html = out_html.resolve()
    out_dir = out_html.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    dest_pdf = out_dir / "document.pdf"
    if dest_pdf.resolve() != pdf_path:
        print(f"copy PDF → {dest_pdf.name} …", flush=True)
        shutil.copy2(pdf_path, dest_pdf)

    # Убрать артефакты прошлого режима (сервер / PDF.js / PNG)
    for extra in ("open_viewer.bat",):
        p = out_dir / extra
        if p.exists():
            p.unlink()
            print(f"removed {extra}", flush=True)
    old_pages = out_dir / "pages"
    if old_pages.is_dir():
        shutil.rmtree(old_pages)
        print("removed legacy pages/", flush=True)

    md_pages = split_md_pages(md_path.read_text(encoding="utf-8"))
    doc = fitz.open(str(pdf_path))
    wanted = parse_pages_arg(args.pages)

    page_nums = sorted(md_pages.keys())
    if wanted is not None:
        wanted_set = set(wanted)
        page_nums = [n for n in page_nums if n in wanted_set]
    page_nums = [n for n in page_nums if 1 <= n <= doc.page_count]
    doc.close()

    meta: list[dict] = []
    title = f"{pdf_path.stem}  ↔  {md_path.name}"
    for n in page_nums:
        body = md_pages.get(n, "")
        print(f"  p{n}: md={len(body)} chars", flush=True)
        meta.append(
            {
                "num": n,
                "html_body": md_to_html(body) if body.strip() else "<p><em>(нет текста в .md)</em></p>",
                "chars": len(body),
            }
        )

    html_doc = build_html(title=title, pages_meta=meta, pdf_rel="document.pdf")
    out_html.write_text(html_doc, encoding="utf-8")

    print(f"wrote {out_html} ({len(page_nums)} pages)", flush=True)
    print(f"open:  {out_html}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
