"""Read a dimension/elevation number from an already classified annotation.

Furniture only marks «this is a size». This module fills the number: INSERT
attributes first, then a unique nearby TEXT. Fail-closed: an empty ``ОТМЕТКА``
is not «there is no mark»; visibility tags ``В``/``Г`` are not the size;
height marks stay a separate kind from the linear chain; ``уклон`` and
``NADPISI`` are not read.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from stamp import Item, _norm

from .furniture import BUILDING_LABEL_BLOCK_REASON, HEIGHT_MARK_BLOCK_REASON
from .schema import PageResult, SymbolInstance, symbol_instance_from_dict


_INDEXED_TAG = re.compile(r"\(\d+\)$")
_LEVEL_RE = re.compile(r"^[+\-]?\d+[.,]\d{1,4}$")
_LINEAR_RE = re.compile(r"^[+\-]?\d{2,6}(?:[.,]\d{1,3})?(?:\s*мм)?$", re.IGNORECASE)
_MM_SUFFIX = re.compile(r"\s*мм$", re.IGNORECASE)

_ELEVATION_TAGS = frozenset({"отметка"})
_LINEAR_TAGS = frozenset({"размер", "значение", "value", "длина"})
_IGNORE_TAGS = frozenset({"в", "г", "вг", "гг", "прим"})
_ELEVATION_LAYERS = frozenset({"otmetki"})
_LINEAR_LAYERS = frozenset({"razmer"})
_TEXT_RADIUS_MM = 8.0

_NOTE_EMPTY_LEVEL = "отметка не прочитана"
_NOTE_MISSING = "число не прочитано"
_NOTE_AMBIGUOUS = "несколько чисел рядом"


@dataclass(frozen=True, slots=True)
class DimensionRead:
    value: str = ""
    kind: str = "linear"
    source: str = ""
    note: str = ""
    tag: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "value": self.value,
            "kind": self.kind,
            "source": self.source,
            "note": self.note,
            "tag": self.tag,
        }


def _folded(text: str) -> str:
    return (text or "").strip().casefold().replace("ё", "е")


def _fold_tag(tag: str) -> str:
    return _norm(_INDEXED_TAG.sub("", tag))


def _normalize_number(text: str) -> str:
    value = _MM_SUFFIX.sub("", (text or "").strip()).replace(",", ".")
    return value.strip()


def looks_like_level(text: str) -> bool:
    return bool(_LEVEL_RE.match(_normalize_number(text)))


def looks_like_linear(text: str) -> bool:
    compact = _normalize_number(text)
    if not compact or "(" in (text or "") or ")" in (text or ""):
        return False
    return bool(_LINEAR_RE.match(compact))


def dimension_kind(
    instance: SymbolInstance | None = None,
    *,
    layer: str = "",
    reason: str = "",
    block_name: str = "",
) -> str | None:
    """Which number this INSERT may carry, or None when it is not a size."""

    if instance is not None:
        layer = instance.layer
        reason = instance.classification_reason or reason
        block_name = instance.block_name or block_name
    reason = reason or ""
    if reason == BUILDING_LABEL_BLOCK_REASON:
        return None
    if reason == HEIGHT_MARK_BLOCK_REASON:
        return "height"
    name = _folded(block_name)
    if name == "высоты" or name.startswith("высоты "):
        return "height"
    folded_layer = _folded(layer)
    if folded_layer in _ELEVATION_LAYERS:
        return "elevation"
    if folded_layer in _LINEAR_LAYERS:
        return "linear"
    return None


def _attribute_number(
    attributes: Mapping[str, str], kind: str
) -> tuple[str, str, str]:
    """Return (value, tag, note). Empty ОТМЕТКА is a note, not a miss of the key."""

    empty_level = False
    for tag, raw in attributes.items():
        folded = _fold_tag(tag)
        if folded in _IGNORE_TAGS:
            continue
        value = str(raw or "").strip()
        if folded in _ELEVATION_TAGS:
            if not value:
                empty_level = True
                continue
            if looks_like_level(value):
                return _normalize_number(value), tag, ""
            continue
        if not value:
            continue
        if folded in _LINEAR_TAGS and looks_like_linear(value) and kind != "elevation":
            return _normalize_number(value), tag, ""
    if empty_level:
        return "", "ОТМЕТКА", _NOTE_EMPTY_LEVEL
    return "", "", ""


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


def _text_looks_like(kind: str, text: str) -> bool:
    if kind == "elevation":
        return looks_like_level(text)
    return looks_like_linear(text) or (kind == "height" and looks_like_level(text))


def _nearby_number(
    instance: SymbolInstance,
    kind: str,
    texts: list[Item],
    claimed: set[int],
) -> tuple[str, str]:
    """Unique closest number-like TEXT. Returns (value, note)."""

    x, y = instance.position.x, instance.position.y
    hits: list[tuple[float, int, str]] = []
    for index, item in enumerate(texts):
        if index in claimed:
            continue
        if not _text_looks_like(kind, item.text):
            continue
        dist = ((item.x - x) ** 2 + (item.y - y) ** 2) ** 0.5
        if dist > _TEXT_RADIUS_MM:
            continue
        hits.append((dist, index, _normalize_number(item.text)))
    if not hits:
        return "", ""
    hits.sort()
    best_dist, best_index, best_value = hits[0]
    close = [value for dist, _, value in hits if dist <= best_dist + 0.2]
    if len(set(close)) > 1:
        return "", _NOTE_AMBIGUOUS
    claimed.add(best_index)
    return best_value, ""


def read_dimension(
    instance: SymbolInstance,
    texts: Iterable[Item] | None = None,
    *,
    claimed: set[int] | None = None,
) -> DimensionRead | None:
    """Fill the number for one classified size INSERT, or None if not a size."""

    kind = dimension_kind(instance)
    if kind is None:
        return None
    value, tag, note = _attribute_number(instance.attributes, kind)
    if value:
        return DimensionRead(
            value=value, kind=kind, source="attributes", note=note, tag=tag
        )
    text_value = ""
    text_note = ""
    if texts is not None:
        text_value, text_note = _nearby_number(
            instance, kind, list(texts), claimed if claimed is not None else set()
        )
    if text_value:
        return DimensionRead(
            value=text_value,
            kind=kind,
            source="text",
            note=note if note == _NOTE_EMPTY_LEVEL else text_note,
            tag=tag,
        )
    return DimensionRead(
        value="",
        kind=kind,
        source="",
        note=note or text_note or _NOTE_MISSING,
        tag=tag,
    )


def attach_dimensions(result: PageResult, texts: Any = None) -> PageResult:
    """Write ``dimension`` on size INSERTs after furniture has classified them."""

    items = _items_from_texts(texts)
    claimed: set[int] = set()
    updated: list[SymbolInstance] = []
    for instance in result.symbol_instances:
        parsed = read_dimension(instance, items, claimed=claimed)
        if parsed is None:
            updated.append(instance)
            continue
        updated.append(replace(instance, dimension=parsed.as_dict()))
    result.symbol_instances = updated
    return result


def dimension_from_mapping(payload: Mapping[str, Any]) -> dict[str, str] | None:
    """HTML fallback: already stored dict, or attributes on an old sidecar."""

    stored = payload.get("dimension")
    if isinstance(stored, Mapping) and (
        str(stored.get("value") or "").strip() or str(stored.get("note") or "").strip()
    ):
        return {
            "value": str(stored.get("value") or ""),
            "kind": str(stored.get("kind") or "linear"),
            "source": str(stored.get("source") or ""),
            "note": str(stored.get("note") or ""),
            "tag": str(stored.get("tag") or ""),
        }
    kind = dimension_kind(
        layer=str(payload.get("layer") or ""),
        reason=str(payload.get("classificationReason") or ""),
        block_name=str(payload.get("blockName") or ""),
    )
    if kind is None:
        return None
    attrs = {
        str(key): str(value)
        for key, value in dict(payload.get("attributes") or {}).items()
    }
    value, tag, note = _attribute_number(attrs, kind)
    if not value and not note:
        note = _NOTE_MISSING
    return {
        "value": value,
        "kind": kind,
        "source": "attributes" if value else "",
        "note": note,
        "tag": tag,
    }


def recount_dimension_attrs(root: str | Path) -> dict[str, Any]:
    """Count readable numbers from sidecar INSERT attributes, without DWG texts."""

    pages = 0
    eligible = 0
    filled = 0
    kinds: Counter[str] = Counter()
    layers: Counter[str] = Counter()
    notes: Counter[str] = Counter()
    empty_level = 0
    visibility_only = 0
    for path in sorted(Path(root).rglob("symbol_instances.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        page = int(payload.get("page") or 1)
        pages += 1
        for item in payload.get("items") or []:
            instance = symbol_instance_from_dict(item, page=page)
            parsed = read_dimension(instance)
            if parsed is None:
                continue
            eligible += 1
            layers[instance.layer] += 1
            kinds[parsed.kind] += 1
            if parsed.value:
                filled += 1
            if parsed.note:
                notes[parsed.note] += 1
            if parsed.note == _NOTE_EMPTY_LEVEL:
                empty_level += 1
            attrs = instance.attributes
            if (
                not parsed.value
                and attrs
                and all(_fold_tag(tag) in _IGNORE_TAGS for tag in attrs)
            ):
                visibility_only += 1
    return {
        "pages": pages,
        "eligible": eligible,
        "filledFromAttributes": filled,
        "emptyOtmetka": empty_level,
        "visibilityTagsOnly": visibility_only,
        "kinds": dict(kinds),
        "layers": dict(layers),
        "notes": dict(notes),
        "note": (
            "Sidecar attributes only. Nearby TEXT is applied on review-sheet, "
            "not in this recount."
        ),
    }
