"""Fail-closed filters for non-legend INSERTs: furniture, objects, annotations, marks.

Runs after H2 INSERT inventory and before H4 legend matching. Instances stay in
the sidecar; only role and status change. Classification requires reproducible
proof (stamp attributes, named block+layer, or a narrow service layer). Geology
borehole marks (CPE/PR_* / Skv/Probe) stay field candidates: a unique legend
cell INSERT is H4, otherwise an explicit «нет стыка» note — never a guessed
abbreviation. Specification marks (axes, room numbers) are identifiers for a
separate schedule, not legend symbols and not drawing objects. Welding stays
unknown unless the block signature is in ``gost_welds.json`` with a GOST 2.312
code (not the layer ``SVARKA`` as a whole). Slope (уклон) and building codes
(ЗД-*) stay unknown.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Any, Iterable

from stamp import _ANCHOR_WORDS, _SERVICE_STARTS, _norm

from .gost_welds import WELD_GOST_REASON, weld_reason
from .schema import (
    PageResult,
    SymbolInstance,
    UnknownSymbolCluster,
    stable_id,
    symbol_instance_from_dict,
)


FORMAT_STAMP_REASON = "FORMAT_STAMP_ATTRIBUTES"
STAMP_ATTRIBUTES_REASON = "STAMP_ATTRIBUTES"
PAPER_FRAME_REASON = "PAPER_FRAME_COVERAGE"
SIGNATURE_BLOCK_REASON = "SIGNATURE_BLOCK"
COLUMN_BLOCK_LAYER_REASON = "COLUMN_BLOCK_LAYER"
TRAP_BLOCK_LAYER_REASON = "TRAP_BLOCK_LAYER"
FURNITURE_BLOCK_LAYER_REASON = "FURNITURE_BLOCK_LAYER"
GEOLOGY_NO_JOIN_REASON = "GEOLOGY_NO_LEGEND_JOIN"
ANONYMOUS_CONSTRUCTION_LAYER_REASON = "ANONYMOUS_CONSTRUCTION_LAYER"
SERVICE_LAYER_REASON = "SERVICE_LAYER"
AXIS_LAYER_REASON = "AXIS_LAYER"
AXIS_ATTRIBUTE_REASON = "AXIS_ATTRIBUTE"
ROOM_NUMBER_BLOCK_REASON = "ROOM_NUMBER_BLOCK"
HEIGHT_MARK_BLOCK_REASON = "HEIGHT_MARK_BLOCK"
BUILDING_LABEL_BLOCK_REASON = "BUILDING_LABEL_BLOCK"
FILTER_ANOMALY = "SHEET_FURNITURE_FILTER_APPLIED"
OBJECT_FILTER_ANOMALY = "DRAWING_OBJECT_FILTER_APPLIED"
ANNOTATION_FILTER_ANOMALY = "DRAWING_ANNOTATION_FILTER_APPLIED"
MARK_FILTER_ANOMALY = "SPECIFICATION_MARK_FILTER_APPLIED"
FRAME_COVERAGE_THRESHOLD = 0.85
STAMP_TOKEN_MINIMUM = 2
AXIS_LAYERS = frozenset({"osi"})
AXIS_ATTRIBUTE_KEYS = frozenset({"ось", "ось'"})
SERVICE_LAYERS = frozenset({"razmer", "otmetki", "nadpisi", "подписи"})
GEOLOGY_LAYERS = frozenset({"skv", "probe"})
GEOLOGY_BLOCKS = frozenset(
    {"cpe", "cce", "cme", "cge", "pr_kv", "pr_tr", "pr_vd"}
)
# Inventory lock (575 sidecars): трап* exists only on Технология (3×трап100).
# Furniture/plumbing: exact name+layer pairs from the catalog, not a layer dump.
# Layer 0 (поручень / МОЙКА KEWANDE) is not a pair. Construction: remainder-top
# layers that are repeating anonymous geometry; SVARKA / ARMATURA / METIZ /
# layer 0 excluded.
TRAP_LAYERS = frozenset({"технология"})
_FURNITURE_PAIRS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^индивидуальное рабочее место\b"), "ар_мебель"),
    (re.compile(r"^m_bath_basin_basin - rect_p$"), "ар_сантех"),
    (re.compile(r"^m_bath_basin_basin - oval_p$"), "santeh"),
    (re.compile(r"^uni_taz_2_top$"), "ар_сантех"),
    (re.compile(r"^m_chair-task"), "i-furn"),
    (re.compile(r"^ms_38$"), "ар_мебель"),
    (re.compile(r"^moyka-kui$"), "santehnika"),
)
CONSTRUCTION_LAYERS = frozenset(
    {
        "металл фахверк",
        "стойки",
        "fachverk",
        "metall",
        "metal",
        "fundament",
        "фундамент",
        "beton",
    }
)
_TRAP_NAME = re.compile(r"^трап(?:$|\d|\s)")

_OPEN_STATUSES = {"unresolved", "unclassified"}
_INDEXED_TAG = re.compile(r"\(\d+\)$")
_EXPLICIT_STAMP_TOKENS = {
    "должн",
    "должность",
    "формат",
    "гип",
    "нконтр",
    "лист",
    "листов",
    "стадия",
    "инв",
    "колуч",
    "изм",
    "шифр",
}
_STAMP_TOKENS = frozenset(
    {
        *_ANCHOR_WORDS.keys(),
        *_ANCHOR_WORDS.values(),
        *_SERVICE_STARTS,
        *_EXPLICIT_STAMP_TOKENS,
    }
)
_PREFIX_TOKENS = tuple(token for token in sorted(_STAMP_TOKENS) if len(token) >= 4)


def _is_open_candidate(instance: SymbolInstance) -> bool:
    return instance.role == "field_candidate" and instance.status in _OPEN_STATUSES


def _is_format_layer(instance: SymbolInstance) -> bool:
    return instance.layer.strip().casefold() == "format"


def _is_stamp_token(text: str) -> bool:
    normalized = _norm(text)
    if not normalized:
        return False
    if normalized in _STAMP_TOKENS:
        return True
    return any(normalized.startswith(token) for token in _PREFIX_TOKENS)


def _folded(value: str | None) -> str:
    return (value or "").strip().casefold()


def _stamp_attribute_count(instance: SymbolInstance) -> int:
    count = 0
    for tag, value in instance.attributes.items():
        if _is_stamp_token(_INDEXED_TAG.sub("", tag)) or _is_stamp_token(value):
            count += 1
    return count


def _has_stamp_attributes(instance: SymbolInstance) -> bool:
    return _stamp_attribute_count(instance) >= 1


def _is_signature_block(instance: SymbolInstance) -> bool:
    return _folded(instance.block_name) == "подпись" and _folded(instance.layer) == "подписи"


def _is_column(instance: SymbolInstance) -> bool:
    return _folded(instance.block_name) == "колонна" and _folded(instance.layer) == "колонна"


def _is_anonymous_name(name: str | None) -> bool:
    folded = _folded(name)
    return not folded or folded.startswith("*u") or folded.startswith("a$")


def _is_trap(instance: SymbolInstance) -> bool:
    return (
        _folded(instance.layer) in TRAP_LAYERS
        and _TRAP_NAME.match(_folded(instance.block_name)) is not None
    )


def _is_anonymous_construction(instance: SymbolInstance) -> bool:
    return (
        _folded(instance.layer) in CONSTRUCTION_LAYERS
        and _is_anonymous_name(instance.block_name)
    )


def _is_named_furniture(instance: SymbolInstance) -> bool:
    name = _folded(instance.block_name)
    layer = _folded(instance.layer)
    if not name:
        return False
    return any(
        pattern.match(name) and layer == expected
        for pattern, expected in _FURNITURE_PAIRS
    )


def object_reason(instance: SymbolInstance) -> str | None:
    """Return a drawing-object reason, or None when name+layer proof is missing."""

    if _is_column(instance):
        return COLUMN_BLOCK_LAYER_REASON
    if _is_trap(instance):
        return TRAP_BLOCK_LAYER_REASON
    if _is_named_furniture(instance):
        return FURNITURE_BLOCK_LAYER_REASON
    if _is_anonymous_construction(instance):
        return ANONYMOUS_CONSTRUCTION_LAYER_REASON
    return None


def _is_service_layer(instance: SymbolInstance) -> bool:
    return _folded(instance.layer) in SERVICE_LAYERS


def _is_axis_layer(instance: SymbolInstance) -> bool:
    return _folded(instance.layer) in AXIS_LAYERS


def _has_axis_attribute(instance: SymbolInstance) -> bool:
    return any(_folded(key) in AXIS_ATTRIBUTE_KEYS for key in instance.attributes)


def _is_layer_zero(instance: SymbolInstance) -> bool:
    return instance.layer.strip() == "0"


def _block_named(instance: SymbolInstance, prefix: str) -> bool:
    name = _folded(instance.block_name)
    return name == prefix or name.startswith(f"{prefix} ")


def mark_reason(instance: SymbolInstance) -> str | None:
    """Return a specification-mark reason, or None when evidence is insufficient."""

    if _is_axis_layer(instance):
        return AXIS_LAYER_REASON
    if _has_axis_attribute(instance):
        return AXIS_ATTRIBUTE_REASON
    if not _is_layer_zero(instance):
        return None
    if _block_named(instance, "номерация"):
        return ROOM_NUMBER_BLOCK_REASON
    return None


def label_reason(instance: SymbolInstance) -> str | None:
    """Return a layer-0 callout reason, or None when the name is insufficient."""

    if not _is_layer_zero(instance):
        return None
    if _block_named(instance, "высоты"):
        return HEIGHT_MARK_BLOCK_REASON
    if _block_named(instance, "новый абк"):
        return BUILDING_LABEL_BLOCK_REASON
    return None


def _is_geology_mark(instance: SymbolInstance) -> bool:
    block = _folded(instance.block_name)
    layer = _folded(instance.layer)
    return (
        layer in GEOLOGY_LAYERS
        or block in GEOLOGY_BLOCKS
        or block.startswith("pr_")
    )


def _bbox_area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))


def _normalize_sheet_bbox(
    sheet_bbox: Iterable[float] | None,
) -> tuple[float, float, float, float] | None:
    if sheet_bbox is None:
        return None
    bbox = tuple(float(value) for value in sheet_bbox)
    if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    return bbox  # type: ignore[return-value]


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


def _covers_sheet_frame(
    instance: SymbolInstance,
    sheet_bbox: tuple[float, float, float, float] | None,
) -> bool:
    if (
        instance.bbox is None
        or sheet_bbox is None
        or instance.position.space != "paper"
    ):
        return False
    sheet_area = _bbox_area(sheet_bbox)
    if sheet_area <= 0:
        return False
    return (
        _intersection_area(instance.bbox, sheet_bbox) / sheet_area
        >= FRAME_COVERAGE_THRESHOLD
    )


def furniture_reason(
    instance: SymbolInstance,
    sheet_bbox: Iterable[float] | None = None,
) -> str | None:
    """Return a furniture reason, or None when evidence is insufficient."""

    if _is_format_layer(instance) and _has_stamp_attributes(instance):
        return FORMAT_STAMP_REASON
    if _stamp_attribute_count(instance) >= STAMP_TOKEN_MINIMUM:
        return STAMP_ATTRIBUTES_REASON
    if _covers_sheet_frame(instance, _normalize_sheet_bbox(sheet_bbox)):
        return PAPER_FRAME_REASON
    if _is_signature_block(instance):
        return SIGNATURE_BLOCK_REASON
    return None


def _as_ignored(
    instance: SymbolInstance,
    role: str,
    reason: str,
) -> SymbolInstance:
    return replace(
        instance,
        status="ignored",
        role=role,
        legend_entry_id=None,
        classification_reason=reason,
    )


def classify_instance(
    instance: SymbolInstance,
    sheet_bbox: Iterable[float] | None = None,
) -> SymbolInstance:
    """Classify one open INSERT, or return it unchanged."""

    if not _is_open_candidate(instance):
        return instance
    reason = furniture_reason(instance, sheet_bbox)
    if reason is not None:
        return _as_ignored(instance, "sheet_furniture", reason)
    if _is_geology_mark(instance):
        if instance.classification_reason == GEOLOGY_NO_JOIN_REASON:
            return instance
        return replace(instance, classification_reason=GEOLOGY_NO_JOIN_REASON)
    reason = object_reason(instance)
    if reason is not None:
        return _as_ignored(instance, "drawing_object", reason)
    reason = weld_reason(instance)
    if reason is not None:
        return _as_ignored(instance, "drawing_annotation", reason)
    reason = mark_reason(instance)
    if reason is not None:
        return _as_ignored(instance, "specification_mark", reason)
    if _is_service_layer(instance):
        return _as_ignored(instance, "drawing_annotation", SERVICE_LAYER_REASON)
    reason = label_reason(instance)
    if reason is not None:
        return _as_ignored(instance, "drawing_annotation", reason)
    return instance


def _unknown_clusters(result: PageResult) -> list[UnknownSymbolCluster]:
    previous = {cluster.signature: cluster for cluster in result.unknown_symbols}
    grouped: dict[str, list[SymbolInstance]] = defaultdict(list)
    for instance in result.symbol_instances:
        if _is_open_candidate(instance):
            grouped[instance.signature].append(instance)
    clusters: list[UnknownSymbolCluster] = []
    for signature, items in sorted(grouped.items()):
        old = previous.get(signature)
        ids = {item.id for item in items}
        if old is not None and old.representative_instance_id in ids:
            representative = old.representative_instance_id
        else:
            representative = items[0].id
        clusters.append(
            UnknownSymbolCluster(
                id=(
                    old.id
                    if old is not None
                    else stable_id("US", result.document_id, result.page, signature)
                ),
                page=result.page,
                signature=signature,
                instance_ids=tuple(item.id for item in items),
                reason=(
                    GEOLOGY_NO_JOIN_REASON
                    if any(_is_geology_mark(item) for item in items)
                    else (old.reason if old is not None else "NO_LEGEND_BINDING_BASELINE")
                ),
                representative_instance_id=representative,
            )
        )
    return clusters


_ROLE_ANOMALIES = {
    "sheet_furniture": FILTER_ANOMALY,
    "drawing_object": OBJECT_FILTER_ANOMALY,
    "drawing_annotation": ANNOTATION_FILTER_ANOMALY,
    "specification_mark": MARK_FILTER_ANOMALY,
}


def classify_sheet_furniture(
    result: PageResult,
    sheet_bbox: Iterable[float] | None = None,
) -> PageResult:
    """Mark non-legend INSERTs as ignored furniture, objects, annotations or marks."""

    updated: list[SymbolInstance] = []
    applied_roles: set[str] = set()
    changed = False
    for instance in result.symbol_instances:
        classified = classify_instance(instance, sheet_bbox)
        updated.append(classified)
        if classified is not instance:
            changed = True
            applied_roles.add(classified.role)
    if not changed:
        return result
    result.symbol_instances = updated
    result.unknown_symbols = _unknown_clusters(result)
    anomalies = set(result.anomaly_codes)
    for role in applied_roles:
        code = _ROLE_ANOMALIES.get(role)
        if code:
            anomalies.add(code)
    result.anomaly_codes = sorted(anomalies)
    result.validate()
    return result


def annotate_geology_marks(result: PageResult) -> PageResult:
    """Stamp «нет стыка» on unresolved CPE/PR_* after H4. Does not invent a label."""

    geology_signatures: set[str] = set()
    updated: list[SymbolInstance] = []
    changed = False
    for instance in result.symbol_instances:
        if _is_geology_mark(instance) and _is_open_candidate(instance):
            geology_signatures.add(instance.signature)
            if instance.classification_reason != GEOLOGY_NO_JOIN_REASON:
                instance = replace(
                    instance, classification_reason=GEOLOGY_NO_JOIN_REASON
                )
                changed = True
        updated.append(instance)
    clusters: list[UnknownSymbolCluster] = []
    for cluster in result.unknown_symbols:
        if (
            cluster.signature in geology_signatures
            and cluster.reason != GEOLOGY_NO_JOIN_REASON
        ):
            cluster = replace(cluster, reason=GEOLOGY_NO_JOIN_REASON)
            changed = True
        clusters.append(cluster)
    if not changed:
        return result
    result.symbol_instances = updated
    result.unknown_symbols = clusters
    result.validate()
    return result


def _is_unknown_candidate(instance: SymbolInstance) -> bool:
    return instance.role == "field_candidate" and instance.status in _OPEN_STATUSES


def recount_non_symbols(root: str | Path) -> dict[str, Any]:
    """Reclassify existing symbol_instances.json files without converting DWG."""

    pages = 0
    before_roles: Counter[str] = Counter()
    after_roles: Counter[str] = Counter()
    before_unknown = 0
    after_unknown = 0
    reasons: Counter[str] = Counter()
    geology_unknown = 0
    confirmed = 0
    probable = 0
    for path in sorted(Path(root).rglob("symbol_instances.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        page = int(payload.get("page") or 1)
        instances = [
            symbol_instance_from_dict(item, page=page)
            for item in payload.get("items") or []
        ]
        pages += 1
        classified = [classify_instance(item) for item in instances]
        for original, updated in zip(instances, classified):
            before_roles[original.role] += 1
            after_roles[updated.role] += 1
            if _is_unknown_candidate(original):
                before_unknown += 1
            if _is_unknown_candidate(updated):
                after_unknown += 1
                if _is_geology_mark(updated):
                    geology_unknown += 1
            if updated.status == "confirmed":
                confirmed += 1
            elif updated.status == "probable":
                probable += 1
            if updated is not original and updated.classification_reason:
                reasons[updated.classification_reason] += 1
    return {
        "pages": pages,
        "confirmed": confirmed,
        "probable": probable,
        "unknownBefore": before_unknown,
        "unknownAfter": after_unknown,
        "unknownDelta": after_unknown - before_unknown,
        "rolesBefore": dict(sorted(before_roles.items())),
        "rolesAfter": dict(sorted(after_roles.items())),
        "reasons": dict(sorted(reasons.items())),
        "geologyUnknownAfter": geology_unknown,
    }
