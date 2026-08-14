#!/usr/bin/env python3
"""Build a self-contained HTML page for eyeball comparison of VLM extractions.

For each fixture: the source page image (embedded base64) + side-by-side
rendered extractions — Gemini ground truth vs the top candidates.

Usage:
    python3 build_compare_html.py                  # default: GT + top-3 of 2026-07 wave
    python3 build_compare_html.py --out my.html

Output: compare_candidates.html next to this script.
"""

import base64
import difflib
import html
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results_local_bench"
GT = HERE / "ground_truth"

# (display name, per-task file resolver, per-task score badge)
FIXTURES = [
    ("01_text_only.png", "TEXT", "ТЕКСТ — ПЗ стр.28 (проза)"),
    ("02_full_of_tables.png", "TABLE", "ТАБЛИЦА — ИОС4 стр.73 (воздушные балансы)"),
    ("03_big_drawing.png", "DRAWING", "ЧЕРТЁЖ — ИОС4 стр.32 (схема отопления, А2)"),
]

CANDIDATES = [
    ("Gemini 2.5 Pro — Ground Truth", "gt", {"TEXT": "эталон", "TABLE": "эталон", "DRAWING": "эталон"}),
    ("№1 Qwen3.5-397B-A17B (облако)", "qwen3.5-397b-a17b",
     {"TEXT": "10/10", "TABLE": "18/18", "DRAWING": "18/18"}),
    ("№2 Qwen3-VL-8B (локальный чемпион)", "qwen3-vl-8b",
     {"TEXT": "10/10", "TABLE": "18/18", "DRAWING": "15/18"}),
    ("№3 Qwen3.5-35B-A3B (кандидат на апгрейд)", "qwen3.5-35b-a3b",
     {"TEXT": "10/10", "TABLE": "18/18", "DRAWING": "14/18"}),
]


def md_to_html(md: str) -> str:
    """Minimal GitHub-flavoured markdown → HTML: tables, headers, lists, emphasis."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    in_ul = False

    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return s

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # table block: header row + separator row
        if (stripped.startswith("|") and i + 1 < len(lines)
                and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1])):
            close_ul()
            out.append("<table>")
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            out.append("<tr>" + "".join(f"<th>{inline(c)}</th>" for c in cells) + "</tr>")
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</table>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            close_ul()
            lvl = min(len(m.group(1)) + 2, 6)  # demote so page h1/h2 stay unique
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
        elif re.match(r"^[-*+]\s+", stripped):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(re.sub(r'^[-*+]\\s+', '', stripped))}</li>")
        elif stripped == "":
            close_ul()
        else:
            close_ul()
            out.append(f"<p>{inline(stripped)}</p>")
        i += 1
    close_ul()
    return "\n".join(out)


def _norm_line(s: str) -> str:
    s = re.sub(r"[|`*#>\-—_:]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def diff_vs_gt(gt_text: str, cand_text: str) -> str:
    """Fuzzy line diff + number-set diff, rendered as a collapsible block."""
    gt_lines = [l.strip() for l in gt_text.splitlines()
                if len(_norm_line(l)) > 3 and not re.match(r"^\|[\s:|-]+\|$", l.strip())]
    cand_norm = [_norm_line(l) for l in cand_text.splitlines() if _norm_line(l)]

    missing_lines = []
    for l in gt_lines:
        n = _norm_line(l)
        best = 0.0
        for c in cand_norm:
            sm = difflib.SequenceMatcher(None, n, c)
            if sm.real_quick_ratio() > best and sm.quick_ratio() > best:
                r = sm.ratio()
                if r > best:
                    best = r
            if best >= 0.72:
                break
        if best < 0.72:
            missing_lines.append(l)

    num_re = re.compile(r"\d+(?:[.,]\d+)?")
    canon = lambda x: x.replace(",", ".")
    gt_nums = {canon(x) for x in num_re.findall(gt_text)}
    cand_nums = {canon(x) for x in num_re.findall(cand_text)}
    missing_nums = sorted(gt_nums - cand_nums, key=lambda x: (len(x), x))
    extra_nums = sorted(cand_nums - gt_nums, key=lambda x: (len(x), x))

    def li(items, cap):
        shown = items[:cap]
        rest = f"<li><i>… и ещё {len(items) - cap}</i></li>" if len(items) > cap else ""
        return "".join(f"<li>{html.escape(str(x))}</li>" for x in shown) + rest

    parts = [f'<details class="diff"><summary>⚠️ vs эталон: строк не найдено {len(missing_lines)}'
             f' · чисел нет {len(missing_nums)} · чисел лишних {len(extra_nums)}</summary>']
    if missing_lines:
        parts.append(f'<div class="dhead miss">Строки эталона, не найденные у кандидата (fuzzy &lt;72%):</div>'
                     f'<ul class="miss">{li(missing_lines, 60)}</ul>')
    if missing_nums:
        parts.append(f'<div class="dhead miss">Числа эталона, отсутствующие у кандидата:</div>'
                     f'<ul class="miss nums">{li(missing_nums, 80)}</ul>')
    if extra_nums:
        parts.append(f'<div class="dhead extra">Числа кандидата, которых нет в эталоне (возможные искажения):</div>'
                     f'<ul class="extra nums">{li(extra_nums, 80)}</ul>')
    if not (missing_lines or missing_nums or extra_nums):
        parts.append("<p>Расхождений не найдено.</p>")
    parts.append("</details>")
    return "".join(parts)


def main() -> None:
    out_path = HERE / "compare_candidates.html"
    if len(sys.argv) >= 3 and sys.argv[1] == "--out":
        out_path = Path(sys.argv[2])

    sections = []
    for png_name, label, title in FIXTURES:
        img_b64 = base64.b64encode((HERE / png_name).read_bytes()).decode()
        gt_text = (GT / png_name.replace(".png", ".md")).read_text(encoding="utf-8")
        cols = []
        for disp, key, scores in CANDIDATES:
            src = GT / png_name.replace(".png", ".md") if key == "gt" \
                else RESULTS / f"{key}.{label}.md"
            cand_text = src.read_text(encoding="utf-8") if src.exists() else ""
            body = md_to_html(cand_text) if cand_text else "<p><i>файл не найден</i></p>"
            diff = "" if key == "gt" or not cand_text else diff_vs_gt(gt_text, cand_text)
            badge = scores.get(label, "")
            cols.append(
                f'<div class="col"><div class="colhead">{html.escape(disp)}'
                f'<span class="badge">{badge}</span></div>'
                f'{diff}<div class="colbody">{body}</div></div>'
            )
        sections.append(f"""
