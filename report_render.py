"""Статический рендер HTML-отчёта: весь контент пишется прямо в разметку.

Почему так, а не как раньше. Прошлая версия рисовала всё в браузере из JSON:
на телефоне это давало пустую страницу — предпросмотр .html в «Файлах» iOS,
во вложениях почты и в мессенджерах выполняется без JavaScript, а старые
движки Android спотыкались на современном синтаксисе (``||=``, ``?.``).

Здесь HTML собирается питоном целиком: таблицы, SVG-графики, тексты. JS остаётся
только надстройкой (вкладки, сортировка, фильтры, сравнение произвольной пары).
Если он не выполнится — на странице по-прежнему видно всё, просто одним полотном.

Файл пишется с BOM (``utf-8-sig``): это снимает вопрос угадывания кодировки
в вьюерах, которые игнорируют ``<meta charset>``.
"""

from __future__ import annotations

import json
import math
from html import escape as _esc
from typing import Any, Callable, Iterable, Sequence

Row = dict[str, Any]

# три акцентных цвета серий — те же переменные, что и в палитре
S = ["var(--s1)", "var(--s2)", "var(--s3)"]


# ── форматирование ──────────────────────────────────────────────────────


def esc(v: Any) -> str:
    return _esc("" if v is None else str(v), quote=True)


def num(x: float) -> str:
    """Короткая запись числа для SVG-атрибутов."""
    r = round(float(x), 2)
    return f"{r:.2f}".rstrip("0").rstrip(".") or "0"


def fmt(v: Any, d: int = 1) -> str:
    if v is None:
        return "н/д"
    return f"{float(v):.{d}f}"


def pct_txt(v: Any) -> str:
    if v is None:
        return '<span class="mut">н/д</span>'
    cls = "ok" if v >= 90 else "warnc" if v >= 70 else "badc"
    return f'<span class="{cls}">{float(v):.1f}%</span>'


def secs(s: Any) -> str:
    if s is None:
        return "н/д"
    s = float(s)
    if s >= 60:
        return f"{int(s // 60)}м {round(s % 60)}с"
    return f"{s:.1f}с"


def money(c: Any) -> str:
    if c is None:
        return '<span class="mut">н/д</span>'
    return "$" + f"{float(c):.4f}"


def _get(d: Any, *path: str) -> Any:
    """Безопасный обход вложенных словарей (аналог ``a?.b?.c``)."""
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


# ── таблица ─────────────────────────────────────────────────────────────


class Col:
    __slots__ = ("k", "t", "num", "f")

    def __init__(
        self,
        k: str,
        t: str,
        num: bool = False,
        f: Callable[[Any, Row], str] | None = None,
    ) -> None:
        self.k, self.t, self.num, self.f = k, t, num, f


def _sort_val(v: Any) -> tuple[int, float, str]:
    """Ключ сортировки: None всегда в конец, числа отдельно от строк."""
    if v is None:
        return (1, 0.0, "")
    if isinstance(v, bool):
        return (0, float(v), "")
    if isinstance(v, (int, float)):
        return (0, float(v), "")
    return (0, 0.0, str(v))


