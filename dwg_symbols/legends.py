"""H3: deterministic legend localization and vector-row extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import html
import json
import math
from pathlib import Path
import re
from statistics import median
from typing import Any, Iterable, Mapping

from .blocks import extract_block_page
from .furniture import classify_sheet_furniture
from .schema import LegendEntry, NoteText, PageResult, stable_id
from .title_block import attach_title_block
from .dimension_read import attach_dimensions
from .axis_read import attach_axes
from .text_labels import attach_text_labels
from .schedule_join import attach_schedule_notes


_HEADING = re.compile(
    r"\b(?:условн\w*\s+обозначен\w*|легенд\w*)\b",
    re.IGNORECASE,
)
_NOTES_HEADING = re.compile(r"^\s*примечан\w*", re.IGNORECASE)
_NOTES_HEADING_SPLIT = re.compile(
    r"(?is)^\s*примечан\w*\s*:?\s*(.*)$",
)
_NOTES_NUMBERED = re.compile(r"(?=\b\d+\.\s)")
_NOTES_SOURCES = frozenset({"text", "mtext"})
_NOTES_COLUMN_PAD_LEFT = 5.0
_NOTES_COLUMN_MIN_WIDTH = 120.0
_MAX_ENTRIES = 200
_MAX_CROP_PRIMITIVES = 500


@dataclass(slots=True)
class LegendExtraction:
    page_result: PageResult
    crops: dict[str, str]
    primitives: list[dict[str, Any]]
    meta: dict[str, Any]
    texts: Any = None
    hatches: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class _Row:
    texts: list[Any]
    column_x: float
    y: float
    upper_y: float = 0.0
    lower_y: float = 0.0


def _plain(text: str) -> str:
    return " ".join(text.replace("\r", "\n").split())


def _is_label(text: str) -> bool:
    value = _plain(text)
    if len(value) < 3 or _HEADING.search(value):
        return False
    meaningful = sum(character.isalpha() for character in value)
    return meaningful >= 2


def has_legend_heading(texts: Iterable[Any]) -> bool:
    """Whether a sheet contains an explicit legend/notation heading."""

    return any(_HEADING.search(_plain(str(item.text))) for item in texts)


def has_notes_heading(texts: Iterable[Any]) -> bool:
    """Whether a sheet contains an explicit notes heading («Примечан…»)."""

    return any(_NOTES_HEADING.search(_plain(str(item.text))) for item in texts)


def _item_height(item: Any) -> float:
    return float(
        getattr(item, "height", 0.0) or getattr(item, "size", 0.0) or 0.0
    )


def _text_bbox(item: Any) -> tuple[float, float, float, float]:
    """Axis-aligned window around a paper-space TEXT insertion."""

    height = max(_item_height(item), 1.0)
    width = float(getattr(item, "width", 0.0) or 0.0)
    text = _plain(str(getattr(item, "text", "") or ""))
    span = max(width, len(text) * height * 0.55, height)
    x = float(getattr(item, "x", 0.0) or 0.0)
    y = float(getattr(item, "y", 0.0) or 0.0)
    return (x, y - height, x + span, y + height * 0.5)


def _union_bbox(
    boxes: Iterable[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    collected = list(boxes)
    if not collected:
        return None
    return (
        min(box[0] for box in collected),
        min(box[1] for box in collected),
        max(box[2] for box in collected),
        max(box[3] for box in collected),
    )


def _as_texts(texts: Any) -> list[Any]:
    if texts is None:
        return []
    return list(getattr(texts, "texts", texts) or [])


def _legend_zone(
    entries: Iterable[Any],
    texts: Iterable[Any],
) -> tuple[float, float, float, float] | None:
    """Union of extracted legend rows and their heading. Fail-closed."""

    items = list(texts)
    headings = [
        item
        for item in items
        if _HEADING.search(_plain(str(getattr(item, "text", "") or "")))
    ]
    row_boxes = [entry.bbox for entry in entries if getattr(entry, "bbox", None)]
    if not headings or not row_boxes:
        return None
    return _union_bbox([*(_text_bbox(item) for item in headings), *row_boxes])


def _as_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    box = tuple(float(item) for item in value)
    if len(box) != 4:
        return None
    return box  # type: ignore[return-value]


def _overlaps_x(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (first[2] < second[0] or first[0] > second[2])


def _point_in_bbox(
    x: float,
    y: float,
    bbox: tuple[float, float, float, float] | None,
) -> bool:
    if bbox is None:
        return False
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def _item_source(item: Any) -> str:
    return str(getattr(item, "source", "text") or "text").lower()


def _item_layer(item: Any) -> str:
    return str(getattr(item, "layer", "") or "")


def _notes_column(
    heading_bbox: tuple[float, float, float, float],
) -> tuple[float, float]:
    return (
        heading_bbox[0] - _NOTES_COLUMN_PAD_LEFT,
        max(heading_bbox[2], heading_bbox[0] + _NOTES_COLUMN_MIN_WIDTH),
    )


def _in_notes_column(
    item: Any,
    col_x0: float,
    col_x1: float,
) -> bool:
    box = _text_bbox(item)
    return not (box[2] < col_x0 or box[0] > col_x1)


def _floor_below_heading(
    heading_bbox: tuple[float, float, float, float],
    zones: Mapping[str, Any] | None,
) -> float | None:
    """Top of stamp/legend sitting below the heading and overlapping in X."""

    floor: float | None = None
    heading_top = heading_bbox[3]
    for name in ("title_block", "legend"):
        box = _as_bbox((zones or {}).get(name))
        if box is None or not _overlaps_x(heading_bbox, box):
            continue
        if box[1] >= heading_top:
            continue
        if box[3] <= heading_top:
            floor = box[3] if floor is None else max(floor, box[3])
    return floor


def _is_notes_heading(item: Any) -> bool:
    return bool(_NOTES_HEADING.search(_plain(str(getattr(item, "text", "") or ""))))


def _is_notes_body_item(
    item: Any,
    *,
    heading: Any,
    col_x0: float,
    col_x1: float,
    floor: float | None,
    stamp: tuple[float, float, float, float] | None,
    legend: tuple[float, float, float, float] | None,
) -> bool:
    if item is heading:
        return False
    if _item_source(item) not in _NOTES_SOURCES:
        return False
    text = _plain(str(getattr(item, "text", "") or ""))
    if not text or _is_notes_heading(item) or _HEADING.search(text):
        return False
    y = float(getattr(item, "y", 0.0) or 0.0)
    x = float(getattr(item, "x", 0.0) or 0.0)
    heading_y = float(getattr(heading, "y", 0.0) or 0.0)
    if y > heading_y:
        return False
    if floor is not None and y < floor:
        return False
    if not _in_notes_column(item, col_x0, col_x1):
        return False
    if _point_in_bbox(x, y, stamp) or _point_in_bbox(x, y, legend):
        return False
    return True


def _note_text_record(item: Any, *, document_id: str, page: int) -> NoteText:
    text = _plain(str(getattr(item, "text", "") or ""))
    x = float(getattr(item, "x", 0.0) or 0.0)
    y = float(getattr(item, "y", 0.0) or 0.0)
    bbox = _text_bbox(item)
    return _note_text(
        document_id=document_id,
        page=page,
        text=text,
        source=_item_source(item),
        layer=_item_layer(item),
        x=x,
        y=y,
        bbox=bbox,
    )


def _note_text(
    *,
    document_id: str,
    page: int,
    text: str,
    source: str,
    layer: str,
    x: float,
    y: float,
    bbox: tuple[float, float, float, float],
) -> NoteText:
    return NoteText(
        id=stable_id("NT", document_id, page, round(x, 2), round(y, 2), text),
        page=page,
        text=text,
        source=source,
        layer=layer,
        x=x,
        y=y,
        bbox=bbox,
    )


def _heading_inline_bodies(
    heading: Any,
    *,
    document_id: str,
    page: int,
) -> list[NoteText]:
    """Remainder of «Примечание: 1. …» when body lives in the same MTEXT."""

    raw = str(getattr(heading, "text", "") or "").replace("\r", "\n")
    match = _NOTES_HEADING_SPLIT.match(raw)
    if not match:
        return []
    remainder = match.group(1).strip()
    if not remainder:
        return []
    chunks = [part.strip() for part in _NOTES_NUMBERED.split(remainder) if part.strip()]
    if not chunks:
        chunks = [_plain(remainder)]
    bbox = _text_bbox(heading)
    x = float(getattr(heading, "x", 0.0) or 0.0)
    y = float(getattr(heading, "y", 0.0) or 0.0)
    source = _item_source(heading)
    layer = _item_layer(heading)
    records: list[NoteText] = []
    for index, chunk in enumerate(chunks):
        text = _plain(chunk)
        if not text:
            continue
        records.append(
            _note_text(
                document_id=document_id,
                page=page,
                text=text,
                source=source,
                layer=layer,
                x=x,
                y=y - index * 0.01,
                bbox=bbox,
            )
        )
    return records


def _notes_window(
    texts: Iterable[Any],
    zones: Mapping[str, Any] | None,
    *,
    document_id: str,
    page: int,
) -> tuple[tuple[float, float, float, float] | None, list[NoteText]]:
    """Heading plus native TEXT below it, stopped at stamp/legend.

    No «Примечан…» anchor → no window and no body. A heading MTEXT that
    already contains «1. …» is split into ``notesTexts``. Does not recut
    ``drawing_field``. TEXT above the heading stays out of the zone.
    """

    headings = [item for item in texts if _is_notes_heading(item)]
    if not headings:
        return None, []
    stamp = _as_bbox((zones or {}).get("title_block"))
    legend = _as_bbox((zones or {}).get("legend"))
    boxes: list[tuple[float, float, float, float]] = []
    bodies: list[NoteText] = []
    seen: set[str] = set()
    for heading in headings:
        heading_bbox = _text_bbox(heading)
        col_x0, col_x1 = _notes_column(heading_bbox)
        floor = _floor_below_heading(heading_bbox, zones)
        column_boxes = [heading_bbox]
        for record in _heading_inline_bodies(
            heading, document_id=document_id, page=page
        ):
            if record.id in seen:
                continue
            seen.add(record.id)
            bodies.append(record)
        for item in texts:
            if not _is_notes_body_item(
                item,
                heading=heading,
                col_x0=col_x0,
                col_x1=col_x1,
                floor=floor,
                stamp=stamp,
                legend=legend,
            ):
                continue
            record = _note_text_record(item, document_id=document_id, page=page)
            if record.id in seen:
                continue
            seen.add(record.id)
            bodies.append(record)
            column_boxes.append(record.bbox)
        union = _union_bbox(column_boxes)
        if union is None:
            continue
        ymin = union[1]
        if floor is not None and floor < union[3]:
            ymin = min(ymin, floor)
        boxes.append((union[0], ymin, union[2], union[3]))
    zone = _union_bbox(boxes)
    bodies.sort(key=lambda item: (-item.y, item.x, item.text))
    return zone, bodies


def attach_legend_notes_zones(
    result: PageResult,
    texts: Any = None,
) -> PageResult:
    """Add optional ``legend`` / ``notes`` keys and ``notesTexts``.

    Does not recut drawing_field. No title-block zones → nothing to attach
    (schema requires title_block). No heading or no extracted rows → no
    ``legend`` key. No «Примечан…» anchor → no ``notes`` key and no body.
    INSERT in the legend table is not reclassified.
    """

    zones = result.sheet_zones
    if not zones:
        result.notes_texts = []
        return result
    items = _as_texts(texts)
    updated = dict(zones)
    legend = _legend_zone(result.legend_entries, items)
    if legend is None:
        updated.pop("legend", None)
    else:
        updated["legend"] = list(legend)
    notes, bodies = _notes_window(
        items,
        updated,
        document_id=result.document_id,
        page=result.page,
    )
    if notes is None:
        updated.pop("notes", None)
        result.notes_texts = []
    else:
        updated["notes"] = list(notes)
        result.notes_texts = bodies
    result.sheet_zones = updated
    return result


def _cluster_by_x(items: Iterable[Any], tolerance: float = 1.8) -> list[list[Any]]:
    clusters: list[list[Any]] = []
    for item in sorted(items, key=lambda candidate: candidate.x):
        if not clusters:
            clusters.append([item])
            continue
        center = median(candidate.x for candidate in clusters[-1])
        if abs(item.x - center) <= tolerance:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    return clusters


def _rows_for_column(items: list[Any], heading_y: float) -> list[_Row]:
    ordered = sorted(items, key=lambda item: item.y, reverse=True)
    rows: list[list[Any]] = []
    for item in ordered:
        if not rows:
            rows.append([item])
            continue
        previous = rows[-1][-1]
        # A large empty band normally separates the legend from the title
        # block or notes below it. Do not bridge that boundary.
        if previous.y - item.y > 55.0:
            break
        join_distance = max(5.2, 1.8 * max(float(previous.height), float(item.height)))
        if previous.y - item.y <= join_distance:
            rows[-1].append(item)
        else:
            rows.append([item])

    result = [
        _Row(
            texts=group,
            column_x=median(item.x for item in group),
            y=sum(item.y for item in group) / len(group),
        )
        for group in rows
    ]
    for index, row in enumerate(result):
        row.upper_y = (
            (heading_y + row.y) / 2
            if index == 0
            else (result[index - 1].y + row.y) / 2
        )
        if index + 1 < len(result):
            row.lower_y = (row.y + result[index + 1].y) / 2
        else:
            height = max(float(item.height) for item in row.texts)
            row.lower_y = min(item.y for item in row.texts) - max(3.0, height)
    return result


def _primitive_bbox(primitive: dict[str, Any]) -> tuple[float, float, float, float] | None:
    points = primitive.get("points") or ()
    if not points:
        return None
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _intersects(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (
        first[2] < second[0]
        or first[0] > second[2]
        or first[3] < second[1]
        or first[1] > second[3]
    )


def _clip_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    bbox: tuple[float, float, float, float],
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Liang-Barsky clip of one segment to an axis-aligned legend cell."""

    x0, y0 = float(start[0]), float(start[1])
    x1, y1 = float(end[0]), float(end[1])
    dx, dy = x1 - x0, y1 - y0
    lower, upper = 0.0, 1.0
    for direction, distance in (
        (-dx, x0 - bbox[0]),
        (dx, bbox[2] - x0),
        (-dy, y0 - bbox[1]),
        (dy, bbox[3] - y0),
    ):
        if abs(direction) < 1e-12:
            if distance < 0:
                return None
            continue
        ratio = distance / direction
        if direction < 0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return None
    return (
        (x0 + lower * dx, y0 + lower * dy),
        (x0 + upper * dx, y0 + upper * dy),
    )


