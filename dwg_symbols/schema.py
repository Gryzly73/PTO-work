"""Versioned JSON-safe contracts for DWG symbol analysis."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from . import SCHEMA_VERSION


class SchemaError(ValueError):
    """Raised when a symbol sidecar violates the public contract."""


def stable_id(prefix: str, *identity: object) -> str:
    if not prefix or not prefix.replace("-", "").isalnum():
        raise SchemaError("prefix must be non-empty and alphanumeric")
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:16]}"


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{name} must be a non-empty string")
    return value


def _confidence(value: Any, name: str = "confidence") -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise SchemaError(f"{name} must be between 0 and 1")
    return float(value)


def _page(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchemaError("page must be a positive integer")
    return value


def _bbox(value: Iterable[float] | None) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    result = tuple(float(item) for item in value)
    if len(result) != 4:
        raise SchemaError("bbox must contain four coordinates")
    if not all(math.isfinite(item) for item in result):
        raise SchemaError("bbox coordinates must be finite")
    if result[0] > result[2] or result[1] > result[3]:
        raise SchemaError("bbox must satisfy x0 <= x1 and y0 <= y1")
    return result  # type: ignore[return-value]


_TITLE_BLOCK_STRINGS = (
    "code",
    "section",
    "sheet",
    "sheetsTotal",
    "stage",
    "title",
    "objectName",
    "org",
    "source",
    "note",
)
_TITLE_BLOCK_SOURCES = frozenset({"", "attributes", "geometry", "merged"})
_DIMENSION_KINDS = frozenset({"elevation", "linear", "height"})
_DIMENSION_SOURCES = frozenset({"", "attributes", "text"})
_DIMENSION_STRINGS = ("value", "kind", "source", "note", "tag")
_AXIS_SOURCES = frozenset({"", "attributes", "text", "merged"})
_AXIS_STRINGS = ("letter", "digit", "label", "source", "note")
_SCHEDULE_KINDS = frozenset({"axis", "room"})
_SCHEDULE_SOURCES = frozenset({"", "table", "missing", "conflict"})
_SCHEDULE_STRINGS = ("kind", "code", "label", "source", "note")
_TEXT_LABEL_KINDS = frozenset({"axis", "axis_letter", "axis_digit", "linear"})
_TEXT_LABEL_SOURCES = frozenset({"text", "mtext", "dimension"})
_NOTE_TEXT_SOURCES = frozenset({"text", "mtext"})
_FIELD_GEOMETRY_KINDS = frozenset({"line", "closed", "hatch"})
_FIELD_GEOMETRY_SOURCES = frozenset({"sheet_primitive"})
_TEXT_LABEL_STRINGS = (
    "kind",
    "text",
    "letter",
    "digit",
    "value",
    "source",
    "layer",
    "note",
)


def _title_block_dict(value: Any) -> dict[str, Any] | None:
    """Normalize optional titleBlock: Stamp.as_dict() plus instanceId."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SchemaError("titleBlock must be an object")
    source = str(value.get("source") or "")
    if source not in _TITLE_BLOCK_SOURCES:
        raise SchemaError(f"unsupported titleBlock.source {source!r}")
    instance_id = value.get("instanceId")
    if instance_id is not None:
        instance_id = _required(str(instance_id), "titleBlock.instanceId")
    payload = {key: str(value.get(key) or "") for key in _TITLE_BLOCK_STRINGS}
    payload["source"] = source
    payload["instanceId"] = instance_id
    return payload


