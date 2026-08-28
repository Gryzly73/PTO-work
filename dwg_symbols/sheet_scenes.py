"""View titles on the drawing field → ``sheetScenes``. Fail-closed, no viewport cut.

A sheet may show several views in one paperspace viewport. Titles are native
TEXT / MTEXT: «Разрез», «Схема расположения», «по линии XII-XII», or a roman
cut line ``XII-XII`` (optionally ``(по отчету ИГИ)``). No such anchor → no
scenes, four cuts are not invented. Field ``кругN`` and axis
``specification_mark`` INSERTs inside a scene bbox fill ``soilIds`` / ``axisIds``
without changing roles. ``кругN`` counts even without a local legend
join: the block name is the layer id. Scheme titles do not take soil keys.
If stacked titles hug the top of the field, the view sits below the caption
(IGR 2×2), not above it (GOST caption-below). ``textLabels`` stay axis/size
only. ``drawing_field`` is not recut. Pairwise ``Relationship`` stays unused
(1:N lives on the scene).
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Iterable, Mapping

from .axis_read import is_axis_insert
from .circle_key import parse_circle_block_name
from .schema import PageResult, SheetScene, SymbolInstance, stable_id

_RELATIONSHIP_PLACEHOLDER = "RELATIONSHIP_STAGE_NOT_RUN"
_TITLE_SOURCES = frozenset({"text", "mtext"})
_ROMAN = r"[IVXLC]+"
_ROMAN_LINE = rf"{_ROMAN}\s*[-–—−]\s*{_ROMAN}"
_VIEW_TITLE = re.compile(
    r"(?i)^\s*(?:"
    r"схем\w*\s+расположен\w+"
    r"|(?:геологическ\w+\s+)?разрез\b"
    r"|по\s+лини\w*\s+"
    + _ROMAN_LINE
    + r"|"
    + _ROMAN_LINE
    + r")"
)
_SCHEME = re.compile(r"(?i)схем\w*\s+расположен")
_WELL_SCHEME = re.compile(r"(?i)(?:скважин|выработ)")
_SECTION = re.compile(r"(?i)разрез")
_MTEXT_BRACES = re.compile(r"\{[^}]*\}")
_MTEXT_TRAILING = re.compile(r"\)+[0-9a-fA-F]{3,6}\s*$")
_CLUSTER_GAP_MM = 18.0
_COLUMN_GAP_MM = 80.0
_CAPTION_ABOVE_SLACK_MM = 80.0
_RESERVED_ZONES = ("title_block", "legend", "notes")


@dataclass(frozen=True, slots=True)
class _TitleHit:
    text: str
    x: float
    y: float
    bbox: tuple[float, float, float, float]


def _plain(text: str) -> str:
    cleaned = _MTEXT_BRACES.sub(" ", text.replace("\r", "\n"))
    cleaned = _MTEXT_TRAILING.sub(")", cleaned)
    return " ".join(cleaned.split())


def _as_texts(texts: Any) -> list[Any]:
    if texts is None:
        return []
    return list(getattr(texts, "texts", texts) or [])


def _item_height(item: Any) -> float:
    return float(getattr(item, "height", 0.0) or getattr(item, "size", 0.0) or 0.0)


def _text_bbox(item: Any) -> tuple[float, float, float, float]:
    height = max(_item_height(item), 1.0)
    text = _plain(str(getattr(item, "text", "") or ""))
    # Ignore MTEXT wrap width: four cut titles share a wide box and would
    # otherwise collapse into one scene.
    span = max(len(text) * height * 0.55, height)
    x = float(getattr(item, "x", 0.0) or 0.0)
    y = float(getattr(item, "y", 0.0) or 0.0)
    return (x, y - height, x + span, y + height * 0.5)


def _as_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    box = tuple(float(item) for item in value)
    if len(box) != 4 or box[2] < box[0] or box[3] < box[1]:
        return None
    return box  # type: ignore[return-value]


def _point_in_bbox(
    x: float,
    y: float,
    bbox: tuple[float, float, float, float] | None,
) -> bool:
    if bbox is None:
        return False
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


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


def _nearby(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    gap: float,
) -> bool:
    return not (
        first[2] + gap < second[0]
        or second[2] + gap < first[0]
        or first[3] + gap < second[1]
        or second[3] + gap < first[1]
    )


def is_view_title(text: str) -> bool:
    """Whether a native string is a view heading, not a legend/note line."""

    return bool(_VIEW_TITLE.search(_plain(text)))


def _is_well_scheme_title(title: str) -> bool:
    """Plan of wells, not a 'схема … на разрезе' soil cut."""

    return bool(_WELL_SCHEME.search(title) and not _SECTION.search(title))


def _item_source(item: Any) -> str:
    return str(getattr(item, "source", "text") or "text").lower()


def _zone(
    zones: Mapping[str, Any] | None, name: str
) -> tuple[float, float, float, float] | None:
    return _as_bbox((zones or {}).get(name))


def _reserved_boxes(
    zones: Mapping[str, Any] | None,
) -> list[tuple[float, float, float, float]]:
    boxes: list[tuple[float, float, float, float]] = []
    for name in _RESERVED_ZONES:
        box = _zone(zones, name)
        if box is not None:
            boxes.append(box)
    return boxes


def _in_reserved(
    x: float,
    y: float,
    reserved: Iterable[tuple[float, float, float, float]],
) -> bool:
    return any(_point_in_bbox(x, y, box) for box in reserved)


def _collect_hits(
    texts: Iterable[Any],
    reserved: Iterable[tuple[float, float, float, float]],
) -> list[_TitleHit]:
    hits: list[_TitleHit] = []
    for item in texts:
        if _item_source(item) not in _TITLE_SOURCES:
            continue
        text = _plain(str(getattr(item, "text", "") or ""))
        if not text or not is_view_title(text):
            continue
        x = float(getattr(item, "x", 0.0) or 0.0)
        y = float(getattr(item, "y", 0.0) or 0.0)
        if _in_reserved(x, y, reserved):
            continue
        hits.append(_TitleHit(text=text, x=x, y=y, bbox=_text_bbox(item)))
    return hits


def _cluster_hits(hits: list[_TitleHit]) -> list[list[_TitleHit]]:
    remaining = list(hits)
    clusters: list[list[_TitleHit]] = []
    while remaining:
        seed = remaining.pop(0)
        group = [seed]
        changed = True
        while changed:
            changed = False
            leftover: list[_TitleHit] = []
            for item in remaining:
                if any(
                    _nearby(member.bbox, item.bbox, _CLUSTER_GAP_MM) for member in group
                ):
                    group.append(item)
                    changed = True
                else:
                    leftover.append(item)
            remaining = leftover
        clusters.append(group)
    return clusters


def _cluster_title(group: list[_TitleHit]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for item in sorted(group, key=lambda hit: (-hit.y, hit.x, hit.text)):
        if item.text in seen:
            continue
        seen.add(item.text)
        parts.append(item.text)
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class _Anchor:
    title: str
    bbox: tuple[float, float, float, float]
    cx: float
    cy: float


def _anchors_from_clusters(clusters: list[list[_TitleHit]]) -> list[_Anchor]:
    anchors: list[_Anchor] = []
    for group in clusters:
        box = _union_bbox(item.bbox for item in group)
        if box is None:
            continue
        title = _cluster_title(group)
        if not title:
            continue
        anchors.append(
            _Anchor(
                title=title,
                bbox=box,
                cx=(box[0] + box[2]) / 2,
                cy=(box[1] + box[3]) / 2,
            )
        )
    return anchors


def _clip_one(
    box: tuple[float, float, float, float],
    hole: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if not _intersects(box, hole):
        return box
    cx = (box[0] + box[2]) / 2
    cy = (box[1] + box[3]) / 2
    hx = (hole[0] + hole[2]) / 2
    hy = (hole[1] + hole[3]) / 2
    overlap_w = min(box[2], hole[2]) - max(box[0], hole[0])
    overlap_h = min(box[3], hole[3]) - max(box[1], hole[1])
    if overlap_w <= 0 or overlap_h <= 0:
        return box
    dx, dy = hx - cx, hy - cy
    if abs(dx) * overlap_h >= abs(dy) * overlap_w:
        if dx >= 0:
            clipped = (box[0], box[1], min(box[2], hole[0]), box[3])
        else:
            clipped = (max(box[0], hole[2]), box[1], box[2], box[3])
    else:
        if dy >= 0:
            clipped = (box[0], box[1], box[2], min(box[3], hole[1]))
        else:
            clipped = (box[0], max(box[1], hole[3]), box[2], box[3])
    if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
        return box
    return clipped


def _usable_field(
    field: tuple[float, float, float, float] | None,
    reserved: Iterable[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    if field is None:
        return None
    box = field
    for hole in reserved:
        if hole == field:
            continue
        box = _clip_one(box, hole)
    if box[2] <= box[0] or box[3] <= box[1]:
        return field
    return box


def _group_columns(anchors: list[_Anchor]) -> list[list[_Anchor]]:
    ordered = sorted(anchors, key=lambda item: item.cx)
    groups: list[list[_Anchor]] = []
    for item in ordered:
        if groups and item.cx - groups[-1][-1].cx <= _COLUMN_GAP_MM:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def _column_caption_above(
    column: list[_Anchor],
    usable: tuple[float, float, float, float],
) -> bool:
    """Two+ titles whose top edge hugs the field top → view is below the title.

    IGR 2×2 puts the caption ~60 mm under the sheet edge; GOST stacked cuts
    sit farther down, so 80 mm still leaves KR1 caption-below alone.
    """

    if len(column) < 2:
        return False
    top = max(item.bbox[3] for item in column)
    return top >= usable[3] - _CAPTION_ABOVE_SLACK_MM


def _scene_box(
    anchor: _Anchor,
    column: list[_Anchor],
    usable: tuple[float, float, float, float],
    column_x0: float,
    column_x1: float,
    *,
    caption_above: bool = False,
) -> tuple[float, float, float, float]:
    """Caption-below (GOST): the view sits above the title, toward +Y.

    Caption-above (IGR 2×2): the title is at the top of the cell; the view
    sits below it, toward −Y.
    """

    stacked = sorted(column, key=lambda item: item.cy)
    index = stacked.index(anchor)
    if caption_above:
        y1 = usable[3] if index + 1 == len(stacked) else anchor.bbox[3]
        y0 = usable[1] if index == 0 else stacked[index - 1].bbox[3]
    else:
        y0 = usable[1] if index == 0 else anchor.bbox[1]
        y1 = (
            usable[3]
            if index + 1 == len(stacked)
            else stacked[index + 1].bbox[1]
        )
    y0 = min(y0, anchor.bbox[1])
    y1 = max(y1, anchor.bbox[3])
    if y1 < y0:
        y0, y1 = y1, y0
    return (
        min(column_x0, anchor.bbox[0]),
        y0,
        max(column_x1, anchor.bbox[2]),
        y1,
    )


def _column_x_bounds(
    groups: list[list[_Anchor]],
    index: int,
    usable: tuple[float, float, float, float],
) -> tuple[float, float]:
    centers = [sum(item.cx for item in group) / len(group) for group in groups]
    x0 = usable[0] if index == 0 else (centers[index - 1] + centers[index]) / 2
    x1 = (
        usable[2]
        if index + 1 == len(groups)
        else (centers[index] + centers[index + 1]) / 2
    )
    return x0, x1


def _scene_sort_key(scene: SheetScene) -> tuple[int, float, float, str]:
    scheme = 0 if _SCHEME.search(scene.title) else 1
    return (scheme, -scene.bbox[3], scene.bbox[0], scene.title)


def _instance_xy(instance: SymbolInstance) -> tuple[float, float]:
    return (float(instance.position.x), float(instance.position.y))


def _is_field_soil(instance: SymbolInstance) -> bool:
    if instance.role == "legend_exemplar":
        return False
    return parse_circle_block_name(instance.block_name) is not None


def _is_field_axis(instance: SymbolInstance) -> bool:
    return instance.role == "specification_mark" and is_axis_insert(instance)


def _ids_in_scene(
    instances: Iterable[SymbolInstance],
    box: tuple[float, float, float, float],
    reserved: Iterable[tuple[float, float, float, float]],
    predicate: Callable[[SymbolInstance], bool],
    sort_key: Callable[[SymbolInstance], tuple],
) -> tuple[str, ...]:
    found: list[SymbolInstance] = []
    reserved_boxes = list(reserved)
    for instance in instances:
        if not predicate(instance):
            continue
        x, y = _instance_xy(instance)
        if _in_reserved(x, y, reserved_boxes):
            continue
        if not _point_in_bbox(x, y, box):
            continue
        found.append(instance)
    found.sort(key=sort_key)
    return tuple(item.id for item in found)


def _bind_scene_members(
    result: PageResult,
    scenes: list[SheetScene],
    reserved: Iterable[tuple[float, float, float, float]],
) -> list[SheetScene]:
    """Attach field кругN and axis marks. Roles stay as they are."""

    reserved_boxes = list(reserved)
    bound: list[SheetScene] = []
    for scene in scenes:
        soil_ids = ()
        if not _is_well_scheme_title(scene.title):
            soil_ids = _ids_in_scene(
                result.symbol_instances,
                scene.bbox,
                reserved_boxes,
                _is_field_soil,
                lambda item: (-item.position.y, item.position.x, item.id),
            )
        axis_ids = _ids_in_scene(
            result.symbol_instances,
            scene.bbox,
            reserved_boxes,
            _is_field_axis,
            lambda item: (item.position.x, item.position.y, item.id),
        )
        bound.append(
            SheetScene(
                id=scene.id,
                page=scene.page,
                title=scene.title,
                bbox=scene.bbox,
                soil_ids=soil_ids,
                axis_ids=axis_ids,
            )
        )
    return bound


def _drop_relationship_placeholder(result: PageResult) -> None:
    if not result.sheet_scenes:
        return
    result.anomaly_codes = [
        code for code in result.anomaly_codes if code != _RELATIONSHIP_PLACEHOLDER
    ]


def collect_sheet_scenes(result: PageResult, texts: Any = None) -> list[SheetScene]:
    """Paper-bbox scenes from view titles. Empty when there is no anchor."""

    zones = result.sheet_zones
    if not zones:
        return []
    reserved = _reserved_boxes(zones)
    hits = _collect_hits(_as_texts(texts), reserved)
    if not hits:
        return []
    anchors = _anchors_from_clusters(_cluster_hits(hits))
    if not anchors:
        return []
    field = _zone(zones, "drawing_field")
    usable = _usable_field(field, reserved)
    columns = _group_columns(anchors)
    scenes: list[SheetScene] = []
    for column_index, column in enumerate(columns):
        if usable is None:
            x0 = min(item.bbox[0] for item in column)
            x1 = max(item.bbox[2] for item in column)
            column_usable = (
                x0,
                min(item.bbox[1] for item in column),
                x1,
                max(item.bbox[3] for item in column),
            )
        else:
            x0, x1 = _column_x_bounds(columns, column_index, usable)
            column_usable = usable
        caption_above = (
            usable is not None and _column_caption_above(column, column_usable)
        )
        for anchor in column:
            box = (
                _scene_box(
                    anchor,
                    column,
                    column_usable,
                    x0,
                    x1,
                    caption_above=caption_above,
                )
                if usable is not None
                else anchor.bbox
            )
            union = _union_bbox((box, anchor.bbox))
            box = union if union is not None else box
            for hole in reserved:
                box = _clip_one(box, hole)
            if box[2] <= box[0] or box[3] <= box[1]:
                box = anchor.bbox
            scenes.append(
                SheetScene(
                    id=stable_id(
                        "SC",
                        result.document_id,
                        result.page,
                        round(anchor.cx, 2),
                        round(anchor.cy, 2),
                        anchor.title,
                    ),
                    page=result.page,
                    title=anchor.title,
                    bbox=box,
                )
            )
    scenes.sort(key=_scene_sort_key)
    return _bind_scene_members(result, scenes, reserved)


def attach_sheet_scenes(result: PageResult, texts: Any = None) -> PageResult:
    """Fill ``sheetScenes``. No title-block zones or no heading → empty list."""

    result.sheet_scenes = collect_sheet_scenes(result, texts)
    _drop_relationship_placeholder(result)
    return result
