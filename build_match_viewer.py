"""Постраничная сверка выгрузки модели с исходным PDF.

Отвечает на вопрос «совпадают ли данные»: слева текст документа, справа то,
что выдала модель, и всё расхождение подсвечено потокенно.

  оранжевым в левой колонке — есть в документе, но модель это потеряла;
  красным в правой   — есть у модели, но в документе такого нет (выдумка);
  обычным            — совпало.

Эталон берётся из текстового слоя; для листов со сломанным ToUnicode
кодировка чинится через deglyph (подстановка выводится по самому документу,
без участия проверяемой выгрузки — иначе эталон зависел бы от гипотезы).

    python build_match_viewer.py --md <out.md> --pdf <file.pdf> [--pages 1-33]
"""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

import fitz

from build_ios2_md import is_garbled_pdf_text
from score_vs_pdftext import (
    _glue_spaced_numbers,
    norm_num,
    norm_word,
    parse_pages,
    split_pages_md,
    strip_pdf_sourced,
    tokens_of,
)

TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{3,}|\d+(?:[.,]\d+)?", re.UNICODE)


def _norm(tok: str) -> str:
    return norm_num(tok) if tok[0].isdigit() else norm_word(tok)


def _significant(tok: str) -> bool:
    """Однозначные числа и короткие слова шумят — их не подсвечиваем."""
    if tok[0].isdigit():
        return len(norm_num(tok).lstrip("-").replace(".", "")) >= 2
    return len(norm_word(tok)) >= 3


def mark(text: str, other: set[str], cls: str, limit: int = 24000) -> str:
    """Размечает токены текста, которых нет в множестве other."""
    text = _glue_spaced_numbers(text)[:limit]
    out: list[str] = []
    pos = 0
    for m in TOKEN_RE.finditer(text):
        out.append(html.escape(text[pos : m.start()]))
        tok = m.group()
        if _significant(tok) and _norm(tok) not in other:
            out.append(f'<mark class="{cls}">{html.escape(tok)}</mark>')
        else:
            out.append(html.escape(tok))
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


PAGE_TPL = """<section class="pg" data-page="{page}" data-loss="{loss}" data-inv="{inv}">
<h2>Страница {page}
  <span class="badge {cls} v-full">{verdict}</span>
  <span class="badge {cls_v} v-vlm hide">{verdict_v}</span></h2>
<div class="m">
  <span title="доля слов и чисел документа, попавших в выгрузку">полнота
    <b class="v-full">{recall}%</b><b class="v-vlm hide">{recall_v}%</b></span>
  <span title="доля чисел выгрузки, которые есть в документе">точность чисел
    <b class="v-full">{prec}%</b><b class="v-vlm hide">{prec_v}%</b></span>
  <span>потеряно: <b class="v-full">{loss}</b><b class="v-vlm hide">{loss_v}</b></span>
  <span>лишних: <b class="v-full">{inv}</b><b class="v-vlm hide">{inv_v}</b></span>
  {note}
</div>
<div class="cols">
  <div class="col">
    <h3>Документ (текстовый слой PDF)</h3>
    <pre class="v-full">{left}</pre><pre class="v-vlm hide">{left_v}</pre></div>
  <div class="col">
    <h3><span class="v-full">Итоговый документ</span
      ><span class="v-vlm hide">Только то, что написала модель</span></h3>
    <pre class="v-full">{right}</pre><pre class="v-vlm hide">{right_v}</pre></div>
</div>
</section>"""