def _dimension_dict(value: Any) -> dict[str, str] | None:
    """Normalize optional per-INSERT dimension grafa."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SchemaError("dimension must be an object")
    kind = str(value.get("kind") or "linear")
    source = str(value.get("source") or "")
    if kind not in _DIMENSION_KINDS:
        raise SchemaError(f"unsupported dimension.kind {kind!r}")
    if source not in _DIMENSION_SOURCES:
        raise SchemaError(f"unsupported dimension.source {source!r}")
    payload = {key: str(value.get(key) or "") for key in _DIMENSION_STRINGS}
    payload["kind"] = kind
    payload["source"] = source
    return payload


def _axis_dict(value: Any) -> dict[str, str] | None:
    """Normalize optional per-INSERT axis letter/digit grafa."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SchemaError("axis must be an object")
    source = str(value.get("source") or "")
    if source not in _AXIS_SOURCES:
        raise SchemaError(f"unsupported axis.source {source!r}")
    payload = {key: str(value.get(key) or "") for key in _AXIS_STRINGS}
    payload["source"] = source
    letter = payload["letter"]
    digit = payload["digit"]
    if not payload["label"]:
        payload["label"] = " / ".join(part for part in (letter, digit) if part)
    return payload


def _schedule_dict(value: Any) -> dict[str, str] | None:
    """Normalize optional kit-table join on a specification mark."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SchemaError("schedule must be an object")
    kind = str(value.get("kind") or "")
    source = str(value.get("source") or "")
    if kind not in _SCHEDULE_KINDS:
        raise SchemaError(f"unsupported schedule.kind {kind!r}")
    if source not in _SCHEDULE_SOURCES:
        raise SchemaError(f"unsupported schedule.source {source!r}")
    payload = {key: str(value.get(key) or "") for key in _SCHEDULE_STRINGS}
    payload["kind"] = kind
    payload["source"] = source
    return payload


@dataclass(frozen=True, slots=True)
class TextLabel:
    """Sheet TEXT that is an axis grafa or a linear size, not an INSERT."""

    id: str
    page: int
    kind: str
    text: str
    letter: str
    digit: str
    value: str
    source: str
    layer: str
    x: float
    y: float
    bbox: tuple[float, float, float, float]
    note: str = ""

    def __post_init__(self) -> None:
        _required(self.id, "textLabel.id")
        _page(self.page)
        if self.kind not in _TEXT_LABEL_KINDS:
            raise SchemaError(f"unsupported textLabel.kind {self.kind!r}")
        if self.source not in _TEXT_LABEL_SOURCES:
            raise SchemaError(f"unsupported textLabel.source {self.source!r}")
        _required(self.text, "textLabel.text")
        if _bbox(self.bbox) is None:
            raise SchemaError("textLabel.bbox is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "kind": self.kind,
            "text": self.text,
            "letter": self.letter,
            "digit": self.digit,
            "value": self.value,
            "source": self.source,
            "layer": self.layer,
            "x": self.x,
            "y": self.y,
            "bbox": list(self.bbox),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class NoteText:
    """Native TEXT under a «Примечан…» anchor. Not a textLabel."""

    id: str
    page: int
    text: str
    source: str
    layer: str
    x: float
    y: float
    bbox: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        _required(self.id, "noteText.id")
        _page(self.page)
        _required(self.text, "noteText.text")
        source = str(self.source or "text")
        if source not in _NOTE_TEXT_SOURCES:
            raise SchemaError(f"unsupported noteText.source {source!r}")
        object.__setattr__(self, "source", source)
        bbox = _bbox(self.bbox)
        if bbox is None:
            raise SchemaError("noteText.bbox is required")
        object.__setattr__(self, "bbox", bbox)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "text": self.text,
            "source": self.source,
            "layer": self.layer,
            "x": self.x,
            "y": self.y,
            "bbox": list(self.bbox),
        }


@dataclass(frozen=True, slots=True)
class SheetScene:
    """Paper-space view anchored by a native title. Not a viewport cut."""

    id: str
    page: int
    title: str
    bbox: tuple[float, float, float, float]
    soil_ids: tuple[str, ...] = ()
    axis_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required(self.id, "sheetScene.id")
        _page(self.page)
        _required(self.title, "sheetScene.title")
        bbox = _bbox(self.bbox)
        if bbox is None:
            raise SchemaError("sheetScene.bbox is required")
        object.__setattr__(self, "bbox", bbox)
        for item_id in self.soil_ids:
            _required(item_id, "sheetScene.soilIds[]")
        for item_id in self.axis_ids:
            _required(item_id, "sheetScene.axisIds[]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "title": self.title,
            "bbox": list(self.bbox),
            "soilIds": list(self.soil_ids),
            "axisIds": list(self.axis_ids),
        }


def _text_label_from_mapping(value: Any, *, page: int) -> TextLabel:
    if not isinstance(value, Mapping):
        raise SchemaError("textLabel must be an object")
    kind = str(value.get("kind") or "")
    source = str(value.get("source") or "text")
    if kind not in _TEXT_LABEL_KINDS:
        raise SchemaError(f"unsupported textLabel.kind {kind!r}")
    if source not in _TEXT_LABEL_SOURCES:
        raise SchemaError(f"unsupported textLabel.source {source!r}")
    bbox = _bbox(value.get("bbox"))
    if bbox is None:
        raise SchemaError("textLabel.bbox is required")
    return TextLabel(
        id=_required(str(value.get("id") or ""), "textLabel.id"),
        page=_page(value.get("page") or page),
        kind=kind,
        text=_required(str(value.get("text") or ""), "textLabel.text"),
        letter=str(value.get("letter") or ""),
        digit=str(value.get("digit") or ""),
        value=str(value.get("value") or ""),
        source=source,
        layer=str(value.get("layer") or ""),
        x=float(value.get("x") or 0.0),
        y=float(value.get("y") or 0.0),
        bbox=bbox,
        note=str(value.get("note") or ""),
    )


@dataclass(frozen=True, slots=True)
class FieldGeometry:
    """Sheet LINE/POLYLINE on the drawing field, not an INSERT."""

    id: str
    page: int
    kind: str
    layer: str
    color: str
    lineweight: float
    bbox: tuple[float, float, float, float]
    signature: str = ""
    legend_entry_id: str = ""
    label: str = ""
    source: str = "sheet_primitive"
    note: str = ""

    def __post_init__(self) -> None:
        _required(self.id, "fieldGeometry.id")
        _page(self.page)
        if self.kind not in _FIELD_GEOMETRY_KINDS:
            raise SchemaError(f"unsupported fieldGeometry.kind {self.kind!r}")
        if self.source not in _FIELD_GEOMETRY_SOURCES:
            raise SchemaError(f"unsupported fieldGeometry.source {self.source!r}")
        if _bbox(self.bbox) is None:
            raise SchemaError("fieldGeometry.bbox is required")
        if self.legend_entry_id and not self.label:
            raise SchemaError("fieldGeometry.label is required when legendEntryId is set")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "kind": self.kind,
            "layer": self.layer,
            "color": self.color,
            "lineweight": self.lineweight,
            "bbox": list(self.bbox),
            "signature": self.signature,
            "legendEntryId": self.legend_entry_id,
            "label": self.label,
            "source": self.source,
            "note": self.note,
        }


def _field_geometry_from_mapping(value: Any, *, page: int) -> FieldGeometry:
    if not isinstance(value, Mapping):
        raise SchemaError("fieldGeometry must be an object")
    kind = str(value.get("kind") or "")
    source = str(value.get("source") or "sheet_primitive")
    if kind not in _FIELD_GEOMETRY_KINDS:
        raise SchemaError(f"unsupported fieldGeometry.kind {kind!r}")
    if source not in _FIELD_GEOMETRY_SOURCES:
        raise SchemaError(f"unsupported fieldGeometry.source {source!r}")
    bbox = _bbox(value.get("bbox"))
    if bbox is None:
        raise SchemaError("fieldGeometry.bbox is required")
    return FieldGeometry(
        id=_required(str(value.get("id") or ""), "fieldGeometry.id"),
        page=_page(value.get("page") or page),
        kind=kind,
        layer=str(value.get("layer") or ""),
        color=str(value.get("color") or ""),
        lineweight=float(value.get("lineweight") or 0.0),
        bbox=bbox,
        signature=str(value.get("signature") or ""),
        legend_entry_id=str(value.get("legendEntryId") or ""),
        label=str(value.get("label") or ""),
        source=source,
        note=str(value.get("note") or ""),
    )


_SHEET_ZONE_KEYS = frozenset({"title_block", "drawing_field", "legend", "notes"})


def _sheet_zones_dict(value: Any) -> dict[str, list[float]] | None:
    """Normalize optional paper-mm zones. title_block is required when present.

    ``legend`` and ``notes`` are optional windows. ``drawing_field`` stays the
    stamp remainder; v1 does not recut it around the legend.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SchemaError("sheetZones must be an object")
    if "title_block" not in value:
        raise SchemaError("sheetZones.title_block is required")
    zones: dict[str, list[float]] = {}
    for key, bbox in value.items():
        name = _required(str(key), "sheetZones key")
        if name not in _SHEET_ZONE_KEYS:
            raise SchemaError(f"unsupported sheetZones key {name!r}")
        normalized = _bbox(bbox)
        if normalized is None:
            raise SchemaError(f"sheetZones.{name} must contain four coordinates")
        zones[name] = list(normalized)
    return zones


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float
    space: str
    units: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.x) or not math.isfinite(self.y):
            raise SchemaError("point coordinates must be finite")
        _required(self.space, "point.space")
        _required(self.units, "point.units")

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "space": self.space,
            "units": self.units,
        }


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    score: float
    source_ids: tuple[str, ...]
    detail: str

    def __post_init__(self) -> None:
        _required(self.kind, "evidence.kind")
        _confidence(self.score, "evidence.score")
        if not self.source_ids:
            raise SchemaError("evidence.sourceIds must not be empty")
        for source_id in self.source_ids:
            _required(source_id, "evidence.sourceIds[]")
        _required(self.detail, "evidence.detail")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "score": self.score,
            "sourceIds": list(self.source_ids),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class LegendEntry:
    id: str
    page: int
    label: str | None
    status: str
    source_kind: str
    signature: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    symbol_bbox: tuple[float, float, float, float] | None = None
    crop_path: str | None = None
    source_texts: tuple[str, ...] = ()
    reference_signatures: tuple[str, ...] = ()
    geometry_signatures: tuple[str, ...] = ()
    confidence: float = 0.0

    STATUSES = {"extracted", "text_unreadable", "unmatched"}

    def __post_init__(self) -> None:
        _required(self.id, "legend.id")
        _page(self.page)
        if self.status not in self.STATUSES:
            raise SchemaError(f"unsupported legend status {self.status!r}")
        _required(self.source_kind, "legend.sourceKind")
        if self.status != "text_unreadable":
            _required(self.label, "legend.label")
        elif self.label is not None:
            raise SchemaError("text_unreadable legend entry must not have a label")
        object.__setattr__(self, "bbox", _bbox(self.bbox))
        object.__setattr__(self, "symbol_bbox", _bbox(self.symbol_bbox))
        if self.crop_path is not None:
            _required(self.crop_path, "legend.cropPath")
        for text in self.source_texts:
            _required(text, "legend.sourceTexts[]")
        for signature in self.reference_signatures:
            _required(signature, "legend.referenceSignatures[]")
        if len(self.reference_signatures) != len(set(self.reference_signatures)):
            raise SchemaError("legend.referenceSignatures must be unique")
        for signature in self.geometry_signatures:
            _required(signature, "legend.geometrySignatures[]")
        if len(self.geometry_signatures) != len(set(self.geometry_signatures)):
            raise SchemaError("legend.geometrySignatures must be unique")
        _confidence(self.confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "label": self.label,
            "status": self.status,
            "sourceKind": self.source_kind,
            "signature": self.signature,
            "bbox": list(self.bbox) if self.bbox else None,
            "symbolBbox": list(self.symbol_bbox) if self.symbol_bbox else None,
            "cropPath": self.crop_path,
            "sourceTexts": list(self.source_texts),
            "referenceSignatures": list(self.reference_signatures),
            "geometrySignatures": list(self.geometry_signatures),
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class SymbolInstance:
    id: str
    page: int
    source_kind: str
    status: str
    position: Point
    source_handle: str
    source_space: str
    layer: str
    signature: str
    block_name: str | None = None
    role: str = "field_candidate"
    bbox: tuple[float, float, float, float] | None = None
    attributes: Mapping[str, str] = field(default_factory=dict)
    legend_entry_id: str | None = None
    symbol_type_id: str | None = None
    confidence: float = 0.0
    classification_reason: str | None = None
    dimension: Mapping[str, str] | None = None
    axis: Mapping[str, str] | None = None
    schedule: Mapping[str, str] | None = None

    STATUSES = {
        "confirmed",
        "probable",
        "conflicting",
        "unresolved",
        "unclassified",
        "reference",
        "ignored",
    }
    ROLES = {
        "field_candidate",
        "legend_exemplar",
        "sheet_furniture",
        "drawing_object",
        "drawing_annotation",
        "specification_mark",
    }
    NON_SYMBOL_ROLES = {
        "sheet_furniture",
        "drawing_object",
        "drawing_annotation",
        "specification_mark",
    }

    def __post_init__(self) -> None:
        _required(self.id, "instance.id")
        _page(self.page)
        _required(self.source_kind, "instance.sourceKind")
        if self.status not in self.STATUSES:
            raise SchemaError(f"unsupported symbol status {self.status!r}")
        _required(self.source_handle, "instance.sourceHandle")
        _required(self.source_space, "instance.sourceSpace")
        _required(self.layer, "instance.layer")
        _required(self.signature, "instance.signature")
        if self.role not in self.ROLES:
            raise SchemaError(f"unsupported symbol role {self.role!r}")
        object.__setattr__(self, "bbox", _bbox(self.bbox))
        object.__setattr__(self, "attributes", dict(self.attributes))
        _confidence(self.confidence)
        if self.classification_reason is not None:
            _required(self.classification_reason, "instance.classificationReason")
        object.__setattr__(self, "dimension", _dimension_dict(self.dimension))
        object.__setattr__(self, "axis", _axis_dict(self.axis))
        object.__setattr__(self, "schedule", _schedule_dict(self.schedule))
        if self.status in {"confirmed", "probable"} and not self.legend_entry_id:
            raise SchemaError(f"{self.status} instance requires legendEntryId")
        if self.status in {"unresolved", "unclassified", "ignored"} and self.legend_entry_id:
            raise SchemaError(f"{self.status} instance cannot have legendEntryId")
        if self.status == "reference" and not self.legend_entry_id:
            raise SchemaError("reference instance requires legendEntryId")
        if self.role == "legend_exemplar" and self.status != "reference":
            raise SchemaError("legend_exemplar instance must have reference status")
        if self.status == "reference" and self.role != "legend_exemplar":
            raise SchemaError("reference status requires legend_exemplar role")
        if self.role in self.NON_SYMBOL_ROLES and self.status != "ignored":
            raise SchemaError(f"{self.role} instance must have ignored status")
        if self.status == "ignored" and self.role not in self.NON_SYMBOL_ROLES:
            raise SchemaError("ignored status requires a non-symbol role")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "sourceKind": self.source_kind,
            "status": self.status,
            "position": self.position.to_dict(),
            "bbox": list(self.bbox) if self.bbox else None,
            "sourceHandle": self.source_handle,
            "sourceSpace": self.source_space,
            "layer": self.layer,
            "signature": self.signature,
            "blockName": self.block_name,
            "role": self.role,
            "attributes": dict(sorted(self.attributes.items())),
            "legendEntryId": self.legend_entry_id,
            "symbolTypeId": self.symbol_type_id,
            "confidence": self.confidence,
            "classificationReason": self.classification_reason,
            "dimension": dict(self.dimension) if self.dimension else None,
            "axis": dict(self.axis) if self.axis else None,
            "schedule": dict(self.schedule) if self.schedule else None,
        }


