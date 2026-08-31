"""Read GOST 21.101 title-block fields from a sheet that already knows its stamp.

Furniture classification only *recognizes* the INSERT (FORMAT/STAMP attributes).
This module fills the grafa: sheet / sheets-total / stage from block attributes,
code and title from ``stamp.read`` on sheet texts. The signature block is
furniture, not the title block.

Fail-closed: an empty ``ШИФР`` attribute does not mean «there is no code»; a
value that fails ``_looks_like_code`` is ignored; a filled attribute that
disagrees with geometry is kept (explicit block field) and noted.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from stamp import (
    Item,
    Stamp,
    _anchors,
    _clean_number,
    _looks_like_code,
    _next_anchor,
    _norm,
    read as read_stamp,
)

from .furniture import (
    FORMAT_STAMP_REASON,
    SIGNATURE_BLOCK_REASON,
    STAMP_ATTRIBUTES_REASON,
    furniture_reason,
)
from .schema import PageResult, SymbolInstance, symbol_instance_from_dict


# ГОСТ 21.101 form 3. Same window as furniture crops when the INSERT has no bbox.
STAMP_WIDTH_MM = 185.0
STAMP_HEIGHT_MM = 55.0

TITLE_BLOCK_REASONS = frozenset({FORMAT_STAMP_REASON, STAMP_ATTRIBUTES_REASON})

_INDEXED_TAG = re.compile(r"\(\d+\)$")
_ATTR_FIELDS = {
    "лист": "sheet",
    "листов": "sheets_total",
    "стадия": "stage",
    "шифр": "code",
}
_CONFLICT_LABELS = {
    "sheet": "листа",
    "sheets_total": "листов",
    "stage": "стадии",
    "code": "шифра",
}
_NOTE_MISSING = "надпись не найдена"
_NOTE_NO_CODE = "шифр не прочитан"


@dataclass(slots=True)
class TitleBlock:
    """Merged title-block grafa. Empty string — the field was not read."""

    code: str = ""
    section: str = ""
    sheet: str = ""
    sheets_total: str = ""
    stage: str = ""
    title: str = ""
    object_name: str = ""
    org: str = ""
    source: str = ""  # attributes | geometry | merged
    note: str = ""
    instance_id: str | None = None
    bbox: tuple[float, float, float, float] | None = None

    @property
    def found(self) -> bool:
        return bool(
            self.code
            or self.sheet
            or self.title
            or self.stage
            or self.sheets_total
            or self.instance_id
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "section": self.section,
            "sheet": self.sheet,
            "sheetsTotal": self.sheets_total,
            "stage": self.stage,
            "title": self.title,
            "objectName": self.object_name,
            "org": self.org,
            "source": self.source,
            "note": self.note,
            "instanceId": self.instance_id,
        }


def is_title_block_insert(instance: SymbolInstance) -> bool:
    """Whether this INSERT is the main title block, not a visa or frame."""

    reason = instance.classification_reason or furniture_reason(instance)
    if reason == SIGNATURE_BLOCK_REASON:
        return False
    return reason in TITLE_BLOCK_REASONS


def read_title_block(
    instances: Iterable[SymbolInstance] | PageResult | None,
    texts: Any = None,
    *,
    sheet_bbox: Iterable[float] | None = None,
) -> TitleBlock:
    """Merge INSERT attributes with ``stamp.read`` on sheet texts."""

    stamp_insert = _pick_stamp_insert(_as_instances(instances))
    items = _items_from_texts(texts)
    geometry = read_stamp(items, source="DWG") if items else Stamp(source="DWG")
    anchors = _anchors(items)
    stage_anchor = _next_anchor(anchors, "стадия")
    sheet_anchor = _next_anchor(anchors, "лист")
    total_anchor = _next_anchor(anchors, "листов")
    has_anchors = stage_anchor is not None or sheet_anchor is not None
    attr_fields = _attribute_fields(stamp_insert.attributes if stamp_insert else {})

    merged, attr_used, geom_used, conflicts = _merge_fields(attr_fields, geometry)
    note = _compose_note(
        conflicts=conflicts,
        has_insert=stamp_insert is not None,
        has_anchors=has_anchors,
        geometry=geometry,
        merged=merged,
    )
    source = _merge_source(attr_used, geom_used)
    scale = _stamp_scale(stage_anchor, total_anchor)
    frame = _normalize_bbox(sheet_bbox)
    bbox = _title_block_bbox(
        stamp_insert,
        stage_anchor=stage_anchor,
        sheet_anchor=sheet_anchor,
        total_anchor=total_anchor,
        scale=scale,
        sheet_bbox=frame,
    )
    return TitleBlock(
        code=merged["code"],
        section=Stamp(code=merged["code"]).section,
        sheet=merged["sheet"],
        sheets_total=merged["sheets_total"],
        stage=merged["stage"],
        title=merged["title"],
        object_name=merged["object_name"],
        org=merged["org"],
        source=source,
        note=note,
        instance_id=stamp_insert.id if stamp_insert is not None else None,
        bbox=bbox,
    )


def sheet_zones(
    title_block: TitleBlock | None,
    sheet_bbox: Iterable[float] | None,
) -> dict[str, list[float]] | None:
    """Paper-mm zones: title block and the drawing field with the stamp cut out.

    Legend and notes windows (and the notes body) are attached later from
    already found rows and headings. This function does not recut ``drawing_field``.
    """

    stamp = title_block.bbox if title_block is not None else None
    frame = _normalize_bbox(sheet_bbox)
    if stamp is None:
        return None
    zones = {"title_block": list(stamp)}
    if frame is not None:
        zones["drawing_field"] = list(_drawing_field(frame, stamp))
    return zones


def attach_title_block(
    result: PageResult,
    texts: Any = None,
    *,
    sheet_bbox: Iterable[float] | None = None,
) -> PageResult:
    """Fill ``titleBlock`` / ``sheetZones`` after furniture has recognized the stamp.

    Call once on the PageResult that will be written. Furniture may run twice
    (extract, then review); the grafa are read from the classified INSERTs and
    sheet texts, not from the furniture pass itself. Optional ``legend`` /
    ``notes`` windows and ``notesTexts`` are attached from already found rows
    and headings; view titles become ``sheetScenes``. ``drawing_field`` is not
    recut.
    """

    title = read_title_block(result, texts, sheet_bbox=sheet_bbox)
    result.title_block = title.as_dict()
    result.sheet_zones = sheet_zones(title, sheet_bbox)
    from .legends import attach_legend_notes_zones
    from .sheet_scenes import attach_sheet_scenes

    result = attach_legend_notes_zones(result, texts)
    return attach_sheet_scenes(result, texts)


def recount_title_block_attrs(root: str | Path) -> dict[str, Any]:
    """Count title-block grafa from sidecar INSERT attributes, without DWG texts.

    Full ``stamp.read`` needs sheet TEXT; that happens only on the 10-sheet
    review. This recount never invents a cipher from an empty ``ШИФР``.
    """

    pages = 0
    with_insert = 0
    reasons: Counter[str] = Counter()
    sheet_filled = 0
    stage_filled = 0
    sheets_total_filled = 0
    code_filled = 0
    cipher_empty = 0
    cipher_missing = 0
    cipher_looks_like = 0
    cipher_rejected = 0
    signature_blocks = 0
    pages_signature_not_stamp = 0
    pages_no_title_block = 0
    for path in sorted(Path(root).rglob("symbol_instances.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        page = int(payload.get("page") or 1)
        instances = [
            symbol_instance_from_dict(item, page=page)
            for item in payload.get("items") or []
        ]
        pages += 1
        stamp = _pick_stamp_insert(instances)
        signatures = [
            item
            for item in instances
            if (item.classification_reason or furniture_reason(item))
            == SIGNATURE_BLOCK_REASON
        ]
        signature_blocks += len(signatures)
        if stamp is None:
            pages_no_title_block += 1
            if signatures:
                pages_signature_not_stamp += 1
            continue
        with_insert += 1
        reason = stamp.classification_reason or furniture_reason(stamp)
        if reason:
            reasons[reason] += 1
        fields = _attribute_fields(stamp.attributes)
        if fields.get("sheet"):
            sheet_filled += 1
        if fields.get("stage"):
            stage_filled += 1
        if fields.get("sheets_total"):
            sheets_total_filled += 1
        if fields.get("code"):
            code_filled += 1
        raw_code = _raw_attribute(stamp.attributes, "шифр")
        if raw_code is None:
            cipher_missing += 1
        elif not raw_code.strip():
            cipher_empty += 1
        elif _looks_like_code(raw_code):
            cipher_looks_like += 1
        else:
            cipher_rejected += 1
    return {
        "pages": pages,
        "withTitleBlockInsert": with_insert,
        "withoutTitleBlockInsert": pages_no_title_block,
        "reasons": dict(sorted(reasons.items())),
        "grafa": {
            "sheetFilled": sheet_filled,
            "stageFilled": stage_filled,
            "sheetsTotalFilled": sheets_total_filled,
            "codeFilled": code_filled,
        },
        "cipher": {
            "empty": cipher_empty,
            "missingTag": cipher_missing,
            "looksLikeCode": cipher_looks_like,
            "rejected": cipher_rejected,
        },
        "signatureBlocks": signature_blocks,
        "pagesWithSignatureNotStamp": pages_signature_not_stamp,
        "note": (
            "Attributes only; empty ШИФР is not «no code». "
            "stamp.read needs sheet texts (10-sheet review)."
        ),
    }


def _raw_attribute(attributes: Mapping[str, str], field: str) -> str | None:
    """Return the raw tag value, including empty; None if the tag is absent."""

    for tag, raw in attributes.items():
        key = _norm(_INDEXED_TAG.sub("", tag))
        if key == field:
            return str(raw or "")
    return None


def _as_instances(
    source: Iterable[SymbolInstance] | PageResult | None,
) -> tuple[SymbolInstance, ...]:
    if source is None:
        return ()
    if isinstance(source, PageResult):
        return tuple(source.symbol_instances)
    return tuple(source)


def _pick_stamp_insert(
    instances: Iterable[SymbolInstance],
) -> SymbolInstance | None:
    candidates = [item for item in instances if is_title_block_insert(item)]
    if not candidates:
        return None

    def _key(item: SymbolInstance) -> tuple[int, float]:
        reason = item.classification_reason or furniture_reason(item)
        format_first = 1 if reason == FORMAT_STAMP_REASON else 0
        return (format_first, item.position.x - item.position.y)

    return max(candidates, key=_key)


def _items_from_texts(texts: Any) -> list[Item]:
    if texts is None:
        return []
    raw = list(getattr(texts, "texts", texts) or [])
    items: list[Item] = []
    for item in raw:
        if isinstance(item, Item):
            if item.text and item.text.strip():
                items.append(item)
            continue
        text = str(getattr(item, "text", "") or "")
        if not text.strip():
            continue
        items.append(
            Item(
                x=float(getattr(item, "x", 0.0) or 0.0),
                y=float(getattr(item, "y", 0.0) or 0.0),
                size=float(
                    getattr(item, "height", 0.0) or getattr(item, "size", 0.0) or 0.0
                ),
                text=text,
            )
        )
    return items


def _attribute_fields(attributes: Mapping[str, str]) -> dict[str, str]:
    """Layer 1: explicit grafa on the INSERT. Empty values are skipped."""

    found: dict[str, str] = {}
    for tag, raw in attributes.items():
        value = str(raw or "").strip()
        if not value:
            continue
        key = _norm(_INDEXED_TAG.sub("", tag))
        field = _ATTR_FIELDS.get(key)
        if field is None:
            continue
        if field == "code":
            # Empty ШИФР never reaches here. A non-empty value that is not a
            # document code (surname, «Формат А1», title) is not a code either.
            if not _looks_like_code(value):
                continue
            found[field] = value
            continue
        if field in {"sheet", "sheets_total"}:
            found[field] = _clean_number(value)
        else:
            found[field] = value
    return found


def _merge_fields(
    attr_fields: Mapping[str, str],
    geometry: Stamp,
) -> tuple[dict[str, str], set[str], set[str], list[str]]:
    geom_fields = {
        "code": geometry.code.strip(),
        "sheet": geometry.sheet.strip(),
        "sheets_total": geometry.sheets_total.strip(),
        "stage": geometry.stage.strip(),
        "title": geometry.title.strip(),
        "object_name": geometry.object_name.strip(),
        "org": geometry.org.strip(),
    }
    merged = {
        "code": "",
        "sheet": "",
        "sheets_total": "",
        "stage": "",
        "title": geom_fields["title"],
        "object_name": geom_fields["object_name"],
        "org": geom_fields["org"],
    }
    attr_used: set[str] = set()
    geom_used: set[str] = set()
    conflicts: list[str] = []
    for field in ("sheet", "sheets_total", "stage", "code"):
        attr_value = attr_fields.get(field, "")
        geom_value = geom_fields[field]
        if attr_value and geom_value and not _same_value(field, attr_value, geom_value):
            conflicts.append(
                f"конфликт {_CONFLICT_LABELS[field]}: атрибут {attr_value}, "
                f"геометрия {geom_value}; принят атрибут"
            )
        if attr_value:
            merged[field] = attr_value
            attr_used.add(field)
        elif geom_value:
            merged[field] = geom_value
            geom_used.add(field)
    for field in ("title", "object_name", "org"):
        if merged[field]:
            geom_used.add(field)
    return merged, attr_used, geom_used, conflicts


def _same_value(field: str, left: str, right: str) -> bool:
    if field in {"sheet", "sheets_total"}:
        return _clean_number(left) == _clean_number(right)
    return left.strip().casefold().replace("ё", "е") == right.strip().casefold().replace(
        "ё", "е"
    )


def _merge_source(attr_used: set[str], geom_used: set[str]) -> str:
    if attr_used and geom_used:
        return "merged"
    if attr_used:
        return "attributes"
    if geom_used:
        return "geometry"
    return ""


def _compose_note(
    *,
    conflicts: list[str],
    has_insert: bool,
    has_anchors: bool,
    geometry: Stamp,
    merged: Mapping[str, str],
) -> str:
    parts = list(conflicts)
    evidence = has_insert or has_anchors or geometry.found or bool(geometry.stage)
    if not evidence and not any(merged.values()):
        parts.append(_NOTE_MISSING)
    elif not merged["code"]:
        parts.append(_NOTE_NO_CODE)
    return "; ".join(parts)


def _stamp_scale(stage: Item | None, total: Item | None) -> float:
    if stage is None or total is None:
        return 1.0
    span = abs(total.x - stage.x)
    if span <= 1:
        return 1.0
    return span / 35.0


def _title_block_bbox(
    stamp_insert: SymbolInstance | None,
    *,
    stage_anchor: Item | None,
    sheet_anchor: Item | None,
    total_anchor: Item | None,
    scale: float,
    sheet_bbox: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    if stamp_insert is not None:
        return _zone_from_insert(stamp_insert, sheet_bbox)
    anchors = [item for item in (stage_anchor, sheet_anchor, total_anchor) if item]
    if not anchors:
        return None
    return _zone_from_anchors(anchors, scale=scale, sheet_bbox=sheet_bbox)


def _zone_from_insert(
    instance: SymbolInstance,
    sheet_bbox: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float]:
    if instance.bbox is not None:
        width = instance.bbox[2] - instance.bbox[0]
        height = instance.bbox[3] - instance.bbox[1]
        # Real stamp geometry, not the 6 mm insertion point some files store.
        if width >= 50.0 and height >= 20.0:
            return instance.bbox
    return _place_stamp(instance.position.x, instance.position.y, sheet_bbox, scale=1.0)


def _zone_from_anchors(
    anchors: list[Item],
    *,
    scale: float,
    sheet_bbox: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float]:
    """185×55 mm from the top-right grafa, not from an INSERT point.

    «Стадия / Лист / Листов» sit in the top-right of form 3. The block extends
    left and down from them; treating the label as an insertion point would
    push the zone off the sheet.
    """

    width = STAMP_WIDTH_MM * scale
    height = STAMP_HEIGHT_MM * scale
    right = max(item.x for item in anchors) + 10.0 * scale
    top = max(item.y for item in anchors) + 5.0 * scale
    placed = (right - width, top - height, right, top)
    if sheet_bbox is None:
        return placed
    return _fit_on_sheet(placed, sheet_bbox)


def _place_stamp(
    x: float,
    y: float,
    sheet_bbox: tuple[float, float, float, float] | None,
    *,
    scale: float,
) -> tuple[float, float, float, float]:
    width = STAMP_WIDTH_MM * scale
    height = STAMP_HEIGHT_MM * scale
    candidates = (
        (x, y, x + width, y + height),
        (x - width, y, x, y + height),
        (x, y - height, x + width, y),
        (x - width, y - height, x, y),
    )
    # No sheet: keep the stamp in the positive quadrant (origin bottom-left).
    frame = sheet_bbox or (
        0.0,
        0.0,
        max(x, width),
        max(y + height, height),
    )
    placed = max(candidates, key=lambda box: _intersection_area(box, frame))
    if sheet_bbox is None:
        return placed
    return _fit_on_sheet(placed, sheet_bbox)


def _drawing_field(
    sheet: tuple[float, float, float, float],
    stamp: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Largest axis-aligned remainder of the sheet after cutting out the stamp."""

    sx0, sy0, sx1, sy1 = sheet
    tx0, ty0, tx1, ty1 = (
        max(sx0, stamp[0]),
        max(sy0, stamp[1]),
        min(sx1, stamp[2]),
        min(sy1, stamp[3]),
    )
    remainders = (
        (sx0, ty1, sx1, sy1),  # above
        (sx0, sy0, sx1, ty0),  # below
        (sx0, sy0, tx0, sy1),  # left
        (tx1, sy0, sx1, sy1),  # right
    )
    valid = [box for box in remainders if box[2] > box[0] and box[3] > box[1]]
    if not valid:
        return sheet
    return max(valid, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))


