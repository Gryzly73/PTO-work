"""Standalone sheet TEXT that is not an INSERT: axis letter/digit and linear size.

Furniture and axis/dimension readers only see blocks. On a typical plan the
axis circles and many chain numbers are TEXT / DIMENSION. This module lifts a
narrow subset of those into ``textLabels`` — not fake INSERT, not a dump of
every string on the sheet.

Fail-closed: ATTRIB stays with the INSERT; stamp zone and NADPISI are skipped;
letter vs digit vs number is classified by value; OSI vs RAZMER vs DIMENSION
decides the kind. Nearby INSERT that already holds the same grafa consumes the
text so geology ``Ж / 7`` is not duplicated.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .axis_read import looks_like_axis_digit, looks_like_axis_letter
from .dimension_read import looks_like_linear
from .schema import PageResult, SymbolInstance, TextLabel, stable_id


_TEXT_RADIUS_MM = 8.0
_SKIP_SOURCES = frozenset({"attrib", "leader"})
_SKIP_LAYERS = frozenset(
    {"format", "nadpisi", "подписи", "stamp", "рамка", "otmetki"}
)
_AXIS_LAYERS = frozenset({"osi", "оси"})
_LINEAR_LAYERS = frozenset({"razmer"})
_LABEL_SOURCES = frozenset({"text", "mtext", "dimension"})
_NOTE_UNPAIRED_DIGIT = "цифра без буквы рядом"
_NOTE_UNPAIRED_LETTER = "буква без цифры рядом"


def _folded(text: str) -> str:
    return (text or "").strip().casefold().replace("ё", "е")


def _in_bbox(x: float, y: float, bbox: Iterable[float] | None) -> bool:
    if bbox is None:
        return False
    x0, y0, x1, y1 = tuple(bbox)
    return x0 <= x <= x1 and y0 <= y <= y1


def _distance(ax: float, ay: float, bx: float, by: float) -> float:
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _text_bbox(x: float, y: float, size: float) -> tuple[float, float, float, float]:
    half = max(float(size or 2.5), 2.5)
    return (x - half, y - half, x + half, y + half)


@dataclass(frozen=True, slots=True)
class _Raw:
    index: int
    x: float
    y: float
    size: float
    text: str
    source: str
    layer: str


def _raw_from_texts(texts: Any) -> list[_Raw]:
    if texts is None:
        return []
    items = list(getattr(texts, "texts", texts) or [])
    out: list[_Raw] = []
    for index, item in enumerate(items):
        if isinstance(item, Mapping):
            text = str(item.get("text") or "")
            source = str(item.get("source") or "text")
            layer = str(item.get("layer") or "")
            x = float(item.get("x") or 0.0)
            y = float(item.get("y") or 0.0)
            size = float(item.get("height") or item.get("size") or 2.5)
        else:
            text = str(getattr(item, "text", "") or "")
            source = str(getattr(item, "source", "text") or "text")
            layer = str(getattr(item, "layer", "") or "")
            x = float(getattr(item, "x", 0.0) or 0.0)
            y = float(getattr(item, "y", 0.0) or 0.0)
            size = float(getattr(item, "height", getattr(item, "size", 2.5)) or 2.5)
        out.append(_Raw(index, x, y, size, text.strip(), source.lower(), layer))
    return out


def _stamp_bbox(result: PageResult) -> tuple[float, float, float, float] | None:
    return _named_bbox(result, "title_block")


def _drawing_bbox(result: PageResult) -> tuple[float, float, float, float] | None:
    return _named_bbox(result, "drawing_field")


def _notes_bbox(result: PageResult) -> tuple[float, float, float, float] | None:
    return _named_bbox(result, "notes")


def _named_bbox(
    result: PageResult,
    name: str,
) -> tuple[float, float, float, float] | None:
    zones = result.sheet_zones or {}
    raw = zones.get(name)
    if raw is None:
        return None
    box = tuple(float(value) for value in raw)
    if len(box) != 4:
        return None
    return box  # type: ignore[return-value]


def _insert_owns(raw: _Raw, instance: SymbolInstance) -> bool:
    if _distance(raw.x, raw.y, instance.position.x, instance.position.y) > _TEXT_RADIUS_MM:
        return False
    axis = instance.axis or {}
    if raw.text and raw.text in {
        str(axis.get("letter") or ""),
        str(axis.get("digit") or ""),
        str(axis.get("label") or ""),
    }:
        return True
    dimension = instance.dimension or {}
    value = str(dimension.get("value") or "")
    if value and raw.text.replace(",", ".") == value.replace(",", "."):
        return True
    attrs = instance.attributes or {}
    return any(str(value).strip() == raw.text for value in attrs.values() if value)


def _owned_indices(result: PageResult, items: list[_Raw]) -> set[int]:
    owned: set[int] = set()
    for raw in items:
        for instance in result.symbol_instances:
            if _insert_owns(raw, instance):
                owned.add(raw.index)
                break
    return owned


def _eligible(
    raw: _Raw,
    stamp: tuple[float, float, float, float] | None,
    field: tuple[float, float, float, float] | None,
    notes: tuple[float, float, float, float] | None = None,
) -> bool:
    if raw.source in _SKIP_SOURCES or raw.source not in _LABEL_SOURCES:
        return False
    if _folded(raw.layer) in _SKIP_LAYERS:
        return False
    if stamp is not None and _in_bbox(raw.x, raw.y, stamp):
        return False
    if notes is not None and _in_bbox(raw.x, raw.y, notes):
        return False
    if field is not None and not _in_bbox(raw.x, raw.y, field):
        return False
    return True


def _axis_kind(raw: _Raw) -> str:
    if _folded(raw.layer) not in _AXIS_LAYERS:
        return ""
    if looks_like_axis_letter(raw.text):
        return "letter"
    if looks_like_axis_digit(raw.text):
        return "digit"
    return ""


def _is_linear(raw: _Raw) -> bool:
    if not looks_like_linear(raw.text):
        return False
    layer = _folded(raw.layer)
    if layer in _LINEAR_LAYERS:
        return True
    return raw.source == "dimension" and layer not in _AXIS_LAYERS


def _pair_axes(candidates: list[tuple[_Raw, str]], page: int) -> list[TextLabel]:
    letters = [(raw, kind) for raw, kind in candidates if kind == "letter"]
    digits = [(raw, kind) for raw, kind in candidates if kind == "digit"]
    used: set[int] = set()
    labels: list[TextLabel] = []
    for raw, _kind in letters:
        hits = [
            other
            for other, _slot in digits
            if other.index not in used
            and _distance(raw.x, raw.y, other.x, other.y) <= _TEXT_RADIUS_MM
        ]
        if len(hits) != 1:
            continue
        digit = hits[0]
        close_letters = [
            other
            for other, _slot in letters
            if other.index != raw.index
            and _distance(digit.x, digit.y, other.x, other.y) <= _TEXT_RADIUS_MM
        ]
        if close_letters:
            continue
        used.add(raw.index)
        used.add(digit.index)
        letter = raw.text.strip().upper().replace("Ё", "Е")
        labels.append(
            _label(
                raw,
                page,
                kind="axis",
                text=f"{letter} / {digit.text.strip()}",
                letter=letter,
                digit=digit.text.strip(),
                x=(raw.x + digit.x) / 2,
                y=(raw.y + digit.y) / 2,
            )
        )
    for raw, kind in candidates:
        if raw.index in used:
            continue
        if kind == "letter":
            letter = raw.text.strip().upper().replace("Ё", "Е")
            labels.append(
                _label(
                    raw,
                    page,
                    kind="axis_letter",
                    text=letter,
                    letter=letter,
                    note=_NOTE_UNPAIRED_LETTER,
                )
            )
        else:
            labels.append(
                _label(
                    raw,
                    page,
                    kind="axis_digit",
                    text=raw.text.strip(),
                    digit=raw.text.strip(),
                    note=_NOTE_UNPAIRED_DIGIT,
                )
            )
    return labels


def _label(
    raw: _Raw,
    page: int,
    *,
    kind: str,
    text: str,
    letter: str = "",
    digit: str = "",
    value: str = "",
    note: str = "",
    x: float | None = None,
    y: float | None = None,
) -> TextLabel:
    px = raw.x if x is None else x
    py = raw.y if y is None else y
    return TextLabel(
        id=stable_id("TL", raw.index, kind, round(px, 3), round(py, 3), text),
        page=page,
        kind=kind,
        text=text,
        letter=letter,
        digit=digit,
        value=value,
        source=raw.source,
        layer=raw.layer,
        x=px,
        y=py,
        bbox=_text_bbox(px, py, raw.size),
        note=note,
    )


def collect_text_labels(
    result: PageResult,
    texts: Any = None,
) -> list[TextLabel]:
    """Axis/linear TEXT that no INSERT already owns. Empty when nothing proven."""

    items = _raw_from_texts(texts)
    if not items:
        return []
    stamp = _stamp_bbox(result)
    field = _drawing_bbox(result)
    notes = _notes_bbox(result)
    owned = _owned_indices(result, items)
    axis_hits: list[tuple[_Raw, str]] = []
    linear_hits: list[_Raw] = []
    for raw in items:
        if raw.index in owned or not _eligible(raw, stamp, field, notes):
            continue
        slot = _axis_kind(raw)
        if slot:
            axis_hits.append((raw, slot))
            continue
        if _is_linear(raw):
            linear_hits.append(raw)
    labels = _pair_axes(axis_hits, result.page)
    for raw in linear_hits:
        value = raw.text.replace(",", ".").strip()
        labels.append(
            _label(raw, result.page, kind="linear", text=value, value=value)
        )
    labels.sort(key=lambda item: (item.kind, item.y, item.x, item.text))
    return labels


def attach_text_labels(result: PageResult, texts: Any = None) -> PageResult:
    """Write ``textLabels`` after INSERT grafa have claimed nearby TEXT."""

    result.text_labels = collect_text_labels(result, texts)
    return result


def dump_sheet_texts(drawing: str | Path, page: int) -> dict[str, Any]:
    """Inventory TEXT on one sheet, then classify. Needs DWG conversion."""

    from dwg_sheets import sheets_for

    from .blocks import extract_block_page
    from .furniture import classify_sheet_furniture
    from .title_block import attach_title_block
    from .dimension_read import attach_dimensions
    from .axis_read import attach_axes

    source = Path(drawing)
    _, sheets = sheets_for(source)
    sheet = sheets[page - 1]
    result = classify_sheet_furniture(extract_block_page(source, page))
    result = attach_title_block(
        result,
        sheet.texts,
        sheet_bbox=(0.0, 0.0, sheet.paper_width, sheet.paper_height)
        if sheet.paper_width and sheet.paper_height
        else None,
    )
    result = attach_dimensions(result, sheet.texts)
    result = attach_axes(result, sheet.texts)
    result = attach_text_labels(result, sheet.texts)
    from .schedule_join import attach_schedule_notes

    result = attach_schedule_notes(result)
    items = _raw_from_texts(sheet.texts)
    layers: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    axis_like = 0
    linear_like = 0
    for raw in items:
        layers[raw.layer or "(empty)"] += 1
        sources[raw.source or "(empty)"] += 1
        if looks_like_axis_letter(raw.text) or looks_like_axis_digit(raw.text):
            axis_like += 1
        if looks_like_linear(raw.text):
            linear_like += 1
    return {
        "documentPath": str(source),
        "page": page,
        "texts": len(items),
        "byLayer": dict(sorted(layers.items(), key=lambda pair: (-pair[1], pair[0]))),
        "bySource": dict(sorted(sources.items())),
        "axisLikeByValue": axis_like,
        "linearLikeByValue": linear_like,
        "labels": [item.to_dict() for item in result.text_labels],
        "labelKinds": dict(
            sorted(Counter(item.kind for item in result.text_labels).items())
        ),
        "note": (
            "labels require OSI/ОСИ for axes and RAZMER or DIMENSION for linear sizes. "
            "ATTRIB and stamp-zone TEXT are skipped. INSERT-owned grafa are not duplicated."
        ),
    }