def symbol_instance_from_dict(payload: Mapping[str, Any], *, page: int) -> SymbolInstance:
    """Rebuild a symbol instance from a sidecar item for JSON-only recounts."""

    position = payload.get("position") or {}
    bbox = payload.get("bbox")
    return SymbolInstance(
        id=str(payload["id"]),
        page=int(payload.get("page") or page),
        source_kind=str(payload.get("sourceKind") or "dwg_insert_candidate"),
        status=str(payload.get("status") or "unresolved"),
        position=Point(
            float(position.get("x") or 0.0),
            float(position.get("y") or 0.0),
            str(position.get("space") or "paper"),
            str(position.get("units") or "mm"),
        ),
        source_handle=str(payload.get("sourceHandle") or payload["id"]),
        source_space=str(payload.get("sourceSpace") or "modelspace"),
        layer=str(payload.get("layer") or "0"),
        signature=str(payload.get("signature") or "blockdef-v1:missing"),
        block_name=payload.get("blockName"),
        role=str(payload.get("role") or "field_candidate"),
        bbox=tuple(bbox) if bbox else None,
        attributes={
            str(key): str(value)
            for key, value in dict(payload.get("attributes") or {}).items()
        },
        legend_entry_id=payload.get("legendEntryId"),
        symbol_type_id=payload.get("symbolTypeId"),
        confidence=float(payload.get("confidence") or 0.0),
        classification_reason=payload.get("classificationReason"),
        dimension=payload.get("dimension"),
        axis=payload.get("axis"),
        schedule=payload.get("schedule"),
    )