def _normalize_bbox(
    value: Iterable[float] | None,
) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    bbox = tuple(float(item) for item in value)
    if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    return bbox  # type: ignore[return-value]


def _fit_on_sheet(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Translate a 185×55 window onto the sheet; clip only if it does not fit."""

    x0, y0, x1, y1 = inner
    width, height = x1 - x0, y1 - y0
    if x0 < outer[0]:
        x1 += outer[0] - x0
        x0 = outer[0]
    if y0 < outer[1]:
        y1 += outer[1] - y0
        y0 = outer[1]
    if x1 > outer[2]:
        x0 -= x1 - outer[2]
        x1 = outer[2]
    if y1 > outer[3]:
        y0 -= y1 - outer[3]
        y1 = outer[3]
    fitted = (x0, y0, x1, y1)
    if (
        fitted[2] > fitted[0]
        and fitted[3] > fitted[1]
        and abs((fitted[2] - fitted[0]) - width) < 1e-6
        and abs((fitted[3] - fitted[1]) - height) < 1e-6
    ):
        return fitted
    clipped = (
        max(outer[0], inner[0]),
        max(outer[1], inner[1]),
        min(outer[2], inner[2]),
        min(outer[3], inner[3]),
    )
    if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
        return inner
    return clipped


def _intersection_area(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    x0 = max(first[0], second[0])
    y0 = max(first[1], second[1])
    x1 = min(first[2], second[2])
    y1 = min(first[3], second[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)
