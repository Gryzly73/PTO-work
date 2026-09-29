"""Static, image-rich HTML reports for reviewed DWG symbol sheets."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Iterable

from .artifacts import load_items
from .circle_key import circle_heading
from .named_block import NAMED_BLOCK_EVIDENCE, field_legend_type_label
from .dimension_read import dimension_from_mapping
from .axis_read import axis_from_mapping
from .gost_welds import WELD_GOST_REASON, weld_entry


_EVIDENCE_DESCRIPTIONS = {
    "exact_block_definition": (
        "Определение DWG-блока точно совпадает с образцом в легенде."
    ),
    "normalized_geometry_style": (
        "Геометрический стиль совпадает с образцом легенды после нормализации "
        "поворота и длины."
    ),
    "layer_label_context": (
        "Название слоя согласуется с подписью легенды и используется как "
        "дополнительное контекстное доказательство."
    ),
    "project_exact_block_definition": (
        "Тот же уникальный блок, что в каталоге легенды другого листа комплекта. "
        "На поле листа стык confirmed; локальная легенда не нужна."
    ),
    "named_block_legend": (
        "Имя блока на поле однозначно совпадает с одной строкой легенды этого листа. "
        "Номер образца или скважины с клетки легенды на поле не переносится."
    ),
}

_REASON_DESCRIPTIONS = {
    "NO_EXACT_LEGEND_BLOCK_MATCH_H4": (
        "Не найдено точного соответствия определения блока образцу легенды."
    ),
    "GEOLOGY_NO_LEGEND_JOIN": (
        "Геознак колонки: в легенде листа нет стыка с блоком. "
        "Аббревиатура не расшифровывается."
    ),
    "NO_GEOMETRY_PROFILE_MATCH_H4": (
        "Не найдено однозначного соответствия геометрическому профилю легенды."
    ),
}

_FURNITURE_REASON_DESCRIPTIONS = {
    "FORMAT_STAMP_ATTRIBUTES": (
        "Слой FORMAT и атрибуты основной надписи (лист, стадия, формат, должности)."
    ),
    "STAMP_ATTRIBUTES": (
        "Два и более атрибута основной надписи при любом слое, включая слой 0."
    ),
    "PAPER_FRAME_COVERAGE": (
        "Вставка в координатах бумаги закрывает большую часть листа — рамка оформления."
    ),
    "SIGNATURE_BLOCK": (
        "Блок «подпись» на слое «Подписи» — служебная подпись листа."
    ),
    "COLUMN_BLOCK_LAYER": (
        "Блок «Колонна» на слое «колонна» — конструктивный объект, не условный знак."
    ),
    "TRAP_BLOCK_LAYER": (
        "Блок «трап…» на слое «Технология» — оборудование, не условный знак легенды."
    ),
    "FURNITURE_BLOCK_LAYER": (
        "Именованный блок мебели или сантехники на своём слое — изделие, "
        "не условный знак легенды."
    ),
    "ANONYMOUS_CONSTRUCTION_LAYER": (
        "Безымянный блок на слое фахверка / стоек / металла / фундамента — "
        "повторяющаяся конструкция, не условный знак."
    ),
    "ANONYMOUS_DIMENSION_LAYER": (
        "Безымянный блок *U / A$ на слое 0_dim — служебный размер, не условный знак."
    ),
    "SERVICE_LAYER": (
        "Вставка на служебном слое размеров, отметок, надписей или LinkedData."
    ),
    "AXIS_LAYER": (
        "Вставка на слое осей — буквенно-цифровая марка оси, не условный знак легенды."
    ),
    "AXIS_ATTRIBUTE": (
        "Атрибут «Ось» / «Ось'» — марка оси, читается по схеме осей, не по легенде."
    ),
    "ROOM_NUMBER_BLOCK": (
        "Блок «номерация …» на слое 0 — номер помещения или модуля по экспликации."
    ),
    "HEIGHT_MARK_BLOCK": (
        "Блок «высоты …» на слое 0 — отметка высоты, не условный знак легенды."
    ),
    "BUILDING_LABEL_BLOCK": (
        "Блок «НОВЫЙ АБК …» на слое 0 — подпись корпуса или модуля."
    ),
    "WELD_GOST_SIGNATURE": (
        "Сигнатура блока совпала со строкой таблицы ГОСТ 2.312 — сварной знак оформления."
    ),
}

_ROLE_BADGES = {
    "sheet_furniture": ("оформление", "furniture"),
    "drawing_object": ("объект", "object"),
    "drawing_annotation": ("аннотация", "annotation"),
    "specification_mark": ("марка", "mark"),
}

_CSS = """
:root { color-scheme: light; --ink:#17202a; --muted:#667085; --line:#d0d5dd;
  --surface:#f7f8fa; --confirmed:#067647; --probable:#b54708; --unknown:#b42318;
  --furniture:#667085; --object:#6941c6; --annotation:#026aa2; --mark:#c11574;
  --textlabel:#135e96; --geometry:#0d9488; --link:#175cd3; }
* { box-sizing:border-box; }
body { margin:0; font:14px/1.5 Inter,Segoe UI,Arial,sans-serif; color:var(--ink);
  background:white; }
main { max-width:1280px; margin:0 auto; padding:28px; }
h1 { margin:0 0 8px; font-size:26px; }
h2 { margin:34px 0 12px; font-size:20px; }
h3 { margin:0 0 6px; font-size:16px; }
a { color:var(--link); }
.eyebrow { color:var(--muted); font-size:12px; font-weight:700; text-transform:uppercase;
  letter-spacing:.05em; }
.scope { margin-top:18px; padding:16px; background:var(--surface);
  border:1px solid var(--line); border-radius:8px; }
.scope dl { display:grid; grid-template-columns:170px 1fr; gap:6px 14px; margin:0; }
.scope dt { color:var(--muted); }
.scope dd { margin:0; overflow-wrap:anywhere; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr)); gap:10px;
  margin:18px 0; }