def _crop_primitives(
    primitives: Iterable[dict[str, Any]],
    bbox: tuple[float, float, float, float],
) -> tuple[list[dict[str, Any]], bool]:
    selected: list[dict[str, Any]] = []
    exceeded = False
    for primitive in primitives:
        if primitive.get("type") == "text":
            continue
        primitive_bbox = _primitive_bbox(primitive)
        if primitive_bbox is None or not _intersects(primitive_bbox, bbox):
            continue
        points = primitive.get("points") or ()
        for start, end in zip(points, points[1:]):
            clipped = _clip_segment(start, end, bbox)
            if clipped is None or clipped[0] == clipped[1]:
                continue
            selected.append({**primitive, "type": "line", "points": list(clipped)})
            if len(selected) >= _MAX_CROP_PRIMITIVES:
                exceeded = True
                break
        if exceeded:
            break
    return selected, exceeded


def _vector_signature(
    primitives: Iterable[dict[str, Any]],
    bbox: tuple[float, float, float, float],
) -> str | None:
    x0, y0, x1, y1 = bbox
    width = max(x1 - x0, 1e-6)
    height = max(y1 - y0, 1e-6)
    normalized: list[dict[str, Any]] = []
    for primitive in primitives:
        points = [
            [
                round(min(1.0, max(0.0, (float(x) - x0) / width)), 3),
                round(min(1.0, max(0.0, (float(y) - y0) / height)), 3),
            ]
            for x, y in primitive.get("points", ())
        ]
        if len(points) < 2:
            continue
        normalized.append(
            {
                "type": primitive.get("type", "line"),
                "points": points,
                "lw": round(float(primitive.get("lw") or 0.0), 2),
            }
        )
    if not normalized:
        return None
    normalized.sort(
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
    )
    payload = json.dumps(
        {"version": 1, "geometry": normalized},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"legend-vector-v1:{hashlib.sha256(payload).hexdigest()}"


def _crop_svg(
    primitives: Iterable[dict[str, Any]],
    bbox: tuple[float, float, float, float],
) -> str:
    x0, y0, x1, y1 = bbox
    width = max(x1 - x0, 1e-6)
    height = max(y1 - y0, 1e-6)
    scale = 8.0
    svg_width = max(80, math.ceil(width * scale))
    svg_height = max(32, math.ceil(height * scale))
    paths: list[str] = []
    for primitive in primitives:
        points = [
            (
                (float(x) - x0) / width * svg_width,
                (y1 - float(y)) / height * svg_height,
            )
            for x, y in primitive.get("points", ())
        ]
        if len(points) < 2:
            continue
        encoded = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
        color = html.escape(str(primitive.get("color") or "#000000"))
        line_width = max(0.5, float(primitive.get("lw") or 0.25) * scale)
        paths.append(
            f'<polyline points="{encoded}" fill="none" stroke="{color}" '
            f'stroke-width="{line_width:.2f}" vector-effect="non-scaling-stroke"/>'
        )
    content = "".join(paths)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_width}" '
        f'height="{svg_height}" viewBox="0 0 {svg_width} {svg_height}">'
        f'<rect width="100%" height="100%" fill="white"/>{content}</svg>\n'
    )


