"""Read axis letter and digit from an already classified specification mark.

Furniture only marks «this is an axis». This module fills the grafa: INSERT
attributes ``Ось`` / ``Ось'`` first (classified by value, not tag), then unique
nearby TEXT. Fail-closed: an empty ``Ось`` is not «there is no axis»; extra
markers and room numbers are not the axis name; no INSERT is invented for
TEXT-only plans.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from stamp import Item, _norm

from .furniture import (
    AXIS_ATTRIBUTE_REASON,
    AXIS_LAYER_REASON,
    ROOM_NUMBER_BLOCK_REASON,
)
from .schema import PageResult, SymbolInstance, symbol_instance_from_dict


_INDEXED_TAG = re.compile(r"\(\d+\)$")
_LETTER_RE = re.compile(r"^[A-ZА-ЯЁ]{1,2}$")
_DIGIT_RE = re.compile(r"^\d{1,3}$")
_QUOTES = "′'`’"
_TEXT_RADIUS_MM = 8.0

_NOTE_EMPTY_LETTER = "буква не прочитана"
_NOTE_EMPTY_DIGIT = "цифра не прочитана"
_NOTE_MISSING = "ось не прочитана"
_NOTE_AMBIGUOUS = "несколько подписей рядом"
_NOTE_UNSORTED = "буква и цифра не разошлись по полям"


@dataclass(frozen=True, slots=True)
class AxisRead:
    letter: str = ""
    digit: str = ""
    source: str = ""
    note: str = ""

    @property
    def label(self) -> str:
        parts = [part for part in (self.letter, self.digit) if part]
        return " / ".join(parts)

    def as_dict(self) -> dict[str, str]:
        return {
            "letter": self.letter,
            "digit": self.digit,
            "label": self.label,
            "source": self.source,
            "note": self.note,
        }


def _folded(text: str) -> str:
    return (text or "").strip().casefold().replace("ё", "е")


def _fold_tag(tag: str) -> str:
    return _norm(_INDEXED_TAG.sub("", tag)).rstrip(_QUOTES)


def _is_axis_value_tag(tag: str) -> bool:
    return _fold_tag(tag) == "ось"


def looks_like_axis_letter(text: str) -> bool:
    value = (text or "").strip().upper().replace("Ё", "Е")
    return bool(_LETTER_RE.match(value))


def looks_like_axis_digit(text: str) -> bool:
    return bool(_DIGIT_RE.match((text or "").strip()))


def _classify_value(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if looks_like_axis_digit(raw):
        return "digit", raw
    if looks_like_axis_letter(raw):
        return "letter", raw.upper().replace("Ё", "Е")
    return "", ""


def is_axis_insert(
    instance: SymbolInstance | None = None,
    *,
    layer: str = "",
    reason: str = "",
    block_name: str = "",
    attributes: Mapping[str, str] | None = None,
) -> bool:
    """Whether this INSERT is an axis mark that may carry letter/digit."""

    if instance is not None:
        layer = instance.layer
        reason = instance.classification_reason or reason
        block_name = instance.block_name or block_name
        attributes = instance.attributes
    reason = reason or ""
    name = _folded(block_name)
    if reason == ROOM_NUMBER_BLOCK_REASON or name == "номерация" or name.startswith(
        "номерация "
    ):
        return False
    if reason in {AXIS_LAYER_REASON, AXIS_ATTRIBUTE_REASON}:
        return True
    if _folded(layer) == "osi":
        return True
    return any(_is_axis_value_tag(tag) for tag in (attributes or {}))


def _attribute_axis(attributes: Mapping[str, str]) -> tuple[str, str, str, bool]:
    """Return (letter, digit, note, had_empty_slot)."""

    letter = ""
    digit = ""
    empty_slot = False
    kinds: list[str] = []
    for tag, raw in attributes.items():
        if not _is_axis_value_tag(tag):
            continue
        value = str(raw or "").strip()
        if not value:
            empty_slot = True
            continue
        kind, parsed = _classify_value(value)
        if not kind:
            continue
        kinds.append(kind)
        if kind == "letter" and not letter:
            letter = parsed
        elif kind == "digit" and not digit:
            digit = parsed
    if kinds.count("letter") > 1 or kinds.count("digit") > 1:
        return "", "", _NOTE_UNSORTED, empty_slot
    if letter and digit:
        return letter, digit, "", empty_slot
    if not letter and not digit:
        return "", "", _NOTE_MISSING, empty_slot
    notes: list[str] = []
    if not letter:
        notes.append(_NOTE_EMPTY_LETTER)
    if not digit:
        notes.append(_NOTE_EMPTY_DIGIT)
    return letter, digit, "; ".join(notes), empty_slot


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


def _nearby_slot(
    instance: SymbolInstance,
    slot: str,
    texts: list[Item],
    claimed: set[int],
) -> tuple[str, str]:
    x, y = instance.position.x, instance.position.y
    hits: list[tuple[float, int, str]] = []
    for index, item in enumerate(texts):
        if index in claimed:
            continue
        kind, parsed = _classify_value(item.text)
        if kind != slot:
            continue
        dist = ((item.x - x) ** 2 + (item.y - y) ** 2) ** 0.5
        if dist > _TEXT_RADIUS_MM:
            continue
        hits.append((dist, index, parsed))
    if not hits:
        return "", ""
    hits.sort()
    best_dist, best_index, best_value = hits[0]
    close = [value for dist, _, value in hits if dist <= best_dist + 0.2]
    if len(set(close)) > 1:
        return "", _NOTE_AMBIGUOUS
    claimed.add(best_index)
    return best_value, ""


def read_axis(
    instance: SymbolInstance,
    texts: Iterable[Item] | None = None,
    *,
    claimed: set[int] | None = None,
) -> AxisRead | None:
    """Fill letter/digit for one axis INSERT, or None when it is not an axis."""

    if not is_axis_insert(instance):
        return None
    letter, digit, note, _empty = _attribute_axis(instance.attributes)
    source = "attributes" if (letter or digit) else ""
    items = list(texts) if texts is not None else None
    used = claimed if claimed is not None else set()
    text_note = ""
    if items is not None:
        if not letter:
            letter, text_note = _nearby_slot(instance, "letter", items, used)
            if letter:
                source = "merged" if source == "attributes" else "text"
        if not digit:
            extra, extra_note = _nearby_slot(instance, "digit", items, used)
            if extra:
                digit = extra
                source = "merged" if source == "attributes" else "text"
            text_note = text_note or extra_note
    if letter and digit:
        return AxisRead(letter=letter, digit=digit, source=source or "attributes", note="")
    if not letter and not digit:
        return AxisRead(source="", note=text_note or note or _NOTE_MISSING)
    combined = "; ".join(part for part in (note, text_note) if part)
    return AxisRead(letter=letter, digit=digit, source=source, note=combined)


def attach_axes(result: PageResult, texts: Any = None) -> PageResult:
    """Write ``axis`` on axis INSERTs after furniture has classified them."""

    items = _items_from_texts(texts)
    claimed: set[int] = set()
    updated: list[SymbolInstance] = []
    for instance in result.symbol_instances:
        parsed = read_axis(instance, items, claimed=claimed)
        if parsed is None:
            updated.append(instance)
            continue
        updated.append(replace(instance, axis=parsed.as_dict()))
    result.symbol_instances = updated
    return result


def axis_from_mapping(payload: Mapping[str, Any]) -> dict[str, str] | None:
    """HTML fallback: stored dict, or attributes on an old sidecar."""

    stored = payload.get("axis")
    if isinstance(stored, Mapping) and (
        str(stored.get("letter") or "").strip()
        or str(stored.get("digit") or "").strip()
        or str(stored.get("note") or "").strip()
    ):
        letter = str(stored.get("letter") or "")
        digit = str(stored.get("digit") or "")
        label = str(stored.get("label") or "").strip() or " / ".join(
            part for part in (letter, digit) if part
        )
        return {
            "letter": letter,
            "digit": digit,
            "label": label,
            "source": str(stored.get("source") or ""),
            "note": str(stored.get("note") or ""),
        }
    if not is_axis_insert(
        layer=str(payload.get("layer") or ""),
        reason=str(payload.get("classificationReason") or ""),
        block_name=str(payload.get("blockName") or ""),
        attributes={
            str(key): str(value)
            for key, value in dict(payload.get("attributes") or {}).items()
        },
    ):
        return None
    attrs = {
        str(key): str(value)
        for key, value in dict(payload.get("attributes") or {}).items()
    }
    letter, digit, note, _empty = _attribute_axis(attrs)
    if not letter and not digit and not note:
        note = _NOTE_MISSING
    label = " / ".join(part for part in (letter, digit) if part)
    return {
        "letter": letter,
        "digit": digit,
        "label": label,
        "source": "attributes" if (letter or digit) else "",
        "note": note,
    }


def recount_axis_attrs(root: str | Path) -> dict[str, Any]:
    """Count readable axis letter/digit from sidecar INSERT attributes."""

    pages = 0
    eligible = 0
    with_letter = 0
    with_digit = 0
    with_both = 0
    notes: Counter[str] = Counter()
    layers: Counter[str] = Counter()
    for path in sorted(Path(root).rglob("symbol_instances.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        page = int(payload.get("page") or 1)
        pages += 1
        for item in payload.get("items") or []:
            instance = symbol_instance_from_dict(item, page=page)
            parsed = read_axis(instance)
            if parsed is None:
                continue
            eligible += 1
            layers[instance.layer] += 1
            if parsed.letter:
                with_letter += 1
            if parsed.digit:
                with_digit += 1
            if parsed.letter and parsed.digit:
                with_both += 1
            if parsed.note:
                notes[parsed.note] += 1
    return {
        "pages": pages,
        "eligible": eligible,
        "withLetter": with_letter,
        "withDigit": with_digit,
        "withBoth": with_both,
        "layers": dict(layers),
        "notes": dict(notes),
        "note": (
            "Sidecar attributes only. Nearby TEXT is applied on review-sheet, "
            "not in this recount. Letter/digit are classified by value, not "
            "by whether the tag is Ось or Ось'."
        ),
    }