.stat { border-top:3px solid var(--line); padding:10px 0; }
.stat strong { display:block; font-size:24px; }
.confirmed { border-top-color:var(--confirmed); }
.probable { border-top-color:var(--probable); }
.unknown { border-top-color:var(--unknown); }
.furniture { border-top-color:var(--furniture); }
.object { border-top-color:var(--object); }
.annotation { border-top-color:var(--annotation); }
.mark { border-top-color:var(--mark); }
.textlabel { border-top-color:var(--textlabel); }
.geometry { border-top-color:var(--geometry); }
.toolbar { display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }
.notice { padding:12px 14px; border-left:4px solid var(--probable);
  background:#fffaeb; }
.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(330px,1fr));
  gap:14px; }
.card { border:1px solid var(--line); border-radius:8px; overflow:hidden;
  break-inside:avoid; }
.card-image { height:180px; display:flex; align-items:center; justify-content:center;
  background:var(--surface); border-bottom:1px solid var(--line); }
.card-image img { width:100%; height:100%; object-fit:contain; }
.missing { color:var(--muted); padding:18px; text-align:center; }
.card-body { padding:14px; }
.meta { display:grid; grid-template-columns:110px 1fr; gap:4px 10px; margin:10px 0 0; }
.meta dt { color:var(--muted); }
.meta dd { margin:0; overflow-wrap:anywhere; }
.grafa { margin:0 0 10px; padding:10px; background:var(--surface); border-radius:6px; }
.grafa .meta { margin:0; }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; color:white;
  font-size:11px; font-weight:700; text-transform:uppercase; }
.badge.confirmed { background:var(--confirmed); }
.badge.probable { background:var(--probable); }
.badge.unknown { background:var(--unknown); }
.badge.furniture { background:var(--furniture); }
.badge.object { background:var(--object); }
.badge.annotation { background:var(--annotation); }
.badge.mark { background:var(--mark); }
.badge.textlabel { background:var(--textlabel); }
.badge.geometry { background:var(--geometry); }
.description { margin:8px 0; }
.technical { color:var(--muted); font-size:12px; overflow-wrap:anywhere; }
details { margin-top:10px; }
details > summary { cursor:pointer; color:var(--link); }
.positions { max-height:240px; overflow:auto; }
.positions table, .inventory { width:100%; border-collapse:collapse; }
th, td { padding:7px 8px; border-bottom:1px solid var(--line); text-align:left; }
th { color:var(--muted); font-size:12px; }
.inventory-wrap { overflow:auto; border:1px solid var(--line); border-radius:8px; }
.empty { color:var(--muted); padding:18px; border:1px dashed var(--line);
  border-radius:8px; }
.anomalies { color:var(--muted); font-size:12px; overflow-wrap:anywhere; }
footer { margin-top:36px; padding-top:14px; border-top:1px solid var(--line);
  color:var(--muted); font-size:12px; }
@media (max-width:800px) { main{padding:16px}.stats{grid-template-columns:repeat(2,1fr)}
  .scope dl{grid-template-columns:1fr}.scope dt{font-weight:700}.cards{grid-template-columns:1fr} }
@media print { .toolbar, details.scope-list { display:none; } main{max-width:none;padding:0}
  .card-image{height:130px} }