<section id="{label}">
  <h2>{html.escape(title)}</h2>
  <details class="imgbox" open>
    <summary>Исходная страница (клик — свернуть; колесо/скролл внутри — приблизить)</summary>
    <div class="imgscroll"><img src="data:image/png;base64,{img_b64}"
         onclick="this.classList.toggle('zoomed')" alt="{png_name}"></div>
  </details>
  <div class="cols">{''.join(cols)}</div>
</section>""")

    page = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Сравнение VLM-кандидатов — ПТО бенч 2026-07</title>
<style>
  body {{ font: 14px/1.45 -apple-system, sans-serif; margin: 0; background: #f5f6f8; color: #1c1c1e; }}
  header {{ position: sticky; top: 0; z-index: 10; background: #1c2733; color: #fff; padding: 10px 18px; }}
  header h1 {{ font-size: 16px; margin: 0 0 6px; }}
  nav a {{ color: #9fd0ff; margin-right: 14px; text-decoration: none; font-size: 13px; }}
  section {{ padding: 14px 18px 30px; }}
  h2 {{ font-size: 15px; margin: 8px 0; }}
  .imgbox {{ background: #fff; border: 1px solid #d9dde3; border-radius: 8px; margin-bottom: 10px; }}
  .imgbox summary {{ cursor: pointer; padding: 8px 12px; font-size: 13px; color: #555; }}
  .imgscroll {{ overflow: auto; max-height: 60vh; border-top: 1px solid #eee; }}
  .imgscroll img {{ width: 100%; cursor: zoom-in; display: block; }}
  .imgscroll img.zoomed {{ width: 250%; cursor: zoom-out; }}
  .cols {{ display: flex; gap: 10px; overflow-x: auto; align-items: flex-start; }}
  .col {{ flex: 0 0 460px; background: #fff; border: 1px solid #d9dde3; border-radius: 8px;
          max-height: 82vh; display: flex; flex-direction: column; }}
  .colhead {{ position: sticky; top: 0; background: #eef2f7; font-weight: 600; font-size: 13px;
              padding: 8px 10px; border-bottom: 1px solid #d9dde3; border-radius: 8px 8px 0 0; }}
  .badge {{ float: right; background: #2c7be5; color: #fff; border-radius: 10px;
            padding: 1px 8px; font-size: 11px; font-weight: 500; }}
  .colbody {{ overflow: auto; padding: 6px 10px; }}
  .colbody table {{ border-collapse: collapse; font-size: 11.5px; margin: 6px 0; }}
  .colbody th, .colbody td {{ border: 1px solid #c9ced6; padding: 2px 5px; white-space: nowrap; }}
  .colbody th {{ background: #f0f3f7; position: sticky; top: 0; }}
  .colbody h3, .colbody h4, .colbody h5, .colbody h6 {{ margin: 10px 0 4px; }}
  .colbody p {{ margin: 5px 0; }}
  code {{ background: #f0f0f2; padding: 0 3px; border-radius: 3px; }}
  .diff {{ border-bottom: 1px solid #d9dde3; background: #fffdf5; font-size: 12px; }}
  .diff summary {{ cursor: pointer; padding: 6px 10px; color: #8a6d00; font-weight: 600; }}
  .diff .dhead {{ padding: 4px 10px 0; font-weight: 600; }}
  .diff ul {{ margin: 4px 0 8px; padding-left: 26px; max-height: 30vh; overflow: auto; }}
  .diff .miss {{ color: #b3261e; }}
  .diff .extra {{ color: #7a5c00; }}
  .diff ul.nums {{ display: flex; flex-wrap: wrap; gap: 2px 14px; list-style: none; padding-left: 10px; }}
</style></head><body>
<header>
  <h1>ПТО VLM-бенч · ручное сравнение извлечений · 12-13.07.2026</h1>
  <nav><a href="#TEXT">Текст</a><a href="#TABLE">Таблица</a><a href="#DRAWING">Чертёж</a></nav>
</header>
{''.join(sections)}
</body></html>"""

    out_path.write_text(page, encoding="utf-8")
    print(f"OUT={out_path}")
    print(f"SIZE_KB={out_path.stat().st_size // 1024}")


if __name__ == "__main__":
    main()