def detect_legend_entries(
    *,
    document_id: str,
    page: int,
    texts: Iterable[Any],
    primitives: Iterable[dict[str, Any]],
    paper_width: float,
    paper_height: float,
) -> tuple[list[LegendEntry], dict[str, str], list[str]]:
    """Extract legend rows from paper-space text and vector primitives."""

    all_texts = list(texts)
    all_primitives = list(primitives)
    headings = [item for item in all_texts if _HEADING.search(_plain(item.text))]
    if not headings:
        return [], {}, ["LEGEND_NOT_FOUND"]

    entries: list[LegendEntry] = []
    crops: dict[str, str] = {}
    anomalies: list[str] = []
    for heading_index, heading in enumerate(headings):
        depth = min(300.0, max(80.0, paper_height * 0.45))
        candidates = [
            item
            for item in all_texts
            if item is not heading
            and heading.y - depth <= item.y < heading.y
            and heading.x - 45.0 <= item.x <= heading.x + min(220.0, paper_width * 0.25)
            and _is_label(item.text)
        ]
        columns = []
        for cluster in _cluster_by_x(candidates):
            lengths = [len(_plain(item.text)) for item in cluster]
            # A real legend column is repeated and descriptive. This rejects
            # nearby title-block columns (roles, dates, surnames and sheet data).
            if len(cluster) >= 3 and median(lengths) >= 16:
                columns.append(cluster)
        for column_index, column in enumerate(columns):
            rows = _rows_for_column(column, heading.y)
            for row_index, row in enumerate(rows):
                source_texts = tuple(_plain(item.text) for item in row.texts)
                label = " ".join(source_texts)
                text_right = max(
                    item.x
                    + max(
                        float(getattr(item, "width", 0.0) or 0.0),
                        len(_plain(item.text)) * float(item.height) * 0.55,
                    )
                    for item in row.texts
                )
                symbol_bbox = (
                    row.column_x - 35.0,
                    row.lower_y,
                    row.column_x - 2.0,
                    row.upper_y,
                )
                row_bbox = (
                    symbol_bbox[0],
                    row.lower_y,
                    text_right,
                    row.upper_y,
                )
                vectors, exceeded = _crop_primitives(all_primitives, symbol_bbox)
                signature = _vector_signature(vectors, symbol_bbox)
                entry_id = stable_id(
                    "LE",
                    document_id,
                    page,
                    heading_index,
                    column_index,
                    row_index,
                    round(row.column_x, 2),
                    round(row.y, 2),
                    label,
                )
                crop_path = f"legend_crops/{entry_id}.svg" if signature else None
                entries.append(
                    LegendEntry(
                        id=entry_id,
                        page=page,
                        label=label,
                        status="extracted" if signature else "unmatched",
                        source_kind="dwg_vector_legend",
                        signature=signature,
                        bbox=row_bbox,
                        symbol_bbox=symbol_bbox,
                        crop_path=crop_path,
                        source_texts=source_texts,
                        confidence=0.92 if signature else 0.68,
                    )
                )
                if signature:
                    crops[entry_id] = _crop_svg(vectors, symbol_bbox)
                if exceeded:
                    anomalies.append("LEGEND_CROP_PRIMITIVE_BUDGET_EXCEEDED")
                if len(entries) >= _MAX_ENTRIES:
                    anomalies.append("LEGEND_ENTRY_BUDGET_EXCEEDED")
                    return entries, crops, sorted(set(anomalies))

    if not entries:
        anomalies.append("LEGEND_ROWS_NOT_EXTRACTED")
    elif any(entry.signature is None for entry in entries):
        anomalies.append("LEGEND_ENTRIES_WITHOUT_VECTOR_SIGNATURE")
    return entries, crops, sorted(set(anomalies))


