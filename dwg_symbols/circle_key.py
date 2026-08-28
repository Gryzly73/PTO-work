"""Parse geology block names ``кругN`` into circled numbers for display.

Used by the HTML click card and the IDEAL markdown legend. Does not change
INSERT roles and does not invent GOST «ИГЭ-N» when that text is not in the
legend ``label``.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

_CIRCLE_BLOCK = re.compile(r"^круг(\d+)$")
_CIRCLED_ONE = 0x2460
_CIRCLED_MAX = 20


def parse_circle_block_name(name: str | None) -> int | None:
    """Return N from ``кругN``, or None if the block is not a numbered circle."""

    if not name:
        return None
    match = _CIRCLE_BLOCK.fullmatch(str(name).strip())
    if not match:
        return None
    number = int(match.group(1))
    if number < 1 or number > _CIRCLED_MAX:
        return None
    return number


def circled_number(number: int) -> str:
    """Unicode circled digit ①…⑳. Caller must pass a value from the parser."""

    if number < 1 or number > _CIRCLED_MAX:
        raise ValueError(f"circled number {number} is outside 1–{_CIRCLED_MAX}")
    return chr(_CIRCLED_ONE + number - 1)


def circle_heading(block_name: str | None, label: str) -> str:
    """HTML card title: ``⑦ — {label}``, or the label alone if there is no ``кругN``."""

    number = parse_circle_block_name(block_name)
    if number is None:
        return label
    return f"{circled_number(number)} — {label}"


def legend_circle_number(
    entry: Mapping[str, Any],
    instances: Iterable[Mapping[str, Any]],
    bindings: Iterable[Mapping[str, Any]],
) -> int | None:
    """Number of the ``кругN`` block joined to this legend line, if unique.

    Join is a binding to the entry or a matching ``referenceSignatures`` value.
    Ambiguous or missing joins stay empty (fail-closed).
    """

    entry_id = str(entry.get("id") or "")
    if not entry_id:
        return None
    ref_sigs = {str(item) for item in (entry.get("referenceSignatures") or []) if item}
    bound_ids = {
        str(item.get("instanceId") or "")
        for item in bindings
        if str(item.get("legendEntryId") or "") == entry_id
    }
    found: set[int] = set()
    for instance in instances:
        number = parse_circle_block_name(instance.get("blockName"))
        if number is None:
            continue
        instance_id = str(instance.get("id") or "")
        signature = str(instance.get("signature") or "")
        if instance_id in bound_ids or (signature and signature in ref_sigs):
            found.add(number)
    if len(found) != 1:
        return None
    return next(iter(found))


def legend_display_line(
    entry: Mapping[str, Any],
    instances: Iterable[Mapping[str, Any]] = (),
    bindings: Iterable[Mapping[str, Any]] = (),
) -> str:
    """Legend bullet text: ``⑦ {label}`` when a circle joins, else the label as-is."""

    label = str(entry.get("label") or "").strip()
    if not label:
        return ""
    number = legend_circle_number(entry, instances, bindings)
    if number is None:
        return label
    return f"{circled_number(number)} {label}"
