"""Clickable LINE/POLYLINE/HATCH on the drawing field — not INSERT.

A click can say «линия / штриховка на поле». A legend label is attached only
when a *same-sheet* sample uniquely matches: the cell's simple stroke, a native
HATCH pattern, or the existing vector hash. That is also §5 (PZU): the row
must live on *this* sheet. Project-catalog / GOST genplan dictionaries are not
applied. A shared sample (one drawing — several labels) stays unlabeled.
Hatch uniqueness stays among *all* legend rows; a unique sample on a
non-soil row (УГВ, дата замера, отметки, «ГРАНИЦЫ») does not label the
fill or a field stroke. HatchPolicy.IGNORE still hides fills on the sheet SVG.

Fail-closed: stamp, legend and notes windows are skipped; FORMAT-like layers
are skipped; a shared sample across two legend rows stays unlabeled.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from .schema import FieldGeometry, PageResult, stable_id


_MIN_LENGTH_MM = 8.0
_CLOSE_MM = 0.2
_MAX_ITEMS = 160
_MAX_SIMPLE_SEGMENTS = 8
_ANGLE_TOL = math.radians(12.0)
_SKIP_LAYERS = frozenset(
    {"format", "nadpisi", "подписи", "stamp", "рамка", "otmetki"}
)
_STROKE_TYPES = frozenset({"line", "polyline"})
_FIELD_TYPES = frozenset({"line", "polyline", "hatch"})
_SOIL_FILL_STEMS = (
    "глин",
    "суглин",
    "супес",
    "песок",
    "песк",
    "торф",
    "сапропел",
    "известняк",
    "почвенно-растительн",
    "дресв",
    "щебн",
    "галечник",
    "мергел",
    "доломит",
    "аргиллит",
    "алевролит",
    "песчаник",
)


def _folded(text: str) -> str:
    return (text or "").strip().casefold().replace("ё", "е")


def _zone(
    result: PageResult, name: str
) -> tuple[float, float, float, float] | None:
    zones = result.sheet_zones or {}
    raw = zones.get(name)
    if raw is None:
        return None
    box = tuple(float(value) for value in raw)
    if len(box) != 4 or box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box  # type: ignore[return-value]


def _in_bbox(
    x: float, y: float, bbox: tuple[float, float, float, float] | None
) -> bool:
    if bbox is None:
        return False
    x0, y0, x1, y1 = bbox
    return x0 <= x <= x1 and y0 <= y <= y1


def _fully_inside(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float] | None,
) -> bool:
    if outer is None:
        return False
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


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


def _points_of(primitive: Mapping[str, Any]) -> list[tuple[float, float]]:
    return [
        (float(point[0]), float(point[1]))
        for point in primitive.get("points") or ()
    ]


def _length(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for start, end in zip(points, points[1:]):
        total += ((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5
    return total


def _is_closed(points: list[tuple[float, float]]) -> bool:
    if len(points) < 4:
        return False
    dx = points[0][0] - points[-1][0]
    dy = points[0][1] - points[-1][1]
    return (dx * dx + dy * dy) ** 0.5 <= _CLOSE_MM


def _raw_bbox(
    points: list[tuple[float, float]],
) -> tuple[float, float, float, float] | None:
    if len(points) < 2:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _padded(
    bbox: tuple[float, float, float, float], span: float = 1.0
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    if x1 - x0 < span:
        mid = (x0 + x1) / 2
        x0, x1 = mid - span / 2, mid + span / 2
    if y1 - y0 < span:
        mid = (y0 + y1) / 2
        y0, y1 = mid - span / 2, mid + span / 2
    return (x0, y0, x1, y1)


def _span(bbox: tuple[float, float, float, float]) -> float:
    return max(bbox[2] - bbox[0], bbox[3] - bbox[1])


def _eligible(
    primitive: Mapping[str, Any],
    result: PageResult,
) -> tuple[list[tuple[float, float]], tuple[float, float, float, float]] | None:
    kind = str(primitive.get("type") or "")
    if kind not in _FIELD_TYPES:
        return None
    if _folded(str(primitive.get("layer") or "")) in _SKIP_LAYERS:
        return None
    points = _points_of(primitive)
    bbox = _raw_bbox(points)
    if bbox is None:
        return None
    if kind == "hatch":
        if _span(bbox) < _MIN_LENGTH_MM:
            return None
    elif _length(points) < _MIN_LENGTH_MM:
        return None
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    stamp = _zone(result, "title_block")
    field = _zone(result, "drawing_field")
    legend = _zone(result, "legend")
    notes = _zone(result, "notes")
    if stamp is not None and _in_bbox(cx, cy, stamp):
        return None
    if legend is not None and (
        _in_bbox(cx, cy, legend) or _fully_inside(bbox, legend)
    ):
        return None
    if notes is not None and _in_bbox(cx, cy, notes):
        return None
    if field is not None and not _in_bbox(cx, cy, field):
        return None
    return points, bbox


def _legend_row_text(entry: Any) -> str:
    parts = [str(getattr(entry, "label", "") or "")]
    parts.extend(str(item) for item in getattr(entry, "source_texts", ()) or ())
    return _folded(" ".join(parts))


def _legend_is_soil_fill(entry: Any) -> bool:
    """Lithology row only. УГВ, date, elevations and «ГРАНИЦЫ» are not soil."""

    text = _legend_row_text(entry)
    if not text:
        return False
    compact = "".join(text.split())
    if "границ" in compact:
        return False
    if "датазамера" in compact:
        return False
    if "уровнягрунтовыхвод" in compact or "уровеньгрунтовыхвод" in compact:
        return False
    if "отметка" in text and any(
        token in text for token in ("устья", "подошв", "забоя")
    ):
        return False
    return any(stem in text for stem in _SOIL_FILL_STEMS)


def _hatch_key(primitive: Mapping[str, Any]) -> str | None:
    name = str(primitive.get("pattern") or "").strip().upper()
    if not name:
        return None
    scale = round(float(primitive.get("pattern_scale") or 1.0), 1)
    color = str(primitive.get("color") or "").strip().lower()
    return f"hatch-pattern-v1:{name}|{scale}|{color}"


def _angles_close(first: float, second: float) -> bool:
    delta = abs(first - second) % math.pi
    delta = min(delta, math.pi - delta)
    return delta <= _ANGLE_TOL


def _simple_crop_signature(vectors: Iterable[Mapping[str, Any]]) -> str | None:
    """One collinear stroke from a legend cell. Not the whole hatch tick field."""

    from .legends import _vector_signature

    segments: list[tuple[tuple[float, float], tuple[float, float], float]] = []
    for primitive in vectors:
        if str(primitive.get("type") or "") not in _STROKE_TYPES:
            continue
        points = _points_of(primitive)
        lw = round(float(primitive.get("lw") or 0.0), 2)
        for start, end in zip(points, points[1:]):
            if _length([start, end]) < 1.0:
                continue
            segments.append((start, end, lw))
    if not segments or len(segments) > _MAX_SIMPLE_SEGMENTS:
        return None
    base = math.atan2(
        segments[0][1][1] - segments[0][0][1],
        segments[0][1][0] - segments[0][0][0],
    )
    if not all(
        _angles_close(
            base,
            math.atan2(end[1] - start[1], end[0] - start[0]),
        )
        for start, end, _lw in segments
    ):
        return None
    start, end, lw = max(segments, key=lambda item: _length([item[0], item[1]]))
    bbox = _raw_bbox([start, end])
    if bbox is None:
        return None
    return _vector_signature(
        [{"type": "line", "points": [start, end], "lw": lw}],
        bbox,
    )


def _legend_sample_keys(
    entry: Any,
    strokes: list[Mapping[str, Any]],
    hatches: list[Mapping[str, Any]],
) -> set[str]:
    keys: set[str] = set()
    if entry.signature:
        keys.add(str(entry.signature))
    bbox = entry.symbol_bbox or entry.bbox
    if not bbox or len(bbox) != 4:
        return keys
    cell = tuple(float(value) for value in bbox)
    from .legends import _crop_primitives

    vectors, _exceeded = _crop_primitives(strokes, cell)
    simple = _simple_crop_signature(vectors)
    if simple:
        keys.add(simple)
    for hatch in hatches:
        box = _raw_bbox(_points_of(hatch))
        if box is None or not _intersects(box, cell):
            continue
        key = _hatch_key(hatch)
        if key:
            keys.add(key)
    return keys


def _unique_legend_keys(
    result: PageResult,
    strokes: list[Mapping[str, Any]],
    hatches: list[Mapping[str, Any]],
) -> dict[str, Any]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for entry in result.legend_entries:
        for key in _legend_sample_keys(entry, strokes, hatches):
            grouped[key].append(entry)
    return {
        key: rows[0]
        for key, rows in grouped.items()
        if len({id(row) for row in rows}) == 1
    }


def _kind_of(primitive: Mapping[str, Any], points: list[tuple[float, float]]) -> str:
    if str(primitive.get("type") or "") == "hatch":
        return "hatch"
    if _is_closed(points):
        return "closed"
    return "line"


def _field_keys(
    primitive: Mapping[str, Any],
    points: list[tuple[float, float]],
    _bbox: tuple[float, float, float, float],
    vector_signature: str | None,
) -> list[str]:
    keys: list[str] = []
    if str(primitive.get("type") or "") == "hatch":
        key = _hatch_key(primitive)
        if key:
            keys.append(key)
        return keys
    if vector_signature:
        keys.append(vector_signature)
    if not _is_closed(points):
        simple = _simple_crop_signature(
            [
                {
                    "type": primitive.get("type"),
                    "points": points,
                    "lw": primitive.get("lw"),
                }
            ]
        )
        if simple and simple not in keys:
            keys.append(simple)
    return keys


def collect_field_geometry(
    result: PageResult,
    primitives: Iterable[Mapping[str, Any]] | None = None,
    hatches: Iterable[Mapping[str, Any]] | None = None,
) -> list[FieldGeometry]:
    """Field LINE/POLYLINE/HATCH windows. Empty when nothing proven."""

    from .legends import _vector_signature

    strokes = [
        item
        for item in (primitives or ())
        if str(item.get("type") or "") in _STROKE_TYPES
    ]
    hatch_items = list(hatches or ())
    unique_rows = _unique_legend_keys(result, strokes, hatch_items)
    items: list[FieldGeometry] = []
    for index, primitive in enumerate([*strokes, *hatch_items]):
        picked = _eligible(primitive, result)
        if picked is None:
            continue
        points, raw_bbox = picked
        signature = None
        if str(primitive.get("type") or "") != "hatch":
            signature = _vector_signature(
                [
                    {
                        "type": primitive.get("type"),
                        "points": points,
                        "lw": primitive.get("lw"),
                    }
                ],
                raw_bbox,
            )
        kind = _kind_of(primitive, points)
        row = None
        for key in _field_keys(primitive, points, raw_bbox, signature):
            hit = unique_rows.get(key)
            if hit is None:
                continue
            if not _legend_is_soil_fill(hit):
                continue
            row = hit
            break
        label = str(row.label or "") if row is not None else ""
        legend_id = row.id if row is not None else ""
        items.append(
            FieldGeometry(
                id=stable_id(
                    "FG",
                    index,
                    kind,
                    round(raw_bbox[0], 3),
                    round(raw_bbox[1], 3),
                    signature or _hatch_key(primitive) or "",
                ),
                page=result.page,
                kind=kind,
                layer=str(primitive.get("layer") or ""),
                color=str(primitive.get("color") or ""),
                lineweight=round(float(primitive.get("lw") or 0.0), 2),
                bbox=_padded(raw_bbox),
                signature=signature or _hatch_key(primitive) or "",
                legend_entry_id=legend_id,
                label=label,
                source="sheet_primitive",
                note="" if row is not None else "",
            )
        )
    rank = {"hatch": 0, "line": 1, "closed": 2}
    items.sort(
        key=lambda item: (
            0 if item.legend_entry_id else 1,
            rank.get(item.kind, 9),
            -(item.bbox[2] - item.bbox[0] + item.bbox[3] - item.bbox[1]),
            item.bbox[1],
            item.bbox[0],
            item.id,
        )
    )
    return items[:_MAX_ITEMS]


def attach_field_geometry(
    result: PageResult,
    primitives: Iterable[Mapping[str, Any]] | None = None,
    hatches: Iterable[Mapping[str, Any]] | None = None,
) -> PageResult:
    """Write ``fieldGeometry`` after sheet zones exist. Does not touch INSERT."""

    result.field_geometry = collect_field_geometry(result, primitives, hatches)
    return result


def dump_sheet_geometry(drawing: str | Path, page: int) -> dict[str, Any]:
    """Inventory field LINE/POLYLINE/HATCH and optional same-sheet labels."""

    from .legends import extract_legend_page

    source = Path(drawing)
    extraction = extract_legend_page(source, page)
    result = attach_field_geometry(
        extraction.page_result,
        extraction.primitives,
        extraction.hatches,
    )
    vectors = [
        item
        for item in extraction.primitives
        if str(item.get("type") or "") in _STROKE_TYPES
    ]
    return {
        "documentPath": str(source),
        "page": page,
        "primitives": len(vectors),
        "hatches": len(extraction.hatches or []),
        "fieldGeometry": [item.to_dict() for item in result.field_geometry],
        "kinds": dict(
            sorted(Counter(item.kind for item in result.field_geometry).items())
        ),
        "labeled": sum(1 for item in result.field_geometry if item.legend_entry_id),
        "note": (
            "fieldGeometry is LINE/POLYLINE/HATCH in the drawing field, not INSERT. "
            "A label requires a unique same-sheet sample (simple stroke, hatch "
            "pattern, or vector hash). Native HATCH is inventoried separately; "
            "the sheet SVG still uses HatchPolicy.IGNORE. PZU / project legend "
            "is not applied."
        ),
    }
