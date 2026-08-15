"""Сборка HTML-отчёта по прогонам VLM (время / модели / качество / сравнение).

Источники (всё локальное, без сети):
  hf_runs/<run>/meta.json          — модель, страницы, время, токены, флаги
  hf_runs/<run>/out.compare.json   — метрика против ЭТАЛОНА (rough recall/key)
  hf_runs/<run>/pdftext.json       — метрика против ТЕКСТОВОГО СЛОЯ PDF
  report_log.json                  — журнал изменений алгоритма (ведётся руками)

Пересобирается сколько угодно раз: python build_report_0815.py
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "hf_runs"

# $/1M токенов (суммарно in+out, блендед). Выведено из замеренных стоимостей
# волны 14.08: rate = стоимость прогона / total_tokens этого прогона.
# Метод проверен на моделях с опубликованным прайсом: qwen3vl-235b даёт 0.56
# против 0.59 расчётных, kimi-k3 — 9.59 против 9.58. Погрешность ±10%.
RATES: dict[str, float | None] = {
    "kimi-k3": 9.59,
    "qwen36-27b": 1.90,
    "glm-45v": 1.39,
    "glm-46v-flash": 0.69,
    "qwen3vl-235b": 0.56,
    "qwen36-35b-a3b": 0.50,
    "gemma4-26b-a4b": 0.20,
    "qwen3vl-32b": 0.10,  # featherless: грубая оценка из «49 стр ≈ $0.05–0.15»
    "qwen3vl-30b-a3b": 0.30,
    "qwen3vl-8b": 0.05,
}

# Влезает ли на потенциальный сервер 4×A16 (~61 GB VRAM, TP=4)
SERVER_FIT = {
    "qwen3vl-32b": True,
    "qwen36-35b-a3b": True,
    "qwen36-27b": True,
    "qwen3vl-30b-a3b": True,
    "qwen3vl-8b": True,
    "gemma4-26b-a4b": True,
    "qwen3vl-235b": False,
    "glm-45v": False,
    "kimi-k3": False,
    "glm-46v-flash": True,
}


def collect_runs(since: str | None) -> list[dict]:
    out: list[dict] = []
    for d in sorted(RUNS.glob("*")):
        mp = d / "meta.json"
        if not mp.exists():
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        stamp = m.get("started_utc", d.name[:15])
        if since and stamp < since:
            continue
        u = m.get("usage", {}) or {}
        model = (m.get("spec") or {}).get("id", "?")
        tot = u.get("total_tokens", 0)
        rate = RATES.get(model)
        rec: dict = {
            "run": d.name,
            "stamp": stamp,
            "model": model,
            "pages": str(m.get("pages", "")),
            "elapsed": round(m.get("elapsed_sec", 0) or 0, 1),
            "in": u.get("prompt_tokens", 0),
            "out": u.get("completion_tokens", 0),
            "total": tot,
            "calls": u.get("calls", 0),
            "retries": u.get("retries", 0),
            "failed": u.get("failed_tiles", 0),
            "cost": round(tot / 1e6 * rate, 4) if rate else None,
            "two_pass": m.get("two_pass"),
            "sheet_aware": m.get("sheet_aware"),
            "table_pages": m.get("table_pages") or m.get("tag") or "",
            "fit": SERVER_FIT.get(model),
        }
        n_pages = max(1, len(_expand_pages(rec["pages"])))
        rec["n_pages"] = n_pages
        rec["sec_per_page"] = round(rec["elapsed"] / n_pages, 1)
        rec["cost_per_page"] = (
            round(rec["cost"] / n_pages, 5) if rec["cost"] is not None else None
        )

        cmp_p = d / "out.compare.json"
        if cmp_p.exists():
            try:
                rows = json.loads(cmp_p.read_text(encoding="utf-8"))
                if rows:
                    rec["etalon"] = {
                        "recall": round(
                            sum(r["token_recall"] for r in rows) / len(rows), 1
                        ),
                        "key": round(
                            sum(r["key_phrase_hit"] for r in rows) / len(rows), 1
                        ),
                        "pages": [r["page"] for r in rows],
                    }
            except Exception:
                pass

        pt = d / "pdftext.json"
        if pt.exists():
            try:
                j = json.loads(pt.read_text(encoding="utf-8"))
                rec["pdftext"] = {
                    "avg": j.get("avg", {}),
                    "n": len(j.get("pages", [])),
                    "label": j.get("label"),
                    # подетально — для теплокарты «страница × метрика»
                    "pages": [
                        {
                            "page": r["page"],
                            "w": r["token_recall"],
                            "num": r["num_recall"],
                            "prec": r["num_precision"],
                            "code": r["code_recall"],
                            "partial": r.get("partial", False),
                            "halluc": r.get("hallucinated_nums", [])[:8],
                        }
                        for r in j.get("pages", [])
                    ],
                }
            except Exception:
                pass
        out.append(rec)
    return out


def _expand_pages(spec: str) -> list[int]:
    res: list[int] = []
    for ch in str(spec).split(","):
        ch = ch.strip()
        if not ch:
            continue
        try:
            if "-" in ch:
                a, b = ch.split("-", 1)
                res.extend(range(int(a), int(b) + 1))
            else:
                res.append(int(ch))
        except ValueError:
            continue
    return res


HTML = """<title>Отчёт ПТО — 15.08</title>
<style>
:root{--bg:#f7f7f8;--fg:#1c1c1e;--mut:#6b6b70;--card:#fff;--line:#e3e3e6;
 --acc:#2f6fd0;--ok:#1f8a4c;--warn:#b8860b;--bad:#c0392b;--chip:#eef2f8}
:root:not([data-theme=light]){}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
 --bg:#17171a;--fg:#ececf0;--mut:#9c9ca4;--card:#212126;--line:#33333a;
 --acc:#6ea8fe;--ok:#4ec27f;--warn:#e0b243;--bad:#f0736a;--chip:#2a2a32}}
:root[data-theme=dark]{--bg:#17171a;--fg:#ececf0;--mut:#9c9ca4;--card:#212126;
 --line:#33333a;--acc:#6ea8fe;--ok:#4ec27f;--warn:#e0b243;--bad:#f0736a;--chip:#2a2a32}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,
 BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:24px 20px 60px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:24px;margin:0 0 4px} h2{font-size:17px;margin:26px 0 10px}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi .v{font-size:22px;font-weight:600} .kpi .l{color:var(--mut);font-size:12px}
.tabs{display:flex;flex-wrap:wrap;gap:8px;margin:22px 0 14px}
button{font:inherit;cursor:pointer}
.tab{background:var(--card);border:1px solid var(--line);color:var(--fg);
 border-radius:999px;padding:7px 15px;font-size:14px}
.tab[aria-selected=true]{background:var(--acc);border-color:var(--acc);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:14px 16px;margin-bottom:14px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:640px}
th,td{border-bottom:1px solid var(--line);padding:7px 9px;text-align:left;white-space:nowrap}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;
 letter-spacing:.03em;cursor:pointer;user-select:none;position:sticky;top:0;background:var(--card)}