"""


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _number(value: object) -> str:
    number = float(value)
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _image(path: str | None, alt: str, report_dir: Path) -> str:
    if not path or not (report_dir / path).is_file():
        return (
            '<div class="card-image"><div class="missing">'
            "Изображение отсутствует: векторный образец не извлечён."
            "</div></div>"
        )
    safe_path = _escape(path.replace("\\", "/"))
    return (
        f'<a class="card-image" href="{safe_path}">'
        f'<img loading="lazy" src="{safe_path}" alt="{_escape(alt)}"></a>'
    )


def _position(instance: dict[str, Any]) -> str:
    point = instance["position"]
    return (
        f"X={_number(point['x'])}, Y={_number(point['y'])} "
        f"{_escape(point.get('units', ''))}"
    )


def _meta(rows: Iterable[tuple[str, object]]) -> str:
    return '<dl class="meta">' + "".join(
        f"<dt>{_escape(label)}</dt><dd>{_escape(value)}</dd>"
        for label, value in rows
    ) + "</dl>"


def _attributes(instance: dict[str, Any]) -> str:
    attributes = instance.get("attributes") or {}
    if not attributes:
        return ""
    rows = "".join(
        f"<tr><td>{_escape(key)}</td><td>{_escape(value)}</td></tr>"
        for key, value in sorted(attributes.items())
    )
    return (
        "<details><summary>Атрибуты DWG-блока</summary>"
        f'<div class="positions"><table>{rows}</table></div></details>'
    )


def _load_summary(report_dir: Path, page_dir: str) -> dict[str, Any]:
    path = report_dir / page_dir / "summary.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _title_block_grafa(title_block: dict[str, Any] | None) -> str:
    """Parsed ГОСТ 21.101 grafa. Empty cipher stays «не прочитан», not blank."""

    if not title_block:
        return ""
    code = str(title_block.get("code") or "").strip()
    rows = (
        ("Стадия", title_block.get("stage") or "—"),
        ("Лист", title_block.get("sheet") or "—"),
        ("Листов", title_block.get("sheetsTotal") or "—"),
        ("Шифр", code or "не прочитан"),
    )
    note = str(title_block.get("note") or "").strip()
    note_html = f'<p class="technical">{_escape(note)}</p>' if note else ""
    return f'<div class="grafa">{_meta(rows)}{note_html}</div>'


def _title_block_has_grafa(title_block: dict[str, Any] | None) -> bool:
    if not title_block:
        return False
    if any(
        str(title_block.get(key) or "").strip()
        for key in ("sheet", "sheetsTotal", "stage", "code", "title", "instanceId")
    ):
        return True
    return str(title_block.get("note") or "").strip() not in {"", "надпись не найдена"}


def _title_block_card(title_block: dict[str, Any]) -> str:
    """Exploded stamp: grafa without an INSERT furniture card."""

    return (
        '<article class="card"><div class="card-body">'
        '<span class="badge furniture">основная надпись</span>'
        "<h3>Основная надпись</h3>"
        f"{_title_block_grafa(title_block)}"
        "</div></article>"
    )


def _weld_grafa(instance: dict[str, Any] | None) -> str:
    """GOST 2.312 fields on a matched weld card."""

    if not instance or instance.get("classificationReason") != WELD_GOST_REASON:
        return ""
    entry = weld_entry(str(instance.get("signature") or ""))
    if entry is None or not entry.enabled:
        return ""
    rows = (
        ("ГОСТ 2.312", entry.gost_code or "—"),
        ("Тип шва", entry.label or "—"),
    )
    return f'<div class="grafa">{_meta(rows)}</div>'


def _legend_card(
    entry: dict[str, Any],
    matched: int,
    page_dir: str,
    report_dir: Path,
) -> str:
    crop = entry.get("cropPath")
    image_path = f"{page_dir}/{crop}" if crop else None
    label = entry.get("label") or "Текст легенды не прочитан"
    metadata = _meta(
        [
            ("ID", entry["id"]),
            ("Статус", entry["status"]),
            ("Confidence", entry["confidence"]),
            ("Связано значков", matched),
            ("Область", entry.get("symbolBbox") or "не определена"),
        ]
    )
    return (
        '<article class="card">'
        f"{_image(image_path, label, report_dir)}"
        '<div class="card-body">'
        f"<h3>{_escape(label)}</h3>"
        '<p class="description">Описание дословно извлечено из локальной легенды '
        "этого листа.</p>"
        f"{metadata}"
        "</div></article>"
    )


def _recognized_card(
    instance: dict[str, Any],
    binding: dict[str, Any],
    legend: dict[str, Any],
    crop_path: str | None,
    report_dir: Path,
) -> str:
    status = binding["status"]
    label = legend.get("label") or "Описание легенды не прочитано"
    evidence = binding.get("evidence") or []
    if any(item.get("kind") == NAMED_BLOCK_EVIDENCE for item in evidence):
        label = field_legend_type_label(label)
    heading = circle_heading(instance.get("blockName"), label)
    explanation = " ".join(
        _EVIDENCE_DESCRIPTIONS.get(item["kind"], item.get("detail", item["kind"]))
        for item in evidence
    )
    technical = " ".join(item.get("detail", "") for item in evidence)
    metadata = _meta(
        [
            ("ID", instance["id"]),
            ("Confidence", binding["confidence"]),
            ("Координаты", _position(instance)),
            ("Слой", instance["layer"]),
            ("DWG-блок", instance.get("blockName") or "—"),
            ("Источник", instance["sourceKind"]),
        ]
    )
    return (
        '<article class="card">'
        f"{_image(crop_path, heading, report_dir)}"
        '<div class="card-body">'
        f'<span class="badge {status}">{_escape(status)}</span>'
        f"<h3>{_escape(heading)}</h3>"
        f'<p class="description">{_escape(explanation)}</p>'
        f"{metadata}"
        f'<p class="technical">{_escape(technical)}</p>'
        f"{_attributes(instance)}"
        "</div></article>"
    )


def _positions_table(
    instance_ids: Iterable[str],
    instances: dict[str, dict[str, Any]],
) -> str:
    rows: list[str] = []
    for instance_id in instance_ids:
        instance = instances.get(instance_id)
        if not instance:
            continue
        rows.append(
            "<tr>"
            f"<td>{_escape(instance_id)}</td>"
            f"<td>{_escape(_position(instance))}</td>"
            f"<td>{_escape(instance['layer'])}</td>"
            f"<td>{_escape(instance.get('blockName') or '—')}</td>"
            "</tr>"
        )
    return (
        '<details><summary>Все экземпляры и координаты</summary>'
        '<div class="positions"><table><thead><tr><th>ID</th><th>Координаты</th>'
        f"<th>Слой</th><th>Блок</th></tr></thead><tbody>{''.join(rows)}"
        "</tbody></table></div></details>"
    )


def _dimension_grafa(instance: dict[str, Any] | None) -> str:
    """Size / elevation number above the annotation card. Raw attrs stay below."""

    if not instance:
        return ""
    entry = dimension_from_mapping(instance)
    if not entry:
        return ""
    value = str(entry.get("value") or "").strip()
    note = str(entry.get("note") or "").strip()
    if not value and not note:
        return ""
    kind = str(entry.get("kind") or "linear")
    labels = {"elevation": "Отметка", "linear": "Размер", "height": "Высота"}
    rows = ((labels.get(kind, "Число"), value or "—"),)
    note_html = f'<p class="technical">{_escape(note)}</p>' if note else ""
    return f'<div class="grafa">{_meta(rows)}{note_html}</div>'


def _axis_grafa(instance: dict[str, Any] | None) -> str:
    """Axis letter/digit above the mark card. Raw attrs stay below."""

    if not instance:
        return ""
    entry = axis_from_mapping(instance)
    schedule = instance.get("schedule") if isinstance(instance.get("schedule"), dict) else {}
    expansion = str((schedule or {}).get("label") or "").strip()
    if not entry:
        return ""
    letter = str(entry.get("letter") or "").strip()
    digit = str(entry.get("digit") or "").strip()
    note = str(entry.get("note") or "").strip()
    label = str(entry.get("label") or "").strip() or " / ".join(
        part for part in (letter, digit) if part
    )
    if not label and not note and not expansion:
        return ""
    rows = (("Ось", label or "—"),)
    if expansion:
        rows = (("Ось", label or "—"), ("Таблица", expansion))
    note_html = f'<p class="technical">{_escape(note)}</p>' if note else ""
    return f'<div class="grafa">{_meta(rows)}{note_html}</div>'


def _room_grafa(instance: dict[str, Any] | None) -> str:
    """Room number as a code. Expansion only when a kit table row joined."""

    if not instance:
        return ""
    reason = str(instance.get("classificationReason") or "")
    schedule = instance.get("schedule") if isinstance(instance.get("schedule"), dict) else {}
    kind = str((schedule or {}).get("kind") or "")
    if reason != "ROOM_NUMBER_BLOCK" and kind != "room":
        return ""
    code = str((schedule or {}).get("code") or "").strip()
    if not code:
        block = str(instance.get("blockName") or "")
        parts = block.split(None, 1)
        if _folded_html(parts[0] if parts else "") == "номерация" and len(parts) > 1:
            code = parts[1].strip()
    expansion = str((schedule or {}).get("label") or "").strip()
    note = str((schedule or {}).get("note") or "").strip()
    if not code and not expansion and not note:
        return ""
    rows = (("Помещение", code or "—"),)
    if expansion:
        rows = (("Помещение", code or "—"), ("Таблица", expansion))
    note_html = f'<p class="technical">{_escape(note)}</p>' if note else ""
    return f'<div class="grafa">{_meta(rows)}{note_html}</div>'


def _folded_html(text: str) -> str:
    return (text or "").strip().casefold().replace("ё", "е")


_TEXT_KIND_TITLES = {
    "axis": "Ось",
    "axis_letter": "Ось, буква",
    "axis_digit": "Ось, цифра",
    "linear": "Размер",
}


def _text_label_card(label: dict[str, Any]) -> str:
    """TEXT outside INSERT: axis grafa or linear size, no crop."""

    kind = str(label.get("kind") or "")
    title = str(label.get("text") or _TEXT_KIND_TITLES.get(kind, "Текст"))
    grafa_label = _TEXT_KIND_TITLES.get(kind, "Текст")
    note = str(label.get("note") or "").strip()
    note_html = f'<p class="technical">{_escape(note)}</p>' if note else ""
    grafa = f'<div class="grafa">{_meta(((grafa_label, title),))}{note_html}</div>'
    metadata = _meta(
        [
            ("ID", label.get("id") or "—"),
            ("Слой", label.get("layer") or "—"),
            ("Источник", label.get("source") or "—"),
            (
                "Координаты",
                f"{float(label.get('x') or 0):.1f}, {float(label.get('y') or 0):.1f} мм",
            ),
        ]
    )
    return (
        '<article class="card"><div class="card-body">'
        f"{grafa}"
        '<span class="badge textlabel">текст вне блока</span>'
        f"<h3>{_escape(title)}</h3>"
        '<p class="description">Подпись с листа, не INSERT. Блок не выдуман.</p>'
        f"{metadata}"
        "</div></article>"
    )


_GEOMETRY_KIND_TITLES = {
    "line": "Линия",
    "closed": "Контур",
    "hatch": "Штриховка",
}


def _field_geometry_card(item: dict[str, Any]) -> str:
    """Field LINE/POLYLINE: visible contour, label only with a same-sheet sample."""

    kind = str(item.get("kind") or "line")
    label = str(item.get("label") or "").strip()
    title = label or _GEOMETRY_KIND_TITLES.get(kind, "Линия")
    grafa_label = "Подпись" if label else _GEOMETRY_KIND_TITLES.get(kind, "Линия")
    grafa_value = label or "нет образца этого листа"
    note = str(item.get("note") or "").strip()
    note_html = f'<p class="technical">{_escape(note)}</p>' if note else ""
    grafa = f'<div class="grafa">{_meta(((grafa_label, grafa_value),))}{note_html}</div>'
    bbox = item.get("bbox") or [0, 0, 0, 0]
    metadata = _meta(
        [
            ("ID", item.get("id") or "—"),
            ("Слой", item.get("layer") or "—"),
            ("Источник", item.get("source") or "—"),
            (
                "Окно",
                f"{float(bbox[0]):.1f}, {float(bbox[1]):.1f} — "
                f"{float(bbox[2]):.1f}, {float(bbox[3]):.1f} мм",
            ),
        ]
    )
    return (
        '<article class="card"><div class="card-body">'
        f"{grafa}"
        '<span class="badge geometry">линия / штриховка</span>'
        f"<h3>{_escape(title)}</h3>"
        '<p class="description">Геометрия листа, не INSERT. '
        "Подпись только если совпал образец этого листа.</p>"
        f"{metadata}"
        "</div></article>"
    )


def _classified_card(
    instance: dict[str, Any],
    crop_path: str | None,
    report_dir: Path,
    title_block: dict[str, Any] | None = None,
) -> str:
    role = instance.get("role") or "sheet_furniture"
    badge_text, badge_class = _ROLE_BADGES.get(role, ("служебный", "furniture"))
    reason_code = instance.get("classificationReason") or ""
    reason = _FURNITURE_REASON_DESCRIPTIONS.get(
        reason_code,
        reason_code or "Не-легендный INSERT, вынесенный из unknown.",
    )
    title = instance.get("blockName") or instance.get("layer") or badge_text
    grafa = (
        _title_block_grafa(title_block)
        if title_block
        else (
            _weld_grafa(instance)
            or _dimension_grafa(instance)
            or _axis_grafa(instance)
            or _room_grafa(instance)
        )
    )
    metadata = _meta(
        [
            ("ID", instance["id"]),
            ("Слой", instance["layer"]),
            ("DWG-блок", instance.get("blockName") or "—"),
            ("Причина", reason_code or "—"),
            ("Координаты", _position(instance)),
            ("Источник", instance.get("sourceKind") or "—"),
        ]
    )
    return (
        '<article class="card">'
        f"{_image(crop_path, title, report_dir)}"
        '<div class="card-body">'
        f"{grafa}"
        f'<span class="badge {badge_class}">{_escape(badge_text)}</span>'
        f"<h3>{_escape(title)}</h3>"
        f'<p class="description">{_escape(reason)}</p>'
        f"{metadata}"
        f"{_attributes(instance)}"
        "</div></article>"
    )


def _unknown_card(
    cluster: dict[str, Any],
    instances: dict[str, dict[str, Any]],
    crop_path: str | None,
    report_dir: Path,
) -> str:
    representative = instances[cluster["representativeInstanceId"]]
    reason_code = str(cluster.get("reason") or "")
    reason = _REASON_DESCRIPTIONS.get(reason_code, reason_code)
    if reason_code == "GEOLOGY_NO_LEGEND_JOIN":
        description = (
            "Геознак колонки скважины. Стыка с легендой колонки на этом листе нет. "
            "Подпись из аббревиатуры блока не ставим."
        )
    else:
        description = (
            "Семантическое значение не определено. Без подтверждения человеком, "
            "локальной легендой или внешним нормативным каталогом название символу "
            "не присваивается."
        )
    metadata = _meta(
        [
            ("Экземпляров", cluster["occurrences"]),
            ("Representative", representative["id"]),
            ("Координаты", _position(representative)),
            ("Слой", representative["layer"]),
            ("DWG-блок", representative.get("blockName") or "—"),
            ("Signature", cluster["signature"]),
        ]
    )
    return (
        '<article class="card">'
        f"{_image(crop_path, 'Нераспознанный кандидат', report_dir)}"
        '<div class="card-body">'
        '<span class="badge unknown">unrecognized</span>'
        f"<h3>Кластер {_escape(cluster['id'])}</h3>"
        f'<p class="description">{_escape(description)}</p>'
        f'<p class="technical">{_escape(reason)}</p>'
        f"{metadata}"
        f"{_positions_table(cluster['instanceIds'], instances)}"
        f"{_attributes(representative)}"
        "</div></article>"
    )


def _scope_list(fixtures: list[dict[str, Any]]) -> str:
    items = "".join(
        f"<li><strong>{_escape(item['id'])}</strong> — "
        f"{_escape(item['documentPath'])}, страница {_escape(item['page'])}</li>"
        for item in fixtures
    )
    return (
        '<details class="scope-list"><summary>Полный состав анализа: '
        f"{len(fixtures)} листов</summary><ol>{items}</ol></details>"
    )


def _document_report(
    fixture: dict[str, Any],
    fixtures: list[dict[str, Any]],
    review: dict[str, Any],
    report_dir: Path,
) -> str:
    page = int(review["page"])
    page_dir = f"dwg_symbols/page_{page:04d}"
    legends = load_items(report_dir / page_dir / "legend_entries.json")
    instance_items = load_items(report_dir / page_dir / "symbol_instances.json")
    bindings = load_items(report_dir / page_dir / "symbol_bindings.json")
    unknowns = load_items(report_dir / page_dir / "unrecognized_symbols.json")
    labels_path = report_dir / page_dir / "text_labels.json"
    text_labels = load_items(labels_path) if labels_path.is_file() else []
    geometry_path = report_dir / page_dir / "field_geometry.json"
    field_geometry = load_items(geometry_path) if geometry_path.is_file() else []
    title_block = _load_summary(report_dir, page_dir).get("titleBlock")
    instances = {item["id"]: item for item in instance_items}
    legend_by_id = {item["id"]: item for item in legends}
    binding_by_instance = {item["instanceId"]: item for item in bindings}
    crop_by_instance = {
        item["instanceId"]: item["path"] for item in review.get("cropIndex", [])
    }
    matched_counts: dict[str, int] = {}
    for binding in bindings:
        legend_id = binding["legendEntryId"]
        matched_counts[legend_id] = matched_counts.get(legend_id, 0) + 1

    confirmed: list[str] = []
    probable: list[str] = []
    for instance_id, binding in binding_by_instance.items():
        instance = instances[instance_id]
        legend = legend_by_id[binding["legendEntryId"]]
        card = _recognized_card(
            instance,
            binding,
            legend,
            crop_by_instance.get(instance_id),
            report_dir,
        )
        (confirmed if binding["status"] == "confirmed" else probable).append(card)
    unknown_cards = [
        _unknown_card(
            cluster,
            instances,
            crop_by_instance.get(cluster["representativeInstanceId"]),
            report_dir,
        )
        for cluster in unknowns
        if cluster["representativeInstanceId"] in instances
    ]
    stamp_id = (title_block or {}).get("instanceId")
    furniture_cards = [
        _classified_card(
            item,
            crop_by_instance.get(item["id"]),
            report_dir,
            title_block=title_block if item["id"] == stamp_id else None,
        )
        for item in instance_items
        if item.get("role") == "sheet_furniture"
    ]
    stamp_on_card = any(
        item.get("role") == "sheet_furniture" and item["id"] == stamp_id
        for item in instance_items
    )
    if title_block and not stamp_on_card and _title_block_has_grafa(title_block):
        furniture_cards.insert(0, _title_block_card(title_block))
    object_cards = [
        _classified_card(item, crop_by_instance.get(item["id"]), report_dir)
        for item in instance_items
        if item.get("role") == "drawing_object"
    ]
    annotation_cards = [
        _classified_card(item, crop_by_instance.get(item["id"]), report_dir)
        for item in instance_items
        if item.get("role") == "drawing_annotation"
    ]
    mark_cards = [
        _classified_card(item, crop_by_instance.get(item["id"]), report_dir)
        for item in instance_items
        if item.get("role") == "specification_mark"
    ]
    text_cards = [_text_label_card(item) for item in text_labels]
    geometry_cards = [_field_geometry_card(item) for item in field_geometry]
    legend_cards = [
        _legend_card(entry, matched_counts.get(entry["id"], 0), page_dir, report_dir)
        for entry in legends
    ]

    def section(title: str, cards: list[str], empty: str) -> str:
        body = f'<div class="cards">{"".join(cards)}</div>' if cards else (
            f'<div class="empty">{_escape(empty)}</div>'
        )
        return f"<h2>{_escape(title)} <small>({len(cards)})</small></h2>{body}"

    filename = Path(fixture["documentPath"]).name
    reason = fixture.get("selectionReason") or "Лист включён в проверочную выборку."
    anomalies = ", ".join(review.get("anomalyCodes", [])) or "нет"
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>{_escape(fixture['id'])}</title>
<style>{_CSS}</style></head><body><main>
<div class="eyebrow">DWG symbols · H2–H4c · подробный отчёт</div>
<h1>{_escape(filename)} — страница {page}</h1>
<p>{_escape(reason)}</p>
<section class="scope"><dl>
<dt>Исходный файл</dt><dd>{_escape(fixture['documentPath'])}</dd>
<dt>Чертёж</dt><dd>{_escape(Path(filename).stem)}</dd>
<dt>Страница файла</dt><dd>{page}</dd>
<dt>ID анализа</dt><dd>{_escape(fixture['id'])}</dd>
<dt>Полнота</dt><dd>{_escape(review['completeness'])}</dd>
<dt>Аномалии</dt><dd class="anomalies">{_escape(anomalies)}</dd>
</dl>{_scope_list(fixtures)}</section>
<div class="stats">
<div class="stat"><strong>{review['legendEntries']}</strong>легенда</div>
<div class="stat confirmed"><strong>{review['confirmed']}</strong>confirmed</div>
<div class="stat probable"><strong>{review['probable']}</strong>probable</div>
<div class="stat unknown"><strong>{review['unknownClusters']}</strong>неизвестных кластеров</div>
<div class="stat unknown"><strong>{review['unknownOccurrences']}</strong>неизвестных экземпляров</div>
<div class="stat furniture"><strong>{review.get('sheetFurniture', 0)}</strong>оформление листа</div>
<div class="stat object"><strong>{review.get('drawingObjects', 0)}</strong>объекты чертежа</div>
<div class="stat annotation"><strong>{review.get('drawingAnnotations', 0)}</strong>аннотации</div>
<div class="stat mark"><strong>{review.get('specificationMarks', 0)}</strong>марки / подписи</div>
<div class="stat textlabel"><strong>{review.get('textLabels', 0)}</strong>текст вне блока</div>
<div class="stat geometry"><strong>{review.get('fieldGeometry', 0)}</strong>линии / штриховки</div>
</div>
<nav class="toolbar"><a href="../index.html">Общий индекс</a>
<a href="page.svg">Исходный SVG-лист</a><a href="page_review.svg">Лист с разметкой</a>
<a href="{page_dir}/summary.json">Sidecar summary</a></nav>
<div class="notice">Confirmed означает точное совпадение блока. Probable требует проверки.
Unrecognized — кандидат H2, а не доказанный неизвестный условный знак.</div>
{section('Распознанная легенда', legend_cards, 'Легенда на странице не извлечена.')}
{section('Значки confirmed', confirmed, 'Confirmed-совпадений нет.')}
{section('Значки probable', probable, 'Probable-совпадений нет.')}
{section('Оформление листа', furniture_cards, 'Служебных блоков оформления не найдено.')}
{section('Объекты чертежа', object_cards, 'Конструктивных объектов в INSERT не выделено.')}
{section('Аннотации', annotation_cards, 'Служебных аннотаций не найдено.')}
{section('Марки / подписи', mark_cards, 'Буквенно-цифровых марок в INSERT не выделено.')}
{section('Текст вне блока', text_cards, 'Осей и размеров обычным текстом не выделено.')}
{section('Линии / штриховки', geometry_cards, 'Линий и контуров на поле не выделено.')}
{section('Нераспознанные', unknown_cards, 'Нераспознанных кластеров нет.')}
<footer>Описание неизвестных намеренно ограничено техническими данными:
семантические названия без доказательства не генерируются.</footer>
</main></body></html>"""