@dataclass(frozen=True, slots=True)
class SymbolBinding:
    id: str
    page: int
    instance_id: str
    legend_entry_id: str
    status: str
    confidence: float
    evidence: tuple[Evidence, ...]

    STATUSES = {"confirmed", "probable", "conflicting"}

    def __post_init__(self) -> None:
        _required(self.id, "binding.id")
        _page(self.page)
        _required(self.instance_id, "binding.instanceId")
        _required(self.legend_entry_id, "binding.legendEntryId")
        if self.status not in self.STATUSES:
            raise SchemaError(f"unsupported binding status {self.status!r}")
        _confidence(self.confidence)
        if not self.evidence:
            raise SchemaError("binding evidence must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "instanceId": self.instance_id,
            "legendEntryId": self.legend_entry_id,
            "status": self.status,
            "confidence": self.confidence,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class UnknownSymbolCluster:
    id: str
    page: int
    signature: str
    instance_ids: tuple[str, ...]
    reason: str
    representative_instance_id: str

    def __post_init__(self) -> None:
        _required(self.id, "unknown.id")
        _page(self.page)
        _required(self.signature, "unknown.signature")
        if not self.instance_ids:
            raise SchemaError("unknown.instanceIds must not be empty")
        if self.representative_instance_id not in self.instance_ids:
            raise SchemaError("representativeInstanceId must reference the cluster")
        _required(self.reason, "unknown.reason")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "signature": self.signature,
            "instanceIds": list(self.instance_ids),
            "reason": self.reason,
            "representativeInstanceId": self.representative_instance_id,
            "occurrences": len(self.instance_ids),
        }