th:hover{color:var(--acc)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:hover td{background:var(--chip)}
.chip{display:inline-block;background:var(--chip);border-radius:6px;padding:1px 7px;font-size:12px}
.ok{color:var(--ok);font-weight:600} .warn{color:var(--warn)} .bad{color:var(--bad)}
.mut{color:var(--mut)}
.ctl{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
select,input{font:inherit;font-size:13px;background:var(--card);color:var(--fg);
 border:1px solid var(--line);border-radius:7px;padding:5px 9px}
.bar{height:7px;background:var(--chip);border-radius:4px;overflow:hidden;min-width:70px;display:inline-block;vertical-align:middle}
.bar>i{display:block;height:100%;background:var(--acc)}
.hide{display:none}
.note{font-size:12.5px;color:var(--mut);margin-top:8px}
.diff{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:700px){.diff{grid-template-columns:1fr}}
ul.log{margin:0;padding-left:18px} ul.log li{margin-bottom:7px}
code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12.5px}
</style>
<div class="wrap">
<h1>Отчёт по оптимизации выгрузки PDF → Markdown</h1>
<div class="sub" id="sub"></div>
<div class="kpis" id="kpis"></div>
<div class="tabs" id="tabs"></div>
<div id="panes"></div>
</div>
<script>
const DATA = __DATA__;
const $ = (s,r)=> (r||document).querySelector(s);
const fmt = (v,d=1)=> v===null||v===undefined ? '<span class=mut>н/д</span>' : (+v).toFixed(d);
const pct = v => v===null||v===undefined ? '<span class=mut>н/д</span>'
  : `<span class="${v>=70?'ok':v>=45?'warn':'bad'}">${(+v).toFixed(1)}%</span>`;
const bar = v => v===null||v===undefined ? '' :
  `<span class="bar" style="width:70px"><i style="width:${Math.max(0,Math.min(100,v))}%"></i></span>`;
const secs = s => s>=60 ? `${Math.floor(s/60)}м ${Math.round(s%60)}с` : `${(+s).toFixed(1)}с`;
const money = c => c===null||c===undefined ? '<span class=mut>н/д</span>' : '$'+(+c).toFixed(4);

function table(rows, cols, sortKey){
  let sk = sortKey || cols[0].k, sdir = -1;
  const el = document.createElement('div'); el.className='scroll';
  const t = document.createElement('table'); el.appendChild(t);
  function draw(){
    const sorted = [...rows].sort((a,b)=>{
      const x=a[sk], y=b[sk];
      if(x===y) return 0;
      if(x===null||x===undefined) return 1;
      if(y===null||y===undefined) return -1;
      return (typeof x==='number'&&typeof y==='number') ? (x-y)*sdir
        : String(x).localeCompare(String(y))*sdir;
    });
    t.innerHTML = '<thead><tr>'+cols.map(c=>
      `<th class="${c.num?'num':''}" data-k="${c.k}">${c.t}${sk===c.k?(sdir<0?' ▾':' ▴'):''}</th>`
      ).join('')+'</tr></thead><tbody>'+
      sorted.map(r=>'<tr>'+cols.map(c=>
        `<td class="${c.num?'num':''}">${c.f?c.f(r[c.k],r):(r[c.k]??'')}</td>`).join('')+'</tr>').join('')+
      '</tbody>';
    t.querySelectorAll('th').forEach(th=>th.onclick=()=>{
      const k=th.dataset.k; if(k===sk) sdir=-sdir; else {sk=k; sdir=-1;} draw();
    });
  }
  draw(); return el;
}

const RUNS = DATA.runs;
const models = [...new Set(RUNS.map(r=>r.model))].sort();

// ── KPI ────────────────────────────────────────────────────────────────
const totalCost = RUNS.reduce((s,r)=>s+(r.cost||0),0);
const totalTok  = RUNS.reduce((s,r)=>s+(r.total||0),0);
const totalTime = RUNS.reduce((s,r)=>s+(r.elapsed||0),0);
const totalPages= RUNS.reduce((s,r)=>s+(r.n_pages||0),0);
$('#sub').innerHTML = `Собрано ${DATA.built} · прогонов ${RUNS.length} · моделей ${models.length}`
  + ` · <span class=chip>бюджет HF API: до $2</span>`;
$('#kpis').innerHTML = [
  ['Прогонов', RUNS.length], ['Страниц обработано', totalPages],
  ['Суммарное время', secs(totalTime)], ['Токенов', (totalTok/1000).toFixed(1)+'k'],
  ['Оценка расхода', '$'+totalCost.toFixed(3)],
  ['Моделей', models.length],
].map(([l,v])=>`<div class=kpi><div class=v>${v}</div><div class=l>${l}</div></div>`).join('');

// ── вкладки ────────────────────────────────────────────────────────────
const TABS = [
  ['quality','Качество'], ['models','Модели'], ['time','Время'],
  ['compare','Сравнение'], ['log','Журнал изменений'],
];
const tabsEl=$('#tabs'), panes=$('#panes');
TABS.forEach(([id,label],i)=>{
  const b=document.createElement('button'); b.className='tab'; b.textContent=label;
  b.setAttribute('aria-selected', i===0); b.onclick=()=>{
    tabsEl.querySelectorAll('.tab').forEach(x=>x.setAttribute('aria-selected',false));
    b.setAttribute('aria-selected',true);
    panes.querySelectorAll('.pane').forEach(p=>p.classList.add('hide'));
    $('#pane-'+id).classList.remove('hide');
  };
  tabsEl.appendChild(b);
  const d=document.createElement('div'); d.className='pane'+(i?' hide':'');
  d.id='pane-'+id; panes.appendChild(d);
});

// ── Качество ───────────────────────────────────────────────────────────
{
  const p=$('#pane-quality');
  p.innerHTML='<div class=card><h2 style="margin-top:0">Метрика 1 — текстовый слой PDF (бесплатная, детерминированная)</h2>'
   +'<div class=note>Эталон = сам текстовый слой документа: точный список слов, чисел и кодов, которые обязаны попасть в выгрузку. '
   +'Секции, собранные из слоя, при замере вырезаются — иначе метрика мерит сама себя. '
   +'<b>Точность чисел</b> = доля чисел выгрузки, которые есть в документе (детектор галлюцинаций).</div></div>';
  const rows = RUNS.filter(r=>r.pdftext).map(r=>({
    run:r.run, model:r.model, pages:r.pages, n:r.pdftext.n,
    w:r.pdftext.avg.token_recall, num:r.pdftext.avg.num_recall,
    prec:r.pdftext.avg.num_precision, code:r.pdftext.avg.code_recall}));
  const c1=document.createElement('div'); c1.className='card';
  if(rows.length) c1.appendChild(table(rows,[
    {k:'model',t:'Модель'},{k:'pages',t:'Страницы'},{k:'n',t:'Стр.',num:1},
    {k:'w',t:'Слова',num:1,f:v=>pct(v)+' '+bar(v)},
    {k:'num',t:'Числа',num:1,f:v=>pct(v)+' '+bar(v)},
    {k:'prec',t:'Точность чисел',num:1,f:v=>pct(v)},
    {k:'code',t:'Коды',num:1,f:v=>pct(v)},
    {k:'run',t:'Прогон',f:v=>`<span class=mut style="font-size:12px">${v}</span>`},
  ],'w'));
  else c1.innerHTML='<div class=mut>Пока нет прогонов с этой метрикой.</div>';
  p.appendChild(c1);

  p.insertAdjacentHTML('beforeend','<div class=card><h2 style="margin-top:0">Метрика 2 — эталон 6 тяжёлых страниц (rough)</h2>'
   +'<div class=note>Историческая грубая метрика против размеченных эталонов. Другая шкала, с метрикой 1 не смешивать.</div></div>');
  const rows2 = RUNS.filter(r=>r.etalon).map(r=>({
    run:r.run, model:r.model, pages:r.pages,
    rec:r.etalon.recall, key:r.etalon.key}));
  const c2=document.createElement('div'); c2.className='card';
  if(rows2.length) c2.appendChild(table(rows2,[
    {k:'model',t:'Модель'},{k:'pages',t:'Страницы'},
    {k:'rec',t:'Recall',num:1,f:v=>pct(v)+' '+bar(v)},
    {k:'key',t:'Key',num:1,f:v=>pct(v)+' '+bar(v)},
    {k:'run',t:'Прогон',f:v=>`<span class=mut style="font-size:12px">${v}</span>`},
  ],'rec'));
  p.appendChild(c2);
}

// ── Модели ─────────────────────────────────────────────────────────────
{
  const p=$('#pane-models');
  const agg={};
  RUNS.forEach(r=>{
    const a = agg[r.model] ||= {model:r.model, runs:0, pages:0, tok:0, cost:0,
      time:0, fit:r.fit, best:null, retries:0, failed:0};
    a.runs++; a.pages+=r.n_pages||0; a.tok+=r.total||0; a.cost+=r.cost||0;
    a.time+=r.elapsed||0; a.retries+=r.retries||0; a.failed+=r.failed||0;
    const q = r.etalon?.recall ?? r.pdftext?.avg?.token_recall ?? null;
    if(q!==null && (a.best===null || q>a.best)) a.best=q;
  });
  const rows=Object.values(agg).map(a=>({...a,
    cpp: a.pages? a.cost/a.pages : null, spp: a.pages? a.time/a.pages : null}));
  const c=document.createElement('div'); c.className='card';
  c.innerHTML='<h2 style="margin-top:0">Сводка по моделям</h2>'
   +'<div class=note>«Влезает» = помещается на потенциальный сервер 4×A16 (~61 GB VRAM, TP=4). '
   +'Стоимость — оценка по ставке $/1M токенов, выведенной из замеров волны 14.08 (±10%).</div>';
  c.appendChild(table(rows,[
    {k:'model',t:'Модель'},
    {k:'fit',t:'Влезает на сервер',f:v=> v===true?'<span class=ok>да</span>':v===false?'<span class=bad>нет</span>':'<span class=mut>?</span>'},
    {k:'runs',t:'Прогонов',num:1},{k:'pages',t:'Страниц',num:1},
    {k:'best',t:'Лучшее качество',num:1,f:v=>pct(v)},
    {k:'spp',t:'Сек/стр',num:1,f:v=>fmt(v,1)},
    {k:'cpp',t:'$/стр',num:1,f:v=>money(v)},
    {k:'cost',t:'Итого $',num:1,f:v=>money(v)},
    {k:'retries',t:'Ретраи',num:1},{k:'failed',t:'Сбои',num:1},
  ],'best'));
  p.appendChild(c);
}

// ── Время ──────────────────────────────────────────────────────────────
{
  const p=$('#pane-time');
  const c=document.createElement('div'); c.className='card';
  c.innerHTML='<h2 style="margin-top:0">Время прогонов</h2><div class=ctl>'
    +'<label>Модель: <select id=fm><option value="">все</option>'
    + models.map(m=>`<option>${m}</option>`).join('')+'</select></label>'
    +'<span class=mut id=tsum></span></div>';
  const host=document.createElement('div'); c.appendChild(host); p.appendChild(c);
  function render(){
    const f=$('#fm').value;
    const rows=RUNS.filter(r=>!f||r.model===f).map(r=>({
      stamp:r.stamp, model:r.model, pages:r.pages, n:r.n_pages,
      elapsed:r.elapsed, spp:r.sec_per_page, calls:r.calls, tok:r.total, cost:r.cost}));
    host.innerHTML=''; host.appendChild(table(rows,[
      {k:'stamp',t:'Когда (UTC)'},{k:'model',t:'Модель'},{k:'pages',t:'Страницы'},
      {k:'elapsed',t:'Время',num:1,f:v=>secs(v)},
      {k:'spp',t:'Сек/стр',num:1,f:v=>fmt(v,1)},
      {k:'calls',t:'Вызовов',num:1},
      {k:'tok',t:'Токенов',num:1,f:v=>(v/1000).toFixed(1)+'k'},
      {k:'cost',t:'Оценка $',num:1,f:v=>money(v)},
    ],'stamp'));
    const tt=rows.reduce((s,r)=>s+r.elapsed,0), tp=rows.reduce((s,r)=>s+r.n,0);
    $('#tsum').textContent=`— ${rows.length} прогонов, ${secs(tt)}, ${tp} страниц, среднее ${tp?(tt/tp).toFixed(1):0} с/стр`;
  }
  render(); $('#fm').onchange=render;
}

// ── Сравнение ──────────────────────────────────────────────────────────
{
  const p=$('#pane-compare');
  const opts=RUNS.map((r,i)=>`<option value="${i}">${r.stamp} · ${r.model} · стр ${r.pages}</option>`).join('');
  p.innerHTML=`<div class=card><h2 style="margin-top:0">Сравнить два прогона</h2>
   <div class=ctl><label>A: <select id=ca>${opts}</select></label>
   <label>B: <select id=cb>${opts}</select></label></div>
   <div id=cmpout></div></div>`;
  function metric(r){
    return {
      'Слова (текст-слой)': r.pdftext?.avg?.token_recall,
      'Числа (текст-слой)': r.pdftext?.avg?.num_recall,
      'Точность чисел': r.pdftext?.avg?.num_precision,
      'Коды (текст-слой)': r.pdftext?.avg?.code_recall,
      'Recall (эталон)': r.etalon?.recall,
      'Key (эталон)': r.etalon?.key,
      'Сек/стр': r.sec_per_page,
      'Токенов': r.total,
      'Оценка $': r.cost,
    };
  }
  function render(){
    const a=RUNS[+$('#ca').value], b=RUNS[+$('#cb').value];
    if(!a||!b) return;
    const ma=metric(a), mb=metric(b);
    const inv={'Сек/стр':1,'Токенов':1,'Оценка $':1};  // меньше = лучше
    const rows=Object.keys(ma).map(k=>{
      const x=ma[k], y=mb[k];
      let d=null, cls='mut';
      if(x!==null&&x!==undefined&&y!==null&&y!==undefined){
        d=y-x; const better = inv[k] ? d<0 : d>0;
        cls = Math.abs(d)<1e-9 ? 'mut' : (better?'ok':'bad');
      }
      const f = k==='Оценка $' ? (v=>money(v)) : k==='Токенов' ? (v=>v??'') : (v=>fmt(v,1));
      return `<tr><td>${k}</td><td class=num>${f(x)}</td><td class=num>${f(y)}</td>
        <td class="num ${cls}">${d===null?'':(d>0?'+':'')+(+d).toFixed(k==='Токенов'?0:3)}</td></tr>`;
    }).join('');
    $('#cmpout').innerHTML=`<div class=scroll><table>
      <thead><tr><th>Метрика</th><th class=num>A: ${a.model}</th>
      <th class=num>B: ${b.model}</th><th class=num>Δ (B−A)</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      <div class=note>A = ${a.run}<br>B = ${b.run}</div>`;
  }
  $('#ca').selectedIndex=Math.max(0,RUNS.length-2);
  $('#cb').selectedIndex=Math.max(0,RUNS.length-1);
  $('#ca').onchange=render; $('#cb').onchange=render; render();
}

// ── Журнал ─────────────────────────────────────────────────────────────
{
  const p=$('#pane-log');
  const items=(DATA.log||[]).map(e=>`<li><b>${e.time||''}</b> — ${e.text}
    ${e.effect?`<br><span class="${e.good===false?'bad':'ok'}">${e.effect}</span>`:''}</li>`).join('');
  p.innerHTML=`<div class=card><h2 style="margin-top:0">Что менялось в алгоритме</h2>
    <ul class=log>${items||'<li class=mut>пусто</li>'}</ul></div>`;
}
</script>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=str(ROOT / "ОТЧЁТ_2026-08-15.html"))
    ap.add_argument("--since", default="20260815", help="UTC-префикс, от какого прогона")
    ap.add_argument("--all", action="store_true", help="включить все прогоны")
    args = ap.parse_args()

    runs = collect_runs(None if args.all else args.since)
    log_p = ROOT / "report_log.json"
    log = json.loads(log_p.read_text(encoding="utf-8")) if log_p.exists() else []
    # итог сверки итогового документа с исходником (build_match_viewer.py --json)
    ready_p = ROOT / "readiness.json"
    readiness = None
    if ready_p.exists():
        try:
            readiness = json.loads(ready_p.read_text(encoding="utf-8"))
        except Exception:
            readiness = None

    data = {
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "runs": runs,
        "log": log,
        "readiness": readiness,
    }
    tpl_p = ROOT / "report_template.html"
    tpl = tpl_p.read_text(encoding="utf-8") if tpl_p.exists() else HTML
    # Данные живут внутри <script type="application/json">, поэтому любая
    # последовательность «</script>» в тексте закрыла бы блок раньше времени.
    # < — валидный JSON-эскейп для «<», так что просто убираем сам символ.
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    html = tpl.replace("__DATA__", payload)
    Path(args.out).write_text(html, encoding="utf-8")
    print(f"wrote {args.out} ({len(runs)} прогонов, {len(log)} записей журнала)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