def build(md_path: Path, pdf_path: Path, pages_spec: str | None, out: Path) -> dict:
    md_text = md_path.read_text(encoding="utf-8")
    pages_md = split_pages_md(md_text)
    doc = fitz.open(pdf_path)

    glyph_map: dict[str, str] = {}
    try:
        from deglyph import build_mapping, coverage, verify

        m = build_mapping(doc)  # только по PDF, без проверяемой выгрузки
        if m and (coverage(doc, m)["pct"] or 0) >= 90 and (verify(doc, m)["pct"] or 0) >= 80:
            glyph_map = m
    except Exception:
        pass

    want = (
        parse_pages(pages_spec, doc.page_count)
        if pages_spec
        else sorted(pages_md.keys())
    )
    sections: list[str] = []
    stats: list[dict] = []
    for p in want:
        if p not in pages_md:
            continue
        ref = doc[p - 1].get_text("text")
        note = ""
        if glyph_map and is_garbled_pdf_text(ref):
            fixed = "".join(glyph_map.get(c, c) for c in ref)
            if not is_garbled_pdf_text(fixed):
                ref, note = fixed, (
                    '<span class="tag">кодировка листа восстановлена</span>'
                )
        if len(ref.strip()) < 40 or is_garbled_pdf_text(ref):
            continue
        rw, rn, _ = tokens_of(ref)
        ref_set = rw | rn

        def measure(hyp_text: str):
            hw, hn, _ = tokens_of(hyp_text)
            hyp_set = hw | hn
            rec = (
                round(100 * len(ref_set & hyp_set) / len(ref_set), 1)
                if ref_set
                else 0.0
            )
            pr = round(100 * len(rn & hn) / len(hn), 1) if hn else 100.0
            v, c = (
                ("совпадает", "ok")
                if rec >= 90 and pr >= 95
                else ("расхождения", "warn")
                if rec >= 70
                else ("много потерь", "bad")
            )
            return {
                "set": hyp_set,
                "recall": rec,
                "prec": pr,
                "loss": len([t for t in ref_set if t not in hyp_set]),
                "inv": len([t for t in hn if t not in rn]),
                "verdict": v,
                "cls": c,
            }

        full_txt = pages_md[p]           # что реально отдаётся дальше по конвейеру
        vlm_txt = strip_pdf_sourced(full_txt)  # только сочинённое моделью
        f, v = measure(full_txt), measure(vlm_txt)

        sections.append(
            PAGE_TPL.format(
                page=p,
                note=note,
                recall=f["recall"], prec=f["prec"], loss=f["loss"], inv=f["inv"],
                verdict=f["verdict"], cls=f["cls"],
                recall_v=v["recall"], prec_v=v["prec"], loss_v=v["loss"],
                inv_v=v["inv"], verdict_v=v["verdict"], cls_v=v["cls"],
                left=mark(ref, f["set"], "lost"),
                left_v=mark(ref, v["set"], "lost"),
                right=mark(full_txt, ref_set, "inv"),
                right_v=mark(vlm_txt, ref_set, "inv"),
            )
        )
        stats.append(
            {
                "page": p,
                "recall": f["recall"], "prec": f["prec"],
                "loss": f["loss"], "inv": f["inv"],
                "vlm_recall": v["recall"], "vlm_prec": v["prec"],
            }
        )
    doc.close()

    n = len(stats) or 1
    avg_r = round(sum(s["recall"] for s in stats) / n, 1)
    avg_p = round(sum(s["prec"] for s in stats) / n, 1)
    avg_rv = round(sum(s["vlm_recall"] for s in stats) / n, 1)
    avg_pv = round(sum(s["vlm_prec"] for s in stats) / n, 1)
    good = sum(1 for s in stats if s["recall"] >= 90 and s["prec"] >= 95)
    head = """<!DOCTYPE html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Сверка выгрузки с исходником</title>
<style>
:root{{color-scheme:light;--bg:#f9f9f7;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
 --mut:#898781;--line:rgba(11,11,11,.10);--acc:#2a78d6;--ok:#0ca30c;--warn:#fab219;
 --bad:#d03b3b;--lost:#ffe2c2;--inv:#ffd4d1;--chip:#f0efec}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0d0d0d;--card:#1a1a19;--ink:#fff;
 --ink2:#c3c2b7;--mut:#898781;--line:rgba(255,255,255,.10);--acc:#3987e5;
 --lost:#5a3c12;--inv:#5c2320;--chip:#26262b}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);padding:24px 18px 60px;
 font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}}
.wrap{{max-width:1500px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 4px}}
.sub{{color:var(--ink2);font-size:13px;margin-bottom:18px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;
 margin-bottom:16px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 15px}}
.kpi b{{display:block;font-size:23px;font-weight:600}}
.kpi span{{color:var(--mut);font-size:12px}}
.legend{{background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:12px 15px;margin-bottom:16px;font-size:13.5px}}
.ctl{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}}
button{{font:inherit;font-size:13.5px;cursor:pointer;background:var(--card);
 color:var(--ink);border:1px solid var(--line);border-radius:999px;padding:6px 14px}}
button[aria-pressed=true]{{background:var(--acc);border-color:var(--acc);color:#fff}}
.pg{{background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:14px 16px;margin-bottom:14px}}
h2{{font-size:16px;margin:0 0 6px}} h3{{font-size:12px;color:var(--mut);margin:0 0 6px;
 text-transform:uppercase;letter-spacing:.04em}}
.badge{{font-size:12px;padding:2px 9px;border-radius:999px;background:var(--chip);
 color:var(--ink2);font-weight:600}}
.badge.ok{{background:#0ca30c22;color:var(--ok)}}
.badge.warn{{background:#fab21922;color:#9a6b00}}
.badge.bad{{background:#d03b3b22;color:var(--bad)}}
@media(prefers-color-scheme:dark){{.badge.warn{{color:var(--warn)}}}}
.m{{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--ink2);
 margin-bottom:10px}}
.tag{{background:var(--chip);border-radius:6px;padding:1px 8px}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
@media(max-width:1000px){{.cols{{grid-template-columns:1fr}}}}
.col{{min-width:0}}
pre{{white-space:pre-wrap;word-break:break-word;font:12.5px/1.5 ui-monospace,
 "Cascadia Code",Consolas,monospace;background:var(--bg);border:1px solid var(--line);
 border-radius:9px;padding:11px;margin:0;max-height:460px;overflow:auto}}
mark{{border-radius:3px;padding:0 2px;color:inherit}}
mark.lost{{background:var(--lost)}} mark.inv{{background:var(--inv)}}
.hide{{display:none}}
</style>
<div class="wrap">
<h1>Сверка выгрузки модели с исходным PDF</h1>
<div class="sub">{md_name} · {pdf_name} · страниц сверено: {n}</div>
<div class="ctl">
 <span style="color:var(--mut);font-size:13px">Что сверяем:</span>
 <button data-v="full" aria-pressed="true">итоговый документ</button>
 <button data-v="vlm" aria-pressed="false">только вывод модели</button>
</div>
<div class="kpis">
 <div class="kpi"><b class="v-full">{avg_r}%</b><b class="v-vlm hide">{avg_rv}%</b>
  <span>полнота в среднем — сколько данных документа попало в выгрузку</span></div>
 <div class="kpi"><b class="v-full">{avg_p}%</b><b class="v-vlm hide">{avg_pv}%</b>
  <span>точность чисел — сколько чисел выгрузки есть в документе</span></div>
 <div class="kpi"><b>{good} из {n}</b>
  <span>страниц итогового документа сошлись полностью (полнота ≥90%, точность ≥95%)</span></div>
</div>
<div class="legend">
 <mark class="lost">оранжевый слева</mark> — есть в документе, но в выгрузку не попало ·
 <mark class="inv">красный справа</mark> — в выгрузке есть, а в документе нет ·
 без подсветки — совпало.
 <div style="color:var(--mut);margin-top:6px">
 <b>Итоговый документ</b> — то, что реально уходит дальше по конвейеру: текст и
 таблицы из слоя PDF плюс описание модели. <b>Только вывод модели</b> — что модель
 написала сама, без данных из слоя; полнота здесь заведомо ниже, потому что
 описание не обязано повторять весь текст листа, и смотреть в этом режиме нужно
 прежде всего на точность чисел. Сравниваются слова от трёх букв и числа от двух
 знаков.</div>
</div>
<div class="ctl">
 <span style="color:var(--mut);font-size:13px">Показать:</span>
 <button data-f="all" aria-pressed="true">все страницы</button>
 <button data-f="loss" aria-pressed="false">где есть потери</button>
 <button data-f="inv" aria-pressed="false">где есть выдумки</button>
</div>
"""
    tail = """</div>
<script>
// переключатель «итоговый документ / только вывод модели»
const vb=[...document.querySelectorAll('.ctl button[data-v]')];
vb.forEach(b=>b.onclick=()=>{
  vb.forEach(x=>x.setAttribute('aria-pressed',x===b));
  const vlm = b.dataset.v==='vlm';
  document.querySelectorAll('.v-full').forEach(e=>e.classList.toggle('hide', vlm));
  document.querySelectorAll('.v-vlm').forEach(e=>e.classList.toggle('hide', !vlm));
});
// фильтр страниц
const fb=[...document.querySelectorAll('.ctl button[data-f]')];
fb.forEach(b=>b.onclick=()=>{
  fb.forEach(x=>x.setAttribute('aria-pressed',x===b));
  const f=b.dataset.f;
  document.querySelectorAll('.pg').forEach(s=>{
    const loss=+s.dataset.loss, inv=+s.dataset.inv;
    s.classList.toggle('hide',
      !(f==='all' || (f==='loss'&&loss>0) || (f==='inv'&&inv>0)));
  });
});
</script>
"""
    out.write_text(
        head.format(
            md_name=html.escape(md_path.name),
            pdf_name=html.escape(pdf_path.name),
            n=len(stats),
            avg_r=avg_r,
            avg_p=avg_p,
            avg_rv=avg_rv,
            avg_pv=avg_pv,
            good=good,
        )
        + "\n".join(sections)
        + tail,
        encoding="utf-8",
    )
    return {"doc": pdf_path.name, "md": md_path.name,
            "pages": len(stats), "avg_recall": avg_r, "avg_prec": avg_p,
            "vlm_avg_recall": avg_rv, "vlm_avg_prec": avg_pv,
            "fully_matched": good, "per_page": stats}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--md", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", default=None)
    ap.add_argument("-o", "--out", default="СВЕРКА_с_исходником.html")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    res = build(Path(args.md), Path(args.pdf), args.pages, Path(args.out))
    print(
        f"wrote {args.out}: страниц {res['pages']}\n"
        f"  итоговый документ: полнота {res['avg_recall']}%, "
        f"точность чисел {res['avg_prec']}%, "
        f"полностью сошлись {res['fully_matched']}\n"
        f"  только вывод модели: полнота {res['vlm_avg_recall']}%, "
        f"точность чисел {res['vlm_avg_prec']}%"
    )
    if args.json:
        Path(args.json).write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
