"""Join specification-mark codes to a real kit table row, or leave an honest note.

Axes and room numbers are already on the mark shelf. This module does not invent
an explikation or axis scheme. A click may say «ось А — …» / «помещение …»
only when the code uniquely matches a row the caller actually supplied. Empty
rows (live sheets without a customer table) stamp «таблица не найдена». An empty
axis grafa stays «буква не прочитана», never «оси нет». Soil, stamp, furniture
and linear TEXT are not codes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any, Iterable, Mapping

from .axis_read import is_axis_insert
from .furniture import ROOM_NUMBER_BLOCK_REASON
from .schema import PageResult, SymbolInstance


NOTE_TABLE_MISSING = "таблица не найдена"
NOTE_ROW_MISSING = "строка таблицы не найдена"
NOTE_TABLE_CONFLICT = "конфликт строк таблицы"

_SCHEDULE_KINDS = frozenset({"axis", "room"})
_AXIS_TEXT_KINDS = frozenset({"axis", "axis_letter", "axis_digit"})


def _folded(text: str) -> str:
    return (text or "").strip().casefold().replace("ё", "е")


def _append_note(*parts: str) -> str:
    seen: list[str] = []
    for part in parts:
        text = (part or "").strip()
        if not text or text in seen:
            continue
        seen.append(text)
    return "; ".join(seen)


def room_number_code(block_name: str | None) -> str:
    """«номерация 5» → «5». Empty when the block is not a numbered room mark."""

    name = (block_name or "").strip()
    folded = _folded(name)
    if folded != "номерация" and not folded.startswith("номерация "):
        return ""
    rest = name.split(None, 1)
    return rest[1].strip() if len(rest) > 1 else ""


def is_room_number_insert(instance: SymbolInstance) -> bool:
    reason = instance.classification_reason or ""
    if reason == ROOM_NUMBER_BLOCK_REASON:
        return True
    return bool(room_number_code(instance.block_name)) and instance.role == (
        "specification_mark"
    )


def _index_rows(
    rows: Iterable[Mapping[str, Any]] | None,
) -> tuple[bool, dict[tuple[str, str], tuple[str, str]]]:
    """Return (table_present, {(kind, folded_code): (table|conflict, label)})."""

    items = list(rows or [])
    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in items:
        kind = _folded(str(row.get("kind") or ""))
        code = _folded(str(row.get("code") or ""))
        label = str(row.get("label") or "").strip()
        if kind not in _SCHEDULE_KINDS or not code or not label:
            continue
        buckets[(kind, code)].append(label)
    index: dict[tuple[str, str], tuple[str, str]] = {}
    for key, labels in buckets.items():
        unique = list(dict.fromkeys(labels))
        if len(unique) == 1:
            index[key] = ("table", unique[0])
        else:
            index[key] = ("conflict", "")
    return bool(items), index


def _lookup(
    index: dict[tuple[str, str], tuple[str, str]],
    *,
    table_present: bool,
    kind: str,
    codes: tuple[str, ...],
) -> tuple[str, str, str]:
    """Return (expansion_label, source, note). Prefer the first supplied code."""

    for raw in codes:
        code = (raw or "").strip()
        if not code:
            continue
        hit = index.get((kind, _folded(code)))
        if hit is None:
            continue
        status, row_label = hit
        if status == "conflict":
            return "", "conflict", NOTE_TABLE_CONFLICT
        if kind == "axis":
            expansion = f"ось {code} — {row_label}"
        else:
            expansion = f"помещение {code} — {row_label}"
        return expansion, "table", ""
    if table_present:
        return "", "missing", NOTE_ROW_MISSING
    return "", "missing", NOTE_TABLE_MISSING


def _schedule_payload(
    *,
    kind: str,
    code: str,
    label: str,
    source: str,
    note: str,
) -> dict[str, str]:
    return {
        "kind": kind,
        "code": code,
        "label": label,
        "source": source,
        "note": note,
    }


def attach_schedule_notes(
    result: PageResult,
    rows: Iterable[Mapping[str, Any]] | None = None,
) -> PageResult:
    """Stamp a table join or an honest missing-table note on codes only.

    ``rows`` is a kit table the caller already has. The live pipeline passes
    nothing: we do not scrape or invent a schedule from the DWG.
    """

    table_present, index = _index_rows(rows)
    updated: list[SymbolInstance] = []
    for instance in result.symbol_instances:
        if is_axis_insert(instance) and instance.axis:
            axis = dict(instance.axis)
            letter = str(axis.get("letter") or "").strip()
            digit = str(axis.get("digit") or "").strip()
            expansion, source, sched_note = _lookup(
                index,
                table_present=table_present,
                kind="axis",
                codes=(letter, digit),
            )
            axis["note"] = _append_note(axis.get("note") or "", sched_note)
            updated.append(
                replace(
                    instance,
                    axis=axis,
                    schedule=_schedule_payload(
                        kind="axis",
                        code=letter or digit,
                        label=expansion,
                        source=source,
                        note=sched_note,
                    ),
                )
            )
            continue
        if is_room_number_insert(instance):
            code = room_number_code(instance.block_name)
            expansion, source, sched_note = _lookup(
                index,
                table_present=table_present,
                kind="room",
                codes=(code,),
            )
            updated.append(
                replace(
                    instance,
                    schedule=_schedule_payload(
                        kind="room",
                        code=code,
                        label=expansion,
                        source=source,
                        note=sched_note,
                    ),
                )
            )
            continue
        updated.append(instance)

    labels = []
    for label in result.text_labels:
        if label.kind not in _AXIS_TEXT_KINDS:
            labels.append(label)
            continue
        expansion, _source, sched_note = _lookup(
            index,
            table_present=table_present,
            kind="axis",
            codes=(label.letter, label.digit),
        )
        labels.append(
            replace(
                label,
                note=_append_note(expansion, label.note, sched_note),
            )
        )

    result.symbol_instances = updated
    result.text_labels = labels
    return result