@dataclass(frozen=True, slots=True)
class Relationship:
    id: str
    page: int
    source_id: str
    target_id: str
    relation_type: str
    status: str
    directed: bool
    confidence: float
    evidence: tuple[Evidence, ...]

    STATUSES = {"observed", "inferred", "conflicting"}

    def __post_init__(self) -> None:
        _required(self.id, "relationship.id")
        _page(self.page)
        _required(self.source_id, "relationship.sourceId")
        _required(self.target_id, "relationship.targetId")
        if self.source_id == self.target_id:
            raise SchemaError("relationship endpoints must differ")
        _required(self.relation_type, "relationship.type")
        if self.status not in self.STATUSES:
            raise SchemaError(f"unsupported relationship status {self.status!r}")
        _confidence(self.confidence)
        if not self.evidence:
            raise SchemaError("relationship evidence must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "sourceId": self.source_id,
            "targetId": self.target_id,
            "type": self.relation_type,
            "status": self.status,
            "directed": self.directed,
            "confidence": self.confidence,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(slots=True)
class PageResult:
    document_id: str
    document_path: str
    page: int
    completeness: str
    anomaly_codes: list[str] = field(default_factory=list)
    legend_entries: list[LegendEntry] = field(default_factory=list)
    symbol_instances: list[SymbolInstance] = field(default_factory=list)
    symbol_bindings: list[SymbolBinding] = field(default_factory=list)
    unknown_symbols: list[UnknownSymbolCluster] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    title_block: dict[str, Any] | None = None
    sheet_zones: dict[str, list[float]] | None = None
    text_labels: list[TextLabel] = field(default_factory=list)
    notes_texts: list[NoteText] = field(default_factory=list)
    sheet_scenes: list[SheetScene] = field(default_factory=list)
    field_geometry: list[FieldGeometry] = field(default_factory=list)

    COMPLETENESS = {"complete", "partial", "blocked"}
    TITLE_BLOCK_SOURCES = _TITLE_BLOCK_SOURCES

    def validate(self) -> None:
        _required(self.document_id, "documentId")
        _required(self.document_path, "documentPath")
        _page(self.page)
        if self.completeness not in self.COMPLETENESS:
            raise SchemaError(f"unsupported completeness {self.completeness!r}")
        for code in self.anomaly_codes:
            _required(code, "anomalyCodes[]")
        ids: set[str] = set()
        for collection in (
            self.legend_entries,
            self.symbol_instances,
            self.symbol_bindings,
            self.unknown_symbols,
            self.relationships,
            self.text_labels,
            self.notes_texts,
            self.sheet_scenes,
            self.field_geometry,
        ):
            for item in collection:
                if item.id in ids:
                    raise SchemaError(f"duplicate entity id {item.id!r}")
                ids.add(item.id)
        legend_ids = {item.id for item in self.legend_entries}
        instance_ids = {item.id for item in self.symbol_instances}
        instances_by_id = {item.id: item for item in self.symbol_instances}
        bound_instance_ids: set[str] = set()
        for binding in self.symbol_bindings:
            if binding.instance_id not in instance_ids:
                raise SchemaError(f"binding references unknown instance {binding.instance_id}")
            if binding.legend_entry_id not in legend_ids:
                raise SchemaError(
                    f"binding references unknown legend entry {binding.legend_entry_id}"
                )
            instance = instances_by_id[binding.instance_id]
            if instance.legend_entry_id != binding.legend_entry_id:
                raise SchemaError(
                    f"binding and instance disagree for {binding.instance_id}"
                )
            bound_instance_ids.add(binding.instance_id)
        typed_without_binding = {
            item.id
            for item in self.symbol_instances
            if item.status in {"confirmed", "probable"} and item.id not in bound_instance_ids
        }
        if typed_without_binding:
            raise SchemaError(
                f"typed instances require bindings: {sorted(typed_without_binding)}"
            )
        clustered_instance_ids: set[str] = set()
        for cluster in self.unknown_symbols:
            missing = set(cluster.instance_ids) - instance_ids
            if missing:
                raise SchemaError(f"unknown cluster references missing instances {missing}")
            non_symbols = {
                item_id
                for item_id in cluster.instance_ids
                if instances_by_id[item_id].role in SymbolInstance.NON_SYMBOL_ROLES
                or instances_by_id[item_id].status == "ignored"
            }
            if non_symbols:
                raise SchemaError(
                    f"unknown cluster contains non-symbol instances {sorted(non_symbols)}"
                )
            invalid = {
                item_id
                for item_id in cluster.instance_ids
                if instances_by_id[item_id].status not in {"unresolved", "unclassified"}
                or instances_by_id[item_id].role != "field_candidate"
            }
            if invalid:
                raise SchemaError(
                    f"unknown cluster contains classified instances {sorted(invalid)}"
                )
            overlap = clustered_instance_ids & set(cluster.instance_ids)
            if overlap:
                raise SchemaError(
                    f"instances occur in multiple unknown clusters: {sorted(overlap)}"
                )
            clustered_instance_ids.update(cluster.instance_ids)
        untyped_without_cluster = {
            item.id
            for item in self.symbol_instances
            if item.role == "field_candidate"
            and item.status in {"unresolved", "unclassified"}
            and item.id not in clustered_instance_ids
        }
        if untyped_without_cluster:
            raise SchemaError(
                f"untyped instances require unknown clusters: {sorted(untyped_without_cluster)}"
            )
        for relation in self.relationships:
            missing = {relation.source_id, relation.target_id} - instance_ids
            if missing:
                raise SchemaError(f"relationship references missing instances {missing}")
        self.title_block = _title_block_dict(self.title_block)
        if self.title_block is not None:
            instance_id = self.title_block.get("instanceId")
            if instance_id is not None and instance_id not in instance_ids:
                raise SchemaError(
                    f"titleBlock references unknown instance {instance_id}"
                )
        self.sheet_zones = _sheet_zones_dict(self.sheet_zones)
        for scene in self.sheet_scenes:
            missing_soils = set(scene.soil_ids) - instance_ids
            if missing_soils:
                raise SchemaError(
                    f"sheetScene references missing soil instances {sorted(missing_soils)}"
                )
            missing_axes = set(scene.axis_ids) - instance_ids
            if missing_axes:
                raise SchemaError(
                    f"sheetScene references missing axis instances {sorted(missing_axes)}"
                )
        for item in self.field_geometry:
            if item.legend_entry_id and item.legend_entry_id not in legend_ids:
                raise SchemaError(
                    f"fieldGeometry references unknown legend entry {item.legend_entry_id}"
                )

    def summary_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schemaVersion": SCHEMA_VERSION,
            "documentId": self.document_id,
            "documentPath": self.document_path,
            "page": self.page,
            "completeness": self.completeness,
            "anomalyCodes": sorted(set(self.anomaly_codes)),
            "counts": {
                "legendEntries": len(self.legend_entries),
                "symbolInstances": len(self.symbol_instances),
                "symbolBindings": len(self.symbol_bindings),
                "unknownClusters": len(self.unknown_symbols),
                "relationships": len(self.relationships),
                "textLabels": len(self.text_labels),
                "notesTexts": len(self.notes_texts),
                "sheetScenes": len(self.sheet_scenes),
                "fieldGeometry": len(self.field_geometry),
            },
            "instanceStatuses": dict(
                sorted(Counter(item.status for item in self.symbol_instances).items())
            ),
            "instanceRoles": dict(
                sorted(Counter(item.role for item in self.symbol_instances).items())
            ),
            "bindingStatuses": dict(
                sorted(Counter(item.status for item in self.symbol_bindings).items())
            ),
            "titleBlock": (
                dict(self.title_block) if self.title_block is not None else None
            ),
            "sheetZones": (
                {name: list(box) for name, box in self.sheet_zones.items()}
                if self.sheet_zones is not None
                else None
            ),
        }