def _index_report(
    fixtures: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> str:
    totals = {
        key: sum(int(review.get(key, 0)) for review in reviews)
        for key in (
            "legendEntries",
            "confirmed",
            "probable",
            "unknownClusters",
            "unknownOccurrences",
            "sheetFurniture",
            "drawingObjects",
            "drawingAnnotations",
            "specificationMarks",
            "textLabels",
            "fieldGeometry",
        )
    }
    rows = "".join(
        "<tr>"
        f'<td><a href="{_escape(fixture["id"])}/report.html">'
        f"{_escape(fixture['id'])}</a></td>"
        f"<td>{_escape(fixture['documentPath'])}</td><td>{fixture['page']}</td>"
        f"<td>{review['legendEntries']}</td><td>{review['confirmed']}</td>"
        f"<td>{review['probable']}</td><td>{review['unknownClusters']}</td>"
        f"<td>{review.get('sheetFurniture', 0)}</td>"
        f"<td>{review.get('drawingObjects', 0)}</td>"
        f"<td>{review.get('drawingAnnotations', 0)}</td>"
        f"<td>{review.get('specificationMarks', 0)}</td>"
        f"<td>{review.get('textLabels', 0)}</td>"
        f"<td>{review.get('fieldGeometry', 0)}</td>"
        "</tr>"
        for fixture, review in zip(fixtures, reviews)
    )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>DWG symbol review</title>
<style>{_CSS}</style></head><body><main>
<div class="eyebrow">DWG symbols · H2–H4c</div>
<h1>Отчёты по распознаванию условных обозначений</h1>
<p>В анализ включено {len(fixtures)} DWG-листов. Для каждого файла доступен
отдельный отчёт с изображениями, описаниями и координатами.</p>
<section class="scope">{_scope_list(fixtures)}</section>
<div class="stats">
<div class="stat"><strong>{totals['legendEntries']}</strong>пунктов легенды</div>
<div class="stat confirmed"><strong>{totals['confirmed']}</strong>confirmed</div>
<div class="stat probable"><strong>{totals['probable']}</strong>probable</div>
<div class="stat unknown"><strong>{totals['unknownClusters']}</strong>кластеров</div>
<div class="stat unknown"><strong>{totals['unknownOccurrences']}</strong>экземпляров</div>
<div class="stat furniture"><strong>{totals['sheetFurniture']}</strong>оформление листа</div>
<div class="stat object"><strong>{totals['drawingObjects']}</strong>объекты чертежа</div>
<div class="stat annotation"><strong>{totals['drawingAnnotations']}</strong>аннотации</div>
<div class="stat mark"><strong>{totals['specificationMarks']}</strong>марки / подписи</div>
<div class="stat textlabel"><strong>{totals.get('textLabels', 0)}</strong>текст вне блока</div>
<div class="stat geometry"><strong>{totals.get('fieldGeometry', 0)}</strong>линии / штриховки</div>
</div>
<div class="notice">Это инвентаризация машинных результатов, а не измеренная
precision/recall: human annotation ещё не выполнена.</div>
<h2>Файлы и страницы</h2><div class="inventory-wrap"><table class="inventory">
<thead><tr><th>Отчёт</th><th>Файл</th><th>Страница</th><th>Легенда</th>
<th>Confirmed</th><th>Probable</th><th>Unknown clusters</th>
<th>Оформление</th><th>Объекты</th><th>Аннотации</th><th>Марки</th><th>Текст</th><th>Линии</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<footer>Источник: sidecar JSON и SVG-crops локального прогона review_10.</footer>
</main></body></html>"""


def write_html_reports(selection: str | Path, root: str | Path) -> Path:
    """Write one report per selected sheet plus a navigable index."""

    selection_data = json.loads(Path(selection).read_text(encoding="utf-8"))
    fixtures = selection_data["fixtures"]
    output = Path(root)
    reviews = [
        json.loads((output / fixture["id"] / "review.json").read_text(encoding="utf-8"))
        for fixture in fixtures
    ]
    for fixture, review in zip(fixtures, reviews):
        destination = output / fixture["id"]
        report = _document_report(fixture, fixtures, review, destination)
        (destination / "report.html").write_text(report, encoding="utf-8", newline="\n")
    index = output / "index.html"
    index.write_text(
        _index_report(fixtures, reviews),
        encoding="utf-8",
        newline="\n",
    )
    return index