def extract_legend_page(path: str | Path, page: int) -> LegendExtraction:
    """Run H2 plus H3 and return sidecar data with SVG crops."""

    from dwg_geometry import sheet_primitives
    from dwg_sheets import sheets_for

    source = Path(path)
    result = extract_block_page(source, page)
    _, sheets = sheets_for(source)
    sheet = sheets[page - 1]
    primitives, meta, _ = sheet_primitives(source, page)
    hatches = list(meta.get("hatches") or [])
    entries, crops, anomalies = detect_legend_entries(
        document_id=result.document_id,
        page=page,
        texts=sheet.texts,
        primitives=primitives,
        paper_width=sheet.paper_width,
        paper_height=sheet.paper_height,
    )
    result.legend_entries = entries
    result.anomaly_codes = [
        code for code in result.anomaly_codes if code != "LEGEND_STAGE_NOT_RUN"
    ]
    result.anomaly_codes.extend(
        code for code in anomalies if code not in result.anomaly_codes
    )
    result = classify_sheet_furniture(result, sheet_bbox=meta.get("bbox"))
    result = attach_title_block(result, sheet.texts, sheet_bbox=meta.get("bbox"))
    result = attach_dimensions(result, sheet.texts)
    result = attach_axes(result, sheet.texts)
    result = attach_text_labels(result, sheet.texts)
    result = attach_schedule_notes(result)
    from .field_geometry import attach_field_geometry

    result = attach_field_geometry(result, primitives, hatches)
    return LegendExtraction(
        result, crops, primitives, meta, sheet.texts, hatches
    )


def write_legend_crops(page_dir: str | Path, crops: dict[str, str]) -> Path:
    """Write deterministic SVG crops next to the public page sidecars."""

    destination = Path(page_dir) / "legend_crops"
    destination.mkdir(parents=True, exist_ok=True)
    expected = {f"{entry_id}.svg" for entry_id in crops}
    for existing in destination.glob("*.svg"):
        if existing.name not in expected:
            existing.unlink()
    for entry_id, svg in sorted(crops.items()):
        target = destination / f"{entry_id}.svg"
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(svg, encoding="utf-8", newline="\n")
        temporary.replace(target)
    return destination