def table(rows: Sequence[Row], cols: Sequence[Col], sort_key: str | None = None) -> str:
    """Готовая HTML-таблица. ``data-s`` на ячейках нужен JS-сортировке."""
    sk = sort_key or cols[0].k
    ordered = sorted(rows, key=lambda r: _sort_val(r.get(sk)), reverse=True)
    # None-значения после reverse оказываются впереди — возвращаем их в хвост
    ordered = [r for r in ordered if r.get(sk) is not None] + [
        r for r in ordered if r.get(sk) is None
    ]

    head = "".join(
        f'<th class="{"num" if c.num else ""}" data-k="{esc(c.k)}" scope="col">'
        f'{esc(c.t)}{" ▾" if c.k == sk else ""}</th>'
        for c in cols
    )
    body = []
    for r in ordered:
        tds = []
        for c in cols:
            v = r.get(c.k)
            html = c.f(v, r) if c.f else esc(v if v is not None else "")
            sv = "" if v is None else (str(v) if not isinstance(v, bool) else str(int(v)))
            tds.append(
                f'<td class="{"num" if c.num else ""}" data-s="{esc(sv)}">{html}</td>'
            )
        body.append("<tr>" + "".join(tds) + "</tr>")
    return (
        '<div class="scroll"><table data-sortable="1"><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


# ── SVG-примитивы ───────────────────────────────────────────────────────


def _svg(w: float, h: float, inner: str, desc: str = "") -> str:
    return (
        f'<div class="chartbox"><svg viewBox="0 0 {num(w)} {num(h)}" class="chartsvg" '
        f'role="img" preserveAspectRatio="xMidYMid meet">'
        + (f"<title>{esc(desc)}</title>" if desc else "")
        + inner
        + "</svg></div>"
    )


def _text(x: float, y: float, s: str, **kw: Any) -> str:
    attrs = " ".join(f'{k.replace("_", "-")}="{esc(v)}"' for k, v in kw.items())
    return f'<text x="{num(x)}" y="{num(y)}" {attrs}>{esc(s)}</text>'


def _line(x1: float, y1: float, x2: float, y2: float, stroke: str, w: float = 1) -> str:
    return (
        f'<line x1="{num(x1)}" y1="{num(y1)}" x2="{num(x2)}" y2="{num(y2)}" '
        f'stroke="{stroke}" stroke-width="{num(w)}"/>'
    )


def legend(items: Iterable[dict]) -> str:
    parts = []
    for i, it in enumerate(items):
        col = it.get("color") or S[i % 3]
        parts.append(f'<span><i style="background:{col}"></i>{esc(it["name"])}</span>')
    return '<div class="legend">' + "".join(parts) + "</div>"


# ── графики ─────────────────────────────────────────────────────────────


def grouped_bars(
    groups: Sequence[str], series: Sequence[dict], mx: float = 100, h: float = 280
) -> str:
    W, padL, padR, padT, padB = 760, 52, 14, 14, 52
    plot_w, plot_h = W - padL - padR, h - padT - padB
    out: list[str] = []

    def y(v: float) -> float:
        return padT + plot_h - (v / mx) * plot_h

    for i in range(5):
        v = mx * i / 4
        out.append(_line(padL, y(v), W - padR, y(v), "var(--grid)"))
        out.append(
            _text(
                padL - 8,
                y(v) + 4,
                f"{v:.0f}%",
                text_anchor="end",
                fill="var(--muted)",
                font_size=11,
            )
        )

    g_w = plot_w / max(1, len(groups))
    inner = g_w * 0.56
    bw = inner / max(1, len(series))
    for gi, g in enumerate(groups):
        gx = padL + gi * g_w + (g_w - inner) / 2
        for si, s in enumerate(series):
            v = s["values"][gi] if gi < len(s["values"]) else None
            if v is None:
                continue
            bx = gx + si * bw
            bh = max(2.0, (v / mx) * plot_h)
            out.append(
                f'<rect x="{num(bx + 1)}" y="{num(y(v))}" width="{num(max(1, bw - 2))}" '
                f'height="{num(bh)}" rx="4" fill="{S[si % 3]}">'
                f"<title>{esc(s['name'])} · {esc(g)}: {fmt(v)}</title></rect>"
            )
            if bw > 26:
                out.append(
                    _text(
                        bx + bw / 2,
                        y(v) - 6,
                        fmt(v, 0),
                        text_anchor="middle",
                        fill="var(--ink2)",
                        font_size=10.5,
                    )
                )
        cx = padL + gi * g_w + g_w / 2
        spans = "".join(
            f'<tspan x="{num(cx)}" dy="{13 if wi else 0}">{esc(word)}</tspan>'
            for wi, word in enumerate(str(g).split(" "))
        )
        out.append(
            f'<text x="{num(cx)}" y="{num(h - padB + 18)}" text-anchor="middle" '
            f'fill="var(--ink2)" font-size="11.5">{spans}</text>'
        )
    out.append(_line(padL, y(0), W - padR, y(0), "var(--axis)"))
    return _svg(W, h, "".join(out), "столбчатая диаграмма; те же числа есть в таблице ниже")


SEQ = [
    "var(--seq100)",
    "var(--seq250)",
    "var(--seq400)",
    "var(--seq550)",
    "var(--seq700)",
]


def heatmap(rows: Sequence[Row], cols: Sequence[Col]) -> str:
    cw = max(74.0, min(120.0, (760 - 90) / max(1, len(cols))))
    ch = 27
    padL, padT = 92, 42
    W = padL + len(cols) * cw + 14
    H = padT + len(rows) * ch + 16
    out: list[str] = []

    def color(v: Any) -> str:
        if v is None:
            return "var(--chip)"
        return SEQ[min(4, max(0, int(((v - 50) / 50) * 5)))]

    for ci, c in enumerate(cols):
        out.append(
            _text(
                padL + ci * cw + cw / 2,
                padT - 14,
                c.t,
                text_anchor="middle",
                fill="var(--muted)",
                font_size=11,
            )
        )
    for ri, r in enumerate(rows):
        out.append(
            _text(
                padL - 10,
                padT + ri * ch + 18,
                r["label"],
                text_anchor="end",
                fill="var(--ink2)",
                font_size=11.5,
            )
        )
        for ci, c in enumerate(cols):
            v = r.get(c.k)
            tip = f"стр. {r.get('page')} · {c.t}: {fmt(v)}%"
            if c.k == "prec" and r.get("halluc"):
                tip += " · лишние числа: " + ", ".join(str(x) for x in r["halluc"])
            out.append(
                f'<rect x="{num(padL + ci * cw + 1)}" y="{num(padT + ri * ch + 1)}" '
                f'width="{num(cw - 2)}" height="{ch - 2}" rx="4" fill="{color(v)}">'
                f"<title>{esc(tip)}</title></rect>"
            )
            dark = v is not None and v >= 80
            out.append(
                _text(
                    padL + ci * cw + cw / 2,
                    padT + ri * ch + 18,
                    "н/д" if v is None else f"{float(v):.0f}%",
                    text_anchor="middle",
                    fill="#fff" if dark else "var(--ink)",
                    font_size=11,
                )
            )
    return _svg(W, H, "".join(out), "теплокарта качества по страницам")


def scatter(pts: Sequence[dict], xlabel: str = "", h: float = 300) -> str:
    if not pts:
        return ""
    W, padL, padR, padT, padB = 760, 52, 90, 16, 46
    plot_w, plot_h = W - padL - padR, h - padT - padB
    xs = [p["x"] for p in pts]
    ys = [p["y"] for p in pts]

    def lx(v: float) -> float:
        return math.log10(max(v, 1e-6))

    x0 = math.floor(lx(min(xs)) * 2) / 2
    x1 = math.ceil(lx(max(xs)) * 2) / 2
    ymin = max(0.0, min(ys) - 10)
    ymax = min(100.0, max(ys) + 6)

    def X(v: float) -> float:
        return padL + ((lx(v) - x0) / ((x1 - x0) or 1)) * plot_w

    def Y(v: float) -> float:
        return padT + plot_h - ((v - ymin) / ((ymax - ymin) or 1)) * plot_h

    out: list[str] = []
    for i in range(5):
        v = ymin + (ymax - ymin) * i / 4
        out.append(_line(padL, Y(v), W - padR, Y(v), "var(--grid)"))
        out.append(
            _text(
                padL - 8,
                Y(v) + 4,
                f"{v:.0f}%",
                text_anchor="end",
                fill="var(--muted)",
                font_size=11,
            )
        )
    for e in range(math.ceil(x0), math.floor(x1) + 1):
        v = 10.0**e
        out.append(_line(X(v), padT, X(v), padT + plot_h, "var(--grid)"))
        out.append(
            _text(
                X(v),
                h - padB + 18,
                "$" + f"{v:.3f}",
                text_anchor="middle",
                fill="var(--muted)",
                font_size=11,
            )
        )
    out.append(
        _text(
            padL + plot_w / 2,
            h - 8,
            f"{xlabel} — шкала логарифмическая",
            text_anchor="middle",
            fill="var(--ink2)",
            font_size=11.5,
        )
    )

    placed = sorted(({"p": p, "x": X(p["x"]), "y": Y(p["y"]), "ly": Y(p["y"])} for p in pts),
                    key=lambda a: a["ly"])
    for i in range(1, len(placed)):
        if (
            placed[i]["ly"] - placed[i - 1]["ly"] < 13
            and abs(placed[i]["x"] - placed[i - 1]["x"]) < 110
        ):
            placed[i]["ly"] = placed[i - 1]["ly"] + 13
    for it in placed:
        p, x, y, ly = it["p"], it["x"], it["y"], it["ly"]
        tip = (
            f"{p['label']} · качество {fmt(p['y'])}% · {xlabel}: ${p['x']:.4f} · "
            + ("влезает на сервер 4×A16" if p["fits"] else "на сервер НЕ влезает")
            + (f" · {p['note']}" if p.get("note") else "")
        )
        g = [f"<title>{esc(tip)}</title>"]
        g.append(f'<circle cx="{num(x)}" cy="{num(y)}" r="12" fill="transparent"/>')
        g.append(
            f'<circle cx="{num(x)}" cy="{num(y)}" r="6" '
            f'fill="{"var(--s1)" if p["fits"] else "var(--surface)"}" '
            f'stroke="var(--s1)" stroke-width="2"/>'
        )
        if abs(ly - y) > 2:
            g.append(_line(x + 7, y, x + 11, ly - 4, "var(--axis)"))
        g.append(_text(x + 13, ly + 4, p["label"], fill="var(--ink2)", font_size=11.5))
        out.append("<g>" + "".join(g) + "</g>")
    out.append(_line(padL, Y(ymin), W - padR, Y(ymin), "var(--axis)"))
    return _svg(W, h, "".join(out), "качество против стоимости страницы")


def line_chart(
    series: Sequence[dict],
    labels: Sequence[str],
    ymin: float | None = None,
    ymax: float | None = None,
    h: float = 250,
) -> str:
    W, padL, padR, padT, padB = 760, 52, 16, 16, 54
    plot_w, plot_h = W - padL - padR, h - padT - padB
    allv = [v for s in series for v in s["values"] if v is not None]
    if not allv:
        return ""
    hi = ymax if ymax is not None else min(100.0, max(allv) * 1.1)
    lo = ymin if ymin is not None else max(0.0, min(allv) - 8)

    def X(i: int) -> float:
        if len(labels) < 2:
            return padL + plot_w / 2
        return padL + (i / (len(labels) - 1)) * plot_w

    def Y(v: float) -> float:
        return padT + plot_h - ((v - lo) / ((hi - lo) or 1)) * plot_h

    out: list[str] = []
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        out.append(_line(padL, Y(v), W - padR, Y(v), "var(--grid)"))
        out.append(
            _text(
                padL - 8,
                Y(v) + 4,
                f"{v:.0f}%",
                text_anchor="end",
                fill="var(--muted)",
                font_size=11,
            )
        )
    for i, lab in enumerate(labels):
        spans = "".join(
            f'<tspan x="{num(X(i))}" dy="{11 if pi else 0}">{esc(part)}</tspan>'
            for pi, part in enumerate(str(lab).split("\n"))
        )
        out.append(
            f'<text x="{num(X(i))}" y="{num(h - padB + 16)}" text-anchor="middle" '
            f'fill="var(--muted)" font-size="10.5">{spans}</text>'
        )
    for si, s in enumerate(series):
        pts = [(X(i), Y(v)) for i, v in enumerate(s["values"]) if v is not None]
        if len(pts) > 1:
            d = "M" + " L ".join(f"{num(a)} {num(b)}" for a, b in pts)
            out.append(
                f'<path d="{d}" fill="none" stroke="{S[si % 3]}" stroke-width="2" '
                f'stroke-linejoin="round" stroke-linecap="round"/>'
            )
        for i, v in enumerate(s["values"]):
            if v is None:
                continue
            lab = str(labels[i]).replace("\n", " ")
            out.append(
                "<g>"
                f"<title>{esc(s['name'])} · {esc(lab)}: {fmt(v)}%</title>"
                f'<circle cx="{num(X(i))}" cy="{num(Y(v))}" r="10" fill="transparent"/>'
                f'<circle cx="{num(X(i))}" cy="{num(Y(v))}" r="4.5" fill="{S[si % 3]}" '
                f'stroke="var(--surface)" stroke-width="2"/></g>'
            )
        last = [(i, v) for i, v in enumerate(s["values"]) if v is not None]
        if last:
            i, v = last[-1]
            out.append(
                _text(X(i) + 8, Y(v) + 4, s["name"], fill="var(--ink2)", font_size=11)
            )
    out.append(_line(padL, Y(lo), W - padR, Y(lo), "var(--axis)"))
    return _svg(W, h, "".join(out), "динамика точности по прогонам")


# ── карточки-обёртки ────────────────────────────────────────────────────


def card(*parts: str) -> str:
    return '<section class="card">' + "".join(parts) + "</section>"


def details(summary: str, body: str) -> str:
    """Таблица-двойник графика: свёрнута, но присутствует в разметке всегда."""
    return (
        f'<details class="tbl"><summary>{esc(summary)}</summary>'
        f'<div class="tblbody">{body}</div></details>'
    )


# ── панели ──────────────────────────────────────────────────────────────


def pane_ready(data: dict) -> str:
    R = data.get("readiness")
    if not R:
        return card(
            "<h2>Готовность продукта</h2>",
            '<p class="hint">Нет данных сверки. Соберите её командой '
            "<code>python build_match_viewer.py --md ИОС2_итог.md --pdf &lt;файл&gt; "
            "--json readiness.json</code>.</p>",
        )
    vlm_prec = R.get("vlm_avg_prec")
    vlm_cls = "warnc" if (vlm_prec or 0) >= 95 else "badc"
    kpis = (
        '<div class="kpis" style="margin-bottom:14px">'
        f'<div class="kpi"><div class="v">{esc(R.get("avg_recall"))}%</div>'
        '<div class="l">Полнота данных</div>'
        '<div class="d"><span class="mut">сколько данных документа попало в выгрузку</span></div></div>'
        f'<div class="kpi"><div class="v">{esc(R.get("avg_prec"))}%</div>'
        '<div class="l">Достоверность чисел</div>'
        '<div class="d"><span class="mut">доля чисел выгрузки, которые есть в документе</span></div></div>'
        f'<div class="kpi"><div class="v">{esc(R.get("fully_matched"))} из {esc(R.get("pages"))}</div>'
        '<div class="l">Страниц сошлись полностью</div>'
        '<div class="d"><span class="mut">полнота ≥90% и достоверность ≥95%</span></div></div>'
        "</div>"
    )
    layers = (
        '<div class="scroll"><table><thead><tr><th scope="col">Слой данных</th>'
        '<th scope="col">Источник</th><th class="num" scope="col">Достоверность</th>'
        '<th scope="col">Чем подтверждено</th></tr></thead><tbody>'
        "<tr><td><b>Текст листов</b></td><td>текстовый слой PDF</td>"
        '<td class="num ok">точно</td>'
        "<td>копируется из документа, а не читается моделью</td></tr>"
        "<tr><td><b>Таблицы</b></td><td>сетка PDF + координаты слов</td>"
        '<td class="num ok">точно</td>'
        "<td>на стр. 45 из слоя 190 чисел против 182 у модели, при 100% против 96.7% достоверности</td></tr>"
        "<tr><td><b>Листы со сломанным шрифтом</b></td><td>подстановка, выведенная по документу</td>"
        '<td class="num ok">99% символов</td>'
        "<td>7 нечитаемых листов стали точным текстом; проверка — доля слов, нашедшихся в словаре</td></tr>"
        "<tr><td><b>Шифр документа</b></td><td>штампы читаемых листов</td>"
        '<td class="num ok">точно</td>'
        "<td>один шифр на комплект, источник подписан в тексте</td></tr>"
        f"<tr><td><b>Описание листа</b></td><td>модель (VLM)</td>"
        f'<td class="num {vlm_cls}">{esc(vlm_prec)}%</td>'
        "<td>единственный слой, где возможна выдумка — здесь остаётся основной риск</td></tr>"
        "</tbody></table></div>"
    )
    left = [
        "Текст и таблицы текстовых листов выгружаются точно и без участия модели, "
        "значит не могут быть перевраны",
        "Листы со сломанной кодировкой перестали быть слепой зоной",
        "Есть бесплатная метрика, позволяющая мерить качество неограниченно и без затрат на API",
        "Стоимость и время упали: вызовов к модели в 7.3 раза меньше",
    ]
    right = [
        f"Описание листа моделью содержит {100 - float(vlm_prec or 0):.1f}% чисел, которых в документе нет",
        "Разделители в шифрах (дефисы, слэши) отсутствуют в слое PDF — из него их не восстановить",
        "Чертежи и планы не проверялись: у них нет пригодного текстового слоя, там работает только модель",
        "Выводы по моделям сделаны по одному прогону и требуют подтверждения",
    ]
    two = (
        '<div class="grid2">'
        '<div><h3 style="color:var(--good)">Готово</h3><ul class="log">'
        + "".join(f"<li><div>{esc(x)}</div></li>" for x in left)
        + '</ul></div><div><h3 style="color:var(--warn)">Требует работы</h3><ul class="log">'
        + "".join(f"<li><div>{esc(x)}</div></li>" for x in right)
        + "</ul></div></div>"
    )
    per_page = R.get("per_page") or []
    pp_tbl = ""
    if per_page:
        rows = [
            {
                "page": p.get("page"),
                "recall": p.get("recall"),
                "prec": p.get("prec"),
                "vlm_recall": p.get("vlm_recall"),
                "vlm_prec": p.get("vlm_prec"),
                "loss": p.get("loss"),
                "inv": p.get("inv"),
            }
            for p in per_page
        ]
        pp_tbl = card(
            "<h2>Сверка постранично</h2>",
            '<p class="hint">Полнота и достоверность каждой страницы итогового документа '
            "против текстового слоя исходника; отдельно — то же для вывода модели.</p>",
            table(
                rows,
                [
                    Col("page", "Страница", True),
                    Col("recall", "Полнота", True, lambda v, r: pct_txt(v)),
                    Col("prec", "Достоверность", True, lambda v, r: pct_txt(v)),
                    Col("vlm_recall", "Полнота VLM", True, lambda v, r: pct_txt(v)),
                    Col("vlm_prec", "Достоверность VLM", True, lambda v, r: pct_txt(v)),
                    Col("loss", "Потеряно слов", True),
                    Col("inv", "Лишних чисел", True),
                ],
                "page",
            ),
        )
    return (
        card(
            "<h2>Итог: насколько данные выгрузки совпадают с исходником</h2>",
            f'<p class="hint">Сверка постранично по документу «{esc(R.get("doc", ""))}»: '
            f'{esc(R.get("pages"))} страниц с текстом и таблицами. Эталон — текстовый слой PDF, '
            "для листов со сломанным шрифтом кодировка восстановлена.</p>",
            kpis,
            "<h3>Из чего складывается результат</h3>",
            layers,
            f'<p class="hint" style="margin-top:12px">Полнота «только вывода модели» — '
            f'{esc(R.get("vlm_avg_recall"))}%, и это не дефект: описание не обязано повторять '
            "весь текст листа, данные берутся из слоя. Смотреть в этом срезе нужно на "
            "достоверность чисел.</p>",
        )
        + card("<h2>Что готово, а что нет</h2>", two)
        + pp_tbl
    )


METRICS = [
    ("token_recall", "Слова"),
    ("num_recall", "Числа"),
    ("num_precision", "Точность чисел"),
    ("code_recall", "Коды"),
]


def pane_quality(runs: list[Row], with_q: list[Row]) -> str:
    out = []
    body = [
        "<h2>Точность моделей по метрикам</h2>",
        '<p class="hint">Эталон — текстовый слой самого документа: точный список слов, чисел и '
        "кодов, которые обязаны попасть в выгрузку. <b>Точность чисел</b> — доля чисел выгрузки, "
        "которые действительно есть в документе; это прямой детектор выдумок. Выше — лучше во "
        "всех четырёх.</p>",
    ]
    if with_q:
        sr = with_q[-3:]
        series = [
            {
                "name": (r["pdftext"].get("label") or r["model"]) + " · " + r["stamp"][9:13],
                "values": [r["pdftext"]["avg"].get(k) for k, _ in METRICS],
            }
            for r in sr
        ]
        body.append(legend(series))
        body.append(grouped_bars([t for _, t in METRICS], series))
        rows = [
            {
                "model": r["model"],
                "label": r["pdftext"].get("label") or "",
                "pages": r["pages"],
                "n": r["pdftext"].get("n"),
                "w": r["pdftext"]["avg"].get("token_recall"),
                "num": r["pdftext"]["avg"].get("num_recall"),
                "prec": r["pdftext"]["avg"].get("num_precision"),
                "code": r["pdftext"]["avg"].get("code_recall"),
            }
            for r in with_q
        ]
        body.append(
            details(
                "Показать таблицей",
                table(
                    rows,
                    [
                        Col("model", "Модель"),
                        Col("label", "Замер"),
                        Col("pages", "Страницы"),
                        Col("n", "Стр.", True),
                        Col("w", "Слова", True, lambda v, r: pct_txt(v)),
                        Col("num", "Числа", True, lambda v, r: pct_txt(v)),
                        Col("prec", "Точность чисел", True, lambda v, r: pct_txt(v)),
                        Col("code", "Коды", True, lambda v, r: pct_txt(v)),
                    ],
                    "prec",
                ),
            )
        )
    else:
        body.append('<div class="mut">Пока нет прогонов с этой метрикой.</div>')
    out.append(card(*body))

    if len(with_q) > 1:
        labels = [r["stamp"][9:11] + ":" + r["stamp"][11:13] + "\n" + r["model"][:12] for r in with_q]
        ser = [
            {"name": "Точность чисел", "values": [_get(r, "pdftext", "avg", "num_precision") for r in with_q]},
            {"name": "Числа", "values": [_get(r, "pdftext", "avg", "num_recall") for r in with_q]},
            {"name": "Слова", "values": [_get(r, "pdftext", "avg", "token_recall") for r in with_q]},
        ]
        out.append(
            card(
                "<h2>Как менялась точность за день</h2>",
                '<p class="hint">Каждая точка — прогон. Разрывы означают, что метрика на тех '
                "страницах неприменима (например, слова на листах с повреждённым текстовым слоем).</p>",
                legend(ser),
                line_chart(ser, labels, ymin=40, ymax=100),
            )
        )

    rows2 = [
        {
            "model": r["model"],
            "pages": r["pages"],
            "rec": r["etalon"]["recall"],
            "key": r["etalon"]["key"],
            "run": r["run"],
        }
        for r in runs
        if r.get("etalon")
    ]
    if rows2:
        out.append(
            card(
                "<h2>Историческая метрика — эталон 6 тяжёлых страниц</h2>",
                '<p class="hint">Другая шкала (грубое совпадение токенов с размеченным эталоном). '
                "С метрикой выше не смешивать.</p>",
                table(
                    rows2,
                    [
                        Col("model", "Модель"),
                        Col("pages", "Страницы"),
                        Col("rec", "Recall", True, lambda v, r: pct_txt(v)),
                        Col("key", "Key", True, lambda v, r: pct_txt(v)),
                        Col("run", "Прогон", False,
                            lambda v, r: f'<span class="mut" style="font-size:12px">{esc(v)}</span>'),
                    ],
                    "rec",
                ),
            )
        )
    return "".join(out)


def model_agg(runs: list[Row]) -> list[Row]:
    agg: dict[str, Row] = {}
    for r in runs:
        a = agg.setdefault(
            r["model"],
            {
                "model": r["model"],
                "runs": 0,
                "pages": 0,
                "tok": 0,
                "cost": 0.0,
                "time": 0.0,
                "fit": r.get("fit"),
                "best": None,
                "prec": None,
                "retries": 0,
                "failed": 0,
            },
        )
        a["runs"] += 1
        a["pages"] += r.get("n_pages") or 0
        a["tok"] += r.get("total") or 0
        a["cost"] += r.get("cost") or 0
        a["time"] += r.get("elapsed") or 0
        a["retries"] += r.get("retries") or 0
        a["failed"] += r.get("failed") or 0
        q = _get(r, "etalon", "recall")
        if q is None:
            q = _get(r, "pdftext", "avg", "token_recall")
        if q is not None and (a["best"] is None or q > a["best"]):
            a["best"] = q
        pr = _get(r, "pdftext", "avg", "num_precision")
        if pr is not None and (a["prec"] is None or pr > a["prec"]):
            a["prec"] = pr
    rows = []
    for a in agg.values():
        b = dict(a)
        b["cpp"] = a["cost"] / a["pages"] if a["pages"] else None
        b["spp"] = a["time"] / a["pages"] if a["pages"] else None
        rows.append(b)
    return rows


def pane_models(runs: list[Row]) -> str:
    rows = model_agg(runs)
    pts = [
        {
            "x": r["cpp"],
            "y": r["best"],
            "label": r["model"],
            "fits": r["fit"] is True,
            "note": f"{r['runs']} прогонов, {r['pages']} страниц",
        }
        for r in rows
        if r["best"] is not None and r["cpp"] is not None and r["cpp"] > 0
    ]
    first = [
        "<h2>Качество против стоимости</h2>",
        '<p class="hint">Ось X — оценка стоимости страницы, ось Y — лучшее качество модели. '
        "Заполненная точка означает, что модель помещается на сервер 4×A16 (~61 GB), полая — что "
        "нет. Выгодны точки левее и выше.</p>",
    ]
    if pts:
        first.append(
            '<div class="legend">'
            '<span><svg width="14" height="14" aria-hidden="true"><circle cx="7" cy="7" r="5" '
            'fill="var(--s1)"/></svg> влезает на сервер</span>'
            '<span><svg width="14" height="14" aria-hidden="true"><circle cx="7" cy="7" r="5" '
            'fill="var(--surface)" stroke="var(--s1)" stroke-width="2"/></svg> не влезает</span>'
            "</div>"
        )
        first.append(scatter(pts, xlabel="оценка стоимости, $ за страницу"))
    return card(*first) + card(
        "<h2>Сводка по моделям</h2>",
        '<p class="hint">Стоимость — оценка по ставке $/1M токенов, выведенной из замеров волны '
        "14.08; метод сверен на двух моделях с опубликованным прайсом, погрешность около 10%.</p>",
        table(
            rows,
            [
                Col("model", "Модель"),
                Col("fit", "Сервер", False,
                    lambda v, r: '<span class="ok">влезает</span>' if v is True
                    else '<span class="badc">нет</span>' if v is False
                    else '<span class="mut">?</span>'),
                Col("runs", "Прогонов", True),
                Col("pages", "Страниц", True),
                Col("best", "Качество", True, lambda v, r: pct_txt(v)),
                Col("prec", "Точность чисел", True, lambda v, r: pct_txt(v)),
                Col("spp", "Сек/стр", True, lambda v, r: fmt(v, 1)),
                Col("cpp", "$/стр", True, lambda v, r: money(v)),
                Col("cost", "Итого $", True, lambda v, r: money(v)),
                Col("retries", "Ретраи", True),
                Col("failed", "Сбои", True),
            ],
            "best",
        ),
    )


HEAT_COLS = [Col("w", "Слова"), Col("num", "Числа"), Col("prec", "Точность чисел"), Col("code", "Коды")]

SCALE_LEGEND = (
    '<div class="legend"><span class="mut">шкала:</span>'
    + "".join(
        f'<span><i style="background:{c}"></i>{l}%</span>'
        for c, l in zip(
            ["var(--chip)"] + SEQ,
            ["&lt;50", "50–60", "60–70", "70–80", "80–90", "90–100"],
        )
    )
    + '<span class="mut">~ — частичный эталон (только цифры)</span></div>'
)


def pane_pages(with_q: list[Row]) -> str:
    runs_wp = [r for r in with_q if r["pdftext"].get("pages")]
    head = [
        "<h2>Качество по каждой странице</h2>",
        '<p class="hint">Чем темнее клетка, тем выше показатель. «н/д» означает, что метрика '
        "к этой странице неприменима. Прогоны показаны все подряд; на компьютере переключатель "
        "оставляет один.</p>",
    ]
    if not runs_wp:
        head.append('<div class="mut">Нет подетальных данных.</div>')
        return card(*head)

    default = len(runs_wp) - 1
    opts = "".join(
        f'<option value="{i}"{" selected" if i == default else ""}>'
        f'{esc(r["stamp"][9:13])} · {esc(r["model"])} · {esc(r["pdftext"].get("label") or "")}</option>'
        for i, r in enumerate(runs_wp)
    )
    head.append(
        '<div class="ctl jsonly"><label>Прогон: <select id="hp">' + opts + "</select></label></div>"
    )

    blocks = []
    for i, r in enumerate(runs_wp):
        rows = [
            dict(x, label="стр. " + str(x["page"]) + (" ~" if x.get("partial") else ""))
            for x in r["pdftext"]["pages"]
        ]
        title = f'{r["stamp"][9:13]} · {r["model"]} · {r["pdftext"].get("label") or ""}'
        blocks.append(
            f'<div class="variant{" active" if i == default else ""}" data-i="{i}">'
            f'<h3 class="vtitle">{esc(title)}</h3>'
            + heatmap(rows, HEAT_COLS)
            + SCALE_LEGEND
            + details(
                "Показать таблицей",
                table(
                    rows,
                    [
                        Col("page", "Страница", True),
                        Col("w", "Слова", True, lambda v, r: pct_txt(v)),
                        Col("num", "Числа", True, lambda v, r: pct_txt(v)),
                        Col("prec", "Точность чисел", True, lambda v, r: pct_txt(v)),
                        Col("code", "Коды", True, lambda v, r: pct_txt(v)),
                        Col("halluc", "Лишние числа", False,
                            lambda v, r: f'<span class="warnc">{esc(", ".join(str(x) for x in v))}</span>'
                            if v else '<span class="mut">нет</span>'),
                    ],
                    "page",
                ),
            )
            + "</div>"
        )
    return card(*head, '<div id="heatvars">' + "".join(blocks) + "</div>")


def pane_time(runs: list[Row], models: list[str]) -> str:
    agg: dict[str, Row] = {}
    for r in runs:
        a = agg.setdefault(r["model"], {"model": r["model"], "time": 0.0, "pages": 0})
        a["time"] += r.get("elapsed") or 0
        a["pages"] += r.get("n_pages") or 0
    spp = [{"model": a["model"], "v": a["time"] / a["pages"]} for a in agg.values() if a["pages"]]
    first = [
        "<h2>Скорость обработки</h2>",
        '<p class="hint">Секунд на страницу — суммарно по всем прогонам модели. Меньше лучше.</p>',
    ]
    if spp:
        mx = max(s["v"] for s in spp)
        first.append(
            grouped_bars(
                [s["model"].replace("-", " ") for s in spp],
                [{"name": "сек/стр", "values": [s["v"] for s in spp]}],
                mx=math.ceil(mx * 1.1),
                h=260,
            )
        )

    total_t = sum(r.get("elapsed") or 0 for r in runs)
    total_p = sum(r.get("n_pages") or 0 for r in runs)
    rows = [
        {
            "stamp": r["stamp"],
            "model": r["model"],
            "pages": r["pages"],
            "n": r.get("n_pages"),
            "elapsed": r.get("elapsed"),
            "spp": r.get("sec_per_page"),
            "calls": r.get("calls"),
            "tok": r.get("total"),
            "cost": r.get("cost"),
        }
        for r in runs
    ]
    ctl = (
        '<div class="ctl jsonly"><label>Модель: <select id="fm"><option value="">все</option>'
        + "".join(f"<option>{esc(m)}</option>" for m in models)
        + "</select></label></div>"
    )
    summary = (
        f'<p class="hint">Всего {len(runs)} прогонов, {secs(total_t)}, {total_p} страниц, '
        f'в среднем {(total_t / total_p if total_p else 0):.1f} с/стр.</p>'
    )
    return card(*first) + card(
        "<h2>Журнал прогонов</h2>",
        summary,
        ctl,
        table(
            rows,
            [
                Col("stamp", "Когда (UTC)"),
                Col("model", "Модель"),
                Col("pages", "Страницы"),
                Col("elapsed", "Время", True, lambda v, r: secs(v)),
                Col("spp", "Сек/стр", True, lambda v, r: fmt(v, 1)),
                Col("calls", "Вызовов", True),
                Col("tok", "Токенов", True, lambda v, r: f"{(v or 0) / 1000:.1f}k"),
                Col("cost", "Оценка $", True, lambda v, r: money(v)),
            ],
            "stamp",
        ),
    )


CMP_M = [
    ("Слова", ("pdftext", "avg", "token_recall")),
    ("Числа", ("pdftext", "avg", "num_recall")),
    ("Точность чисел", ("pdftext", "avg", "num_precision")),
    ("Коды", ("pdftext", "avg", "code_recall")),
]
CMP_T = [("Сек/стр", ("sec_per_page",)), ("Токенов", ("total",)), ("Оценка $", ("cost",))]


def _cmp_row(name: str, x: Any, y: Any, inv: bool) -> str:
    d, cls = None, "mut"
    if x is not None and y is not None:
        d = y - x
        better = d < 0 if inv else d > 0
        cls = "mut" if abs(d) < 1e-9 else ("ok" if better else "badc")
    if name == "Оценка $":
        f = money
    elif name == "Токенов":
        f = lambda v: "" if v is None else esc(v)
    else:
        f = lambda v: fmt(v, 1)
    dv = "" if d is None else ("+" if d > 0 else "") + f"{d:.{0 if name == 'Токенов' else 2}f}"
    return (
        f"<tr><td>{esc(name)}</td><td class=\"num\">{f(x)}</td>"
        f'<td class="num">{f(y)}</td><td class="num {cls}">{dv}</td></tr>'
    )


def pane_compare(runs: list[Row]) -> str:
    if len(runs) < 2:
        return card("<h2>Сравнить два прогона</h2>", '<div class="mut">Мало прогонов.</div>')
    ia, ib = len(runs) - 2, len(runs) - 1
    a, b = runs[ia], runs[ib]

    def opts(sel: int) -> str:
        return "".join(
            f'<option value="{i}"{" selected" if i == sel else ""}>'
            f'{esc(r["stamp"])} · {esc(r["model"])} · стр {esc(r["pages"])}</option>'
            for i, r in enumerate(runs)
        )

    series = [
        {"name": "A · " + a["model"], "values": [_get(a, *p) for _, p in CMP_M]},
        {"name": "B · " + b["model"], "values": [_get(b, *p) for _, p in CMP_M]},
    ]
    chart = ""
    if any(v is not None for s in series for v in s["values"]):
        chart = legend(series) + grouped_bars([t for t, _ in CMP_M], series, h=250)

    body = "".join(_cmp_row(t, _get(a, *p), _get(b, *p), False) for t, p in CMP_M)
    body += "".join(_cmp_row(t, _get(a, *p), _get(b, *p), True) for t, p in CMP_T)
    tbl = (
        '<div class="scroll"><table><thead><tr><th scope="col">Показатель</th>'
        '<th class="num" scope="col">A</th><th class="num" scope="col">B</th>'
        '<th class="num" scope="col">Δ (B−A)</th></tr></thead><tbody>'
        + body
        + "</tbody></table></div>"
        f'<p class="hint">A = {esc(a["run"])}<br>B = {esc(b["run"])}</p>'
    )
    return card(
        "<h2>Сравнить два прогона</h2>",
        '<p class="hint">Зелёным отмечено улучшение, красным ухудшение. Для времени, токенов и '
        "стоимости лучше меньше, для метрик качества — больше. По умолчанию сравниваются два "
        "последних прогона; выбор другой пары работает там, где доступен JavaScript.</p>",
        '<div class="ctl jsonly">'
        f'<label>A: <select id="ca">{opts(ia)}</select></label>'
        f'<label>B: <select id="cb">{opts(ib)}</select></label></div>',
        f'<div id="cmpchart">{chart}</div><div id="cmpout">{tbl}</div>',
    )


def pane_log(data: dict) -> str:
    items = []
    for e in reversed(data.get("log") or []):
        eff = ""
        if e.get("effect"):
            cls = "badc" if e.get("good") is False else "ok"
            eff = f'<div class="e {cls}">{esc(e["effect"])}</div>'
        items.append(
            f'<li><div class="t">{esc(e.get("time") or "")}</div>'
            f'<div>{esc(e.get("text") or "")}</div>{eff}</li>'
        )
    return card(
        "<h2>Что менялось в алгоритме</h2>",
        '<p class="hint">Сверху — самое свежее.</p>',
        '<ul class="log">' + ("".join(items) or '<li class="mut">пусто</li>') + "</ul>",
    )


# ── сборка страницы ─────────────────────────────────────────────────────

TABS = [
    ("ready", "Готовность"),
    ("quality", "Качество"),
    ("models", "Модели"),
    ("pages", "По страницам"),
    ("time", "Время"),
    ("compare", "Сравнение"),
    ("log", "Журнал"),
]

CSS = """
/* ── палитра: валидированная (validate_palette.js, light+dark, --pairs all) ── */
:root{
  color-scheme:light;
  --plane:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
  --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --crit:#d03b3b;
  --seq100:#cde2fb; --seq250:#86b6ef; --seq400:#3987e5; --seq550:#1c5cab; --seq700:#0d366b;
  --chip:#f0efec;
}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
  color-scheme:dark;
  --plane:#0d0d0d; --surface:#1a1a19;
  --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
  --chip:#26262b;
}}
:root[data-theme=dark]{
  color-scheme:dark;
  --plane:#0d0d0d; --surface:#1a1a19;
  --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
  --chip:#26262b;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--plane);color:var(--ink);
 font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
 padding:26px 20px 70px;position:relative;overflow-x:hidden;
 overflow-wrap:break-word}

/* ── фоновая анимация: только на больших экранах с мышью. На телефонах три
      размытых слоя по 44vw съедают память GPU и роняют вкладку в белый лист ── */
#bg{display:none}
@media(min-width:901px) and (pointer:fine){
  #bg{display:block;position:fixed;inset:0;z-index:-1;overflow:hidden;pointer-events:none}
  #bg span{position:absolute;border-radius:50%;filter:blur(70px);opacity:.16}
  #bg span:nth-child(1){width:44vw;height:44vw;left:-8vw;top:-10vw;
   background:var(--s1);animation:drift1 44s ease-in-out infinite alternate}
  #bg span:nth-child(2){width:38vw;height:38vw;right:-6vw;top:14vh;
   background:var(--s3);animation:drift2 58s ease-in-out infinite alternate}
}
@keyframes drift1{to{transform:translate3d(14vw,10vh,0) scale(1.15)}}
@keyframes drift2{to{transform:translate3d(-12vw,16vh,0) scale(.88)}}
@media(prefers-reduced-motion:reduce){#bg span{animation:none}}

.wrap{max-width:1200px;margin:0 auto}
h1{font-size:25px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:16px;margin:0 0 3px} h3{font-size:14px;margin:18px 0 8px;color:var(--ink2)}
.sub{color:var(--ink2);font-size:13px;margin-bottom:20px}
.hint{color:var(--muted);font-size:12.5px;margin:0 0 14px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:10px}
.kpi{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:13px 15px}
.kpi .v{font-size:23px;font-weight:600;line-height:1.15}
.kpi .l{color:var(--muted);font-size:12px;margin-top:2px}
.kpi .d{font-size:12px;margin-top:3px}
.tabs{display:flex;flex-wrap:wrap;gap:8px;margin:22px 0 14px}
button{font:inherit;cursor:pointer}
.tab{display:inline-block;background:var(--surface);border:1px solid var(--ring);
 color:var(--ink);border-radius:999px;padding:7px 16px;font-size:14px;
 text-decoration:none;-webkit-tap-highlight-color:transparent}
.tab:hover{background:var(--chip)}
.tab[aria-selected=true]{background:var(--s1);border-color:var(--s1);color:#fff}
.card{display:block;background:var(--surface);border:1px solid var(--ring);
 border-radius:12px;padding:16px 18px;margin-bottom:14px}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:560px}
th,td{border-bottom:1px solid var(--grid);padding:7px 9px;text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;
 letter-spacing:.04em;user-select:none;position:sticky;top:0;background:var(--surface)}
html.js th[data-k]{cursor:pointer}
html.js th[data-k]:hover{color:var(--s1)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:hover td{background:var(--chip)}
.chip{display:inline-block;background:var(--chip);border-radius:6px;padding:1px 8px;font-size:12px}
.ok{color:var(--good);font-weight:600}.warnc{color:var(--warn)}.badc{color:var(--crit)}
.mut{color:var(--muted)}
.ctl{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
select{font:inherit;font-size:13px;background:var(--surface);color:var(--ink);
 border:1px solid var(--ring);border-radius:8px;padding:5px 10px;max-width:100%}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:2px 0 10px;font-size:12.5px;color:var(--ink2)}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;
 vertical-align:-1px}
.legend svg{display:inline-block;width:14px;height:14px;vertical-align:-2px}
.chartbox{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -2px}
svg.chartsvg{display:block;width:100%;min-width:560px;height:auto}
details.tbl{margin-top:10px}
details.tbl>summary{color:var(--s1);font-size:12.5px;cursor:pointer;
 list-style:none;display:inline-block;padding:2px 0}
details.tbl>summary::-webkit-details-marker{display:none}
details.tbl>summary:before{content:'▸ '}
details.tbl[open]>summary:before{content:'▾ '}
.tblbody{margin-top:8px}
ul.log{margin:0;padding-left:0;list-style:none}
ul.log li{margin-bottom:14px;padding-left:16px;border-left:2px solid var(--grid);position:relative}
ul.log li:before{content:'';position:absolute;left:-5px;top:6px;width:8px;height:8px;
 border-radius:50%;background:var(--s1);border:2px solid var(--surface)}
ul.log .t{font-size:12px;color:var(--muted)}
ul.log .e{font-size:13px;margin-top:3px}
code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12.5px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:860px){.grid2{grid-template-columns:1fr}}
.panetitle{font-size:19px;margin:26px 0 10px;padding-bottom:6px;
 border-bottom:1px solid var(--grid)}
.vtitle{margin:4px 0 10px}

/* Всё видно по умолчанию. Прячет лишнее только скрипт — и только после того,
   как он полностью отработал и поставил html.js. Упал скрипт или его вовсе
   не выполняют (предпросмотр вложения, «Файлы» на iPhone) — страница целая. */
html.js .pane{display:none}
html.js .pane.active{display:block}
html.js .panetitle{display:none}
html.js .variant{display:none}
html.js .variant.active{display:block}
html.js .vtitle{display:none}
.jsonly{display:none}
html.js .jsonly{display:flex}

@media(max-width:640px){
  body{padding:14px 11px 48px;font-size:14.5px}
  h1{font-size:20px} .sub{margin-bottom:14px}
  .card{padding:12px 12px;border-radius:10px;margin-bottom:10px}
  .kpis{grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:8px}
  .kpi{padding:10px 11px;border-radius:10px}
  .kpi .v{font-size:19px}
  .tabs{gap:6px;margin:16px 0 10px}
  .tab{padding:6px 12px;font-size:13px}
  .panetitle{font-size:17px;margin:20px 0 8px}
}
@media print{
  #bg{display:none}
  html.js .pane,html.js .variant{display:block!important}
  html.js .panetitle,html.js .vtitle{display:block!important}
  .tabs,.jsonly{display:none!important}
  details.tbl{display:none}
  .card{break-inside:avoid;border-color:#bbb}
}
"""

# Скрипт — надстройка. Синтаксис намеренно консервативный (ES5): его читают
# в том числе старые движки Android WebView, а падение парсера убило бы
# обработчики целиком. Контент от него не зависит.
JS = r"""
(function(){
  var d=document, root=d.documentElement;
  function $(s,r){return (r||d).querySelector(s);}
  function all(s,r){return Array.prototype.slice.call((r||d).querySelectorAll(s));}

  var DATA=null;
  try{ DATA=JSON.parse($('#reportdata').textContent); }catch(e){ DATA=null; }

  /* ── вкладки ── */
  var tabs=all('.tab'), panes=all('.pane');
  function show(id){
    for(var i=0;i<panes.length;i++){
      var on = panes[i].id === 'pane-'+id;
      if(on) panes[i].className='pane active'; else panes[i].className='pane';
    }
    for(var j=0;j<tabs.length;j++)
      tabs[j].setAttribute('aria-selected', tabs[j].getAttribute('data-id')===id?'true':'false');
  }
  for(var t=0;t<tabs.length;t++){
    (function(el){
      el.onclick=function(ev){ ev.preventDefault(); show(el.getAttribute('data-id'));
        /* на file:// часть браузеров бросает SecurityError — вкладки важнее адреса */
        try{ history.replaceState(null,'','#'+el.getAttribute('data-id')); }catch(e){}
      };
    })(tabs[t]);
  }

  /* ── сортировка таблиц по клику на заголовок ── */
  function val(td){
    var s=td.getAttribute('data-s');
    if(s===null||s==='') return null;
    var n=parseFloat(s);
    return (!isNaN(n) && /^-?[0-9.]+$/.test(s)) ? n : s;
  }
  all('table[data-sortable]').forEach(function(tb){
    var ths=all('th',tb), state={k:null,dir:-1};
    ths.forEach(function(th,ci){
      if(!th.getAttribute('data-k')) return;
      th.onclick=function(){
        var k=th.getAttribute('data-k');
        state.dir = (state.k===k) ? -state.dir : -1; state.k=k;
        var body=tb.tBodies[0], rows=all('tr',body);
        rows.sort(function(a,b){
          var x=val(a.cells[ci]), y=val(b.cells[ci]);
          if(x===null&&y===null) return 0;
          if(x===null) return 1;
          if(y===null) return -1;
          if(typeof x==='number'&&typeof y==='number') return (x-y)*state.dir;
          return String(x).localeCompare(String(y))*state.dir;
        });
        rows.forEach(function(r){body.appendChild(r);});
        ths.forEach(function(o){
          var base=o.textContent.replace(/[ ]?[▾▴]$/,'');
          o.textContent = (o===th) ? base+(state.dir<0?' ▾':' ▴') : base;
        });
      };
    });
  });

  /* ── переключатель прогона в теплокарте ── */
  var hp=$('#hp');
  if(hp){
    hp.onchange=function(){
      all('#heatvars .variant').forEach(function(v){
        v.className = (v.getAttribute('data-i')===hp.value) ? 'variant active' : 'variant';
      });
    };
  }

  /* ── фильтр журнала прогонов по модели ── */
  var fm=$('#fm');
  if(fm){
    var tbl=fm.parentNode.parentNode.parentNode.querySelector('table[data-sortable]');
    fm.onchange=function(){
      var want=fm.value;
      all('tbody tr',tbl).forEach(function(tr){
        tr.style.display = (!want || tr.cells[1].getAttribute('data-s')===want) ? '' : 'none';
      });
    };
  }

  /* ── сравнение произвольной пары прогонов ── */
  var ca=$('#ca'), cb=$('#cb');
  if(ca&&cb&&DATA&&DATA.runs){
    var R=DATA.runs;
    var M=[['Слова','token_recall'],['Числа','num_recall'],
           ['Точность чисел','num_precision'],['Коды','code_recall']];
    function q(r,k){ return (r.pdftext&&r.pdftext.avg&&r.pdftext.avg[k]!=null)?r.pdftext.avg[k]:null; }
    function f(name,v){
      if(v===null||v===undefined) return name==='Токенов'?'':'н/д';
      if(name==='Оценка $') return '$'+(+v).toFixed(4);
      if(name==='Токенов') return String(v);
      return (+v).toFixed(1);
    }
    function row(name,x,y,inv){
      var dd=null, cls='mut';
      if(x!==null&&y!==null){ dd=y-x;
        var better = inv ? dd<0 : dd>0;
        cls = Math.abs(dd)<1e-9 ? 'mut' : (better?'ok':'badc'); }
      var dv = dd===null?'':((dd>0?'+':'')+dd.toFixed(name==='Токенов'?0:2));
      return '<tr><td>'+name+'</td><td class="num">'+f(name,x)+'</td><td class="num">'
        +f(name,y)+'</td><td class="num '+cls+'">'+dv+'</td></tr>';
    }
    function render(){
      var a=R[+ca.value], b=R[+cb.value];
      if(!a||!b) return;
      var html='';
      for(var i=0;i<M.length;i++) html+=row(M[i][0],q(a,M[i][1]),q(b,M[i][1]),false);
      html+=row('Сек/стр', a.sec_per_page==null?null:a.sec_per_page,
                           b.sec_per_page==null?null:b.sec_per_page, true);
      html+=row('Токенов', a.total==null?null:a.total, b.total==null?null:b.total, true);
      html+=row('Оценка $', a.cost==null?null:a.cost, b.cost==null?null:b.cost, true);
      $('#cmpout').innerHTML='<div class="scroll"><table><thead><tr><th>Показатель</th>'
        +'<th class="num">A</th><th class="num">B</th><th class="num">Δ (B−A)</th></tr></thead>'
        +'<tbody>'+html+'</tbody></table></div>'
        +'<p class="hint">A = '+a.run+'<br>B = '+b.run+'</p>';
      var ch=$('#cmpchart'); if(ch) ch.style.display='none';
    }
    ca.onchange=render; cb.onchange=render;
  }

  /* Активируем «режим вкладок» последним: если что-то выше упало, страница
     остаётся развёрнутой целиком, а не пустой. */
  var start = (location.hash||'').replace('#','');
  var known=false;
  for(var k=0;k<tabs.length;k++) if(tabs[k].getAttribute('data-id')===start) known=true;
  root.className += ' js';
  show(known ? start : tabs[0].getAttribute('data-id'));
})();
"""


def _day(built: str) -> str:
    """«2026-08-15 11:34 UTC» → «15.08» для заголовка вкладки."""
    s = str(built)[:10]
    parts = s.split("-")
    return f"{parts[2]}.{parts[1]}" if len(parts) == 3 else s


def render(data: dict) -> str:
    runs: list[Row] = data.get("runs") or []
    with_q = [r for r in runs if _get(r, "pdftext", "avg")]
    models = sorted({r["model"] for r in runs})

    total_cost = sum(r.get("cost") or 0 for r in runs)
    total_tok = sum(r.get("total") or 0 for r in runs)
    total_time = sum(r.get("elapsed") or 0 for r in runs)
    total_pages = sum(r.get("n_pages") or 0 for r in runs)
    today = str(data.get("built", ""))[:10].replace("-", "")
    today_cost = sum(r.get("cost") or 0 for r in runs if str(r.get("stamp", ""))[:8] >= today)
    best_prec = max([_get(r, "pdftext", "avg", "num_precision") or 0 for r in with_q] + [0])

    kpis = [
        ("Прогонов", str(len(runs)), ""),
        ("Страниц обработано", str(total_pages), ""),
        ("Суммарное время", secs(total_time), ""),
        ("Токенов", f"{total_tok / 1000:.0f}k", ""),
        (
            "Израсходовано сегодня",
            f"${today_cost:.3f}",
            f'<span class="mut">из $2 — {today_cost / 2 * 100:.1f}%; '
            f"за всю историю ${total_cost:.2f}</span>",
        ),
        (
            "Точность чисел, лучшая",
            f"{best_prec:.1f}%" if best_prec else "н/д",
            '<span class="mut">доля чисел, что есть в документе</span>',
        ),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="v">{v}</div><div class="l">{esc(l)}</div>'
        + (f'<div class="d">{d}</div>' if d else "")
        + "</div>"
        for l, v, d in kpis
    )

    bodies = {
        "ready": pane_ready(data),
        "quality": pane_quality(runs, with_q),
        "models": pane_models(runs),
        "pages": pane_pages(with_q),
        "time": pane_time(runs, models),
        "compare": pane_compare(runs),
        "log": pane_log(data),
    }
    tabs_html = "".join(
        f'<a class="tab" href="#pane-{tid}" data-id="{tid}" role="button" '
        f'aria-selected="{"true" if i == 0 else "false"}">{esc(label)}</a>'
        for i, (tid, label) in enumerate(TABS)
    )
    panes_html = "".join(
        f'<h2 class="panetitle" id="pane-{tid}-title">{esc(label)}</h2>'
        f'<div class="pane" id="pane-{tid}">{bodies[tid]}</div>'
        for tid, label in TABS
    )

    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Отчёт ПТО — {esc(_day(data.get("built", "")))}</title>
<style>{CSS}</style>
</head>
<body>
<div id="bg"><span></span><span></span></div>
<div class="wrap">
<h1>Оптимизация выгрузки PDF → Markdown</h1>
<div class="sub">Собрано {esc(data.get("built", ""))} · прогонов {len(runs)} · моделей {len(models)}
 · <span class="chip">бюджет HF API: до $2</span></div>
<div class="kpis">{kpi_html}</div>
<nav class="tabs">{tabs_html}</nav>
{panes_html}
</div>
<script id="reportdata" type="application/json">{payload}</script>
<script>{JS}</script>
</body>
</html>
"""
