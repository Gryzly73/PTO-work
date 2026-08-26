#!/usr/bin/env python3
"""Просмотрщик сверки для чертежей: лист DWG слева, наш разбор справа.

Зачем. У PDF такая сверка уже есть — `build_quality_viewer.py` показывает
страницу оригинала рядом с выводом модели. У чертежей сверять было нечем:
DWG браузер не открывает, а глазами по markdown не поймёшь, всё ли с листа
выбито и туда ли встали подписи.

Здесь на вход идёт САМ DWG — конвертация и разбор происходят внутри, как в
конвейере. Слева рисуется лист (тот же SVG, что уходит интерфейсу), справа —
markdown этого листа. Видно сразу: пустой ли лист, встала ли подпись на своё
место, что попало в «расхождения» со вторым путём чтения.

Открывается двойным кликом, без сервера.

  python build_dwg_viewer.py "1. Стадия П DWG/2 - ПЗУ/Жуковский 1_ПЗУ.dwg"
  python build_dwg_viewer.py "1. Стадия П DWG/2 - ПЗУ" -o сверка_пзу

Папка на входе означает «все DWG внутри, рекурсивно».
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import time
import traceback
from pathlib import Path

import markdown as md_lib

ROOT = Path(__file__).resolve().parent


def md_to_html(text: str) -> str:
    return md_lib.markdown(
        text,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
        output_format="html5",
    )


def collect(paths: list[Path]) -> list[Path]:
    """Список DWG/DXF по путям: файл берём как есть, папку разворачиваем."""
    found: list[Path] = []
    for item in paths:
        if item.is_dir():
            found += sorted(
                p for p in item.rglob("*") if p.suffix.lower() in (".dwg", ".dxf")
            )
        elif item.suffix.lower() in (".dwg", ".dxf"):
            found.append(item)
    return found


def sheet_records(source: Path, out_dir: Path, index: int) -> list[dict]:
    """Листы одного чертежа: картинка на диск, markdown в память."""
    import dwg_sheets
    from dwg_render import sheet_preview

    dxf, sheets = dwg_sheets.sheets_for(source)
    records = []
    for number, sheet in enumerate(sheets, 1):
        stem = f"{index:02d}_{number:03d}"
        image = ""
        # Лист без окон и без своего layout'а — служебный список подписей
        # («Текст из исходного DWG»): координат у них нет, и картинка вышла бы
        # кучей строк в одной точке.
        drawable = bool(sheet.windows or sheet.layout_name)
        if drawable:
            try:
                svg = sheet_preview(source, number, "svg")
                if svg:
                    target = out_dir / "sheets" / f"{stem}.svg"
                    target.write_text(svg, encoding="utf-8")
                    image = f"sheets/{stem}.svg"
            except Exception as error:
                print(f"    лист {number}: картинка не вышла — {error}", flush=True)
        try:
            markdown, _kind = dwg_sheets.page_markdown(source, number)
        except Exception as error:
            markdown = f"**Разбор листа не удался:** `{error}`"
        records.append(
            {
                "file": source.name,
                "sheet": number,
                "name": sheet.name,
                "title": sheet.title,
                "texts": len(sheet.texts),
                "note": sheet.note,
                "state": dwg_sheets.sheet_content(sheet),
                "image": image,
                "html": md_to_html(markdown),
            }
        )
    return records


# Как называется состояние листа в шапке. Совпадает с плашкой на картинке —
# чтобы глазами сходилось одно с другим.
STATE_LABEL = {
    "text": "",
    "drawing": "только графика",
    "blank": "пуст в чертеже",
    "lost": "потерян конвертером",
}


def build(records: list[dict], out_dir: Path, title: str) -> Path:
    nav = "\n".join(
        '<button type="button" class="nav-item" data-idx="{i}">'
        '<span class="nav-num">{n}</span>'
        '<span class="nav-name">{name}</span>'
        '<span class="nav-meta">{file} · {texts} подписей{state}</span>'
        "</button>".format(
            i=i,
            n=item["sheet"],
            name=html.escape(item["name"][:38]),
            file=html.escape(item["file"][:28]),
            texts=item["texts"],
            state=(
                f' · {STATE_LABEL[item["state"]]}'
                if STATE_LABEL.get(item["state"])
                else ""
            ),
        )
        for i, item in enumerate(records)
    )
    articles = "\n".join(
        f'<article class="sheet-panel" data-idx="{i}" hidden>{item["html"]}</article>'
        for i, item in enumerate(records)
    )
    payload = json.dumps(
        [
            {
                "image": item["image"],
                "name": item["name"],
                "file": item["file"],
                "note": item["note"],
                "state": item["state"],
                "texts": item["texts"],
            }
            for item in records
        ],
        ensure_ascii=False,
    )

    page = _TEMPLATE.format(
        title=html.escape(title),
        nav=nav,
        articles=articles,
        payload=payload,
        total=len(records),
    )
    target = out_dir / "index.html"
    target.write_text(page, encoding="utf-8")
    return target


_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg: #0d1117; --panel: #161b22; --border: #30363d;
  --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff; --warn: #ffa657;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
  height: 100vh; display: flex; flex-direction: column;
}}
header {{
  display: flex; align-items: center; gap: 16px; padding: 10px 16px;
  border-bottom: 1px solid var(--border); background: var(--panel);
}}
header h1 {{ font-size: 15px; margin: 0; font-weight: 600; }}
.hint {{ color: var(--muted); font-size: 12px; }}
.toolbar {{ margin-left: auto; display: flex; align-items: center; gap: 8px; }}
button {{
  background: #21262d; color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 10px; cursor: pointer; font-size: 13px;
}}
button:hover {{ border-color: var(--accent); }}
.layout {{ flex: 1; display: grid; grid-template-columns: 280px 1fr 1fr; min-height: 0; }}
nav.sheets {{ overflow-y: auto; border-right: 1px solid var(--border); background: var(--panel); }}
.nav-item {{
  display: block; width: 100%; text-align: left; border: 0; border-bottom: 1px solid var(--border);
  border-radius: 0; padding: 8px 12px; background: transparent;
}}
.nav-item.active {{ background: #1f6feb22; border-left: 3px solid var(--accent); }}
.nav-num {{ color: var(--muted); font-size: 11px; display: block; }}
.nav-name {{ display: block; font-weight: 600; }}
.nav-meta {{ display: block; color: var(--muted); font-size: 11px; }}
section {{ display: flex; flex-direction: column; min-width: 0; min-height: 0; }}
.pane-head {{
  display: flex; gap: 10px; align-items: baseline; padding: 6px 12px;
  border-bottom: 1px solid var(--border); background: var(--panel); font-size: 12px;
}}
.pane-head strong {{ font-size: 13px; }}
.pane-head span {{ color: var(--muted); }}
.drawing {{ flex: 1; overflow: auto; background: #fff; padding: 10px; }}
.drawing img {{ display: block; width: var(--zoom, 100%); max-width: none; }}
.drawing .missing {{ color: #57606a; padding: 40px; text-align: center; font-size: 13px; }}
.md-scroll {{ flex: 1; overflow-y: auto; padding: 14px 18px; border-left: 1px solid var(--border); }}
.note {{ color: var(--warn); font-size: 12px; padding: 6px 12px; border-bottom: 1px solid var(--border); }}
.note:empty {{ display: none; }}
.sheet-panel[hidden] {{ display: none; }}
.sheet-panel h2, .sheet-panel h3 {{ font-size: 14px; color: var(--accent); margin: 14px 0 6px; }}
.sheet-panel table {{ border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 13px; }}
.sheet-panel th, .sheet-panel td {{ border: 1px solid var(--border); padding: 4px 7px; text-align: left; }}
.sheet-panel th {{ background: #21262d; }}
.sheet-panel code {{ background: #21262d; padding: 1px 5px; border-radius: 4px; }}
.sheet-panel strong {{ color: var(--warn); }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <span class="hint">← → — листы · +/− — масштаб чертежа</span>
  <div class="toolbar">
    <button type="button" id="zoomOut">−</button>
    <span id="zoomLabel">100%</span>
    <button type="button" id="zoomIn">+</button>
    <button type="button" id="prev">←</button>
    <span id="label">1 / {total}</span>
    <button type="button" id="next">→</button>
  </div>
</header>

<div class="layout">
  <nav class="sheets" id="nav" aria-label="Листы">
{nav}
  </nav>

  <section>
    <div class="pane-head"><strong>Чертёж</strong><span id="drawMeta"></span></div>
    <div class="note" id="note"></div>
    <div class="drawing" id="drawing"></div>
  </section>

  <section>
    <div class="pane-head"><strong>Наш разбор</strong><span id="mdMeta"></span></div>
    <div class="md-scroll" id="mdScroll">
{articles}
    </div>
  </section>
</div>

<script>
const SHEETS = {payload};
let idx = 0, zoom = 100;

const nav = document.getElementById("nav");
const drawing = document.getElementById("drawing");
const note = document.getElementById("note");
const label = document.getElementById("label");
const zoomLabel = document.getElementById("zoomLabel");
const drawMeta = document.getElementById("drawMeta");
const mdMeta = document.getElementById("mdMeta");

function show(next) {{
  idx = Math.max(0, Math.min(SHEETS.length - 1, next));
  const item = SHEETS[idx];
  drawing.innerHTML = item.image
    ? '<img alt="лист" src="' + item.image + '">'
    : '<div class="missing">картинка листа не построена</div>';
  applyZoom();
  note.textContent = item.note || "";
  label.textContent = (idx + 1) + " / " + SHEETS.length;
  drawMeta.textContent = item.file;
  mdMeta.textContent = item.name + " · подписей: " + item.texts;
  document.querySelectorAll(".sheet-panel").forEach((el) => {{
    el.hidden = Number(el.dataset.idx) !== idx;
  }});
  document.querySelectorAll(".nav-item").forEach((el) => {{
    el.classList.toggle("active", Number(el.dataset.idx) === idx);
  }});
  const active = nav.querySelector(".nav-item.active");
  if (active) active.scrollIntoView({{ block: "nearest" }});
  document.getElementById("mdScroll").scrollTop = 0;
}}

function applyZoom() {{
  drawing.style.setProperty("--zoom", zoom + "%");
  zoomLabel.textContent = zoom + "%";
}}

nav.addEventListener("click", (event) => {{
  const button = event.target.closest(".nav-item");
  if (button) show(Number(button.dataset.idx));
}});
document.getElementById("prev").onclick = () => show(idx - 1);
document.getElementById("next").onclick = () => show(idx + 1);
document.getElementById("zoomIn").onclick = () => {{ zoom = Math.min(1600, zoom + 25); applyZoom(); }};
document.getElementById("zoomOut").onclick = () => {{ zoom = Math.max(25, zoom - 25); applyZoom(); }};
document.addEventListener("keydown", (event) => {{
  if (event.key === "ArrowLeft") show(idx - 1);
  if (event.key === "ArrowRight") show(idx + 1);
  if (event.key === "+" || event.key === "=") {{ zoom = Math.min(1600, zoom + 25); applyZoom(); }}
  if (event.key === "-") {{ zoom = Math.max(25, zoom - 25); applyZoom(); }}
}});
show(0);
</script>
</body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path, help="DWG/DXF или папка с ними")
    parser.add_argument("-o", "--out", type=Path, default=Path("dwg_viewer"))
    parser.add_argument("--title", default="Сверка чертежа с разбором")
    args = parser.parse_args()

    files = collect(args.sources)
    if not files:
        print("Не нашёл ни одного DWG или DXF по указанным путям")
        return 1

    out_dir = args.out
    (out_dir / "sheets").mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for index, source in enumerate(files, 1):
        started = time.time()
        print(f"[{index}/{len(files)}] {source.name} …", flush=True)
        try:
            records += sheet_records(source, out_dir, index)
        except Exception as error:
            print(f"    не разобрался: {type(error).__name__}: {error}", flush=True)
            traceback.print_exc(limit=2)
            continue
        print(f"    листов: {len(records)} всего ({time.time() - started:.1f} с)", flush=True)

    if not records:
        print("Ни одного листа не разобралось — смотреть нечего")
        return 1
    target = build(records, out_dir, args.title)
    print(f"\nготово: {target}")
    print(f"листов: {len(records)} из файлов: {len(files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
