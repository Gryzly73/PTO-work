"""Versioned, JSON-safe entities used by the symbols pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, ClassVar, Mapping, TypeVar

from . import SYMBOLS_SCHEMA_VERSION
from .geometry import BBox, GeometryError

SCHEMA_VERSION = SYMBOLS_SCHEMA_VERSION


class SchemaError(ValueError):
    """Malformed symbol entity or unsupported sidecar schema."""


def stable_id(prefix: str, page: int, *identity: object) -> str:
    """Build a reproducible ID without depending on detection order."""

    if not prefix or not prefix.replace("-", "").isalnum():
        raise SchemaError("ID prefix must be non-empty and alphanumeric")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise SchemaError("page must be a positive integer")
    payload = json.dumps(
        [page, *identity],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:12]
    return f"{prefix}-p{page:04d}-{digest}"


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, name)


def _page(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchemaError("page must be a positive integer")
    return value


def _confidence(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise SchemaError(f"{name} must be between 0 and 1")
    return float(value)


def _bbox(value: Any, name: str) -> BBox:
    if isinstance(value, BBox):
        return value
    if not isinstance(value, (list, tuple)):
        raise SchemaError(f"{name} must be a bbox array")
    try:
        return BBox.from_list(value)
    except GeometryError as exc:
        raise SchemaError(f"{name}: {exc}") from exc


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise SchemaError(f"{name} must be an array")
    result = tuple(_required_string(item, f"{name}[]") for item in value)
    if len(set(result)) != len(result):
        raise SchemaError(f"{name} must not contain duplicates")
    return result


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{name} must be an object")
    return dict(value)


def _raise_schema_array(name: str) -> tuple[Any, ...]:
    raise SchemaError(f"{name} must be an array")


def _validate_keys(
    data: Mapping[str, Any], required: set[str], optional: set[str] = frozenset()
) -> None:
    missing = required - data.keys()
    if missing:
        raise SchemaError(f"missing fields: {sorted(missing)}")
    unknown = data.keys() - required - optional
    if unknown:
        raise SchemaError(f"unknown fields: {sorted(unknown)}")


@dataclass(frozen=True, slots=True)
class VisualSignature:
    method: str
    version: int
    value: str

    def __post_init__(self) -> None:
        _required_string(self.method, "method")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise SchemaError("visual signature version must be a positive integer")
        _required_string(self.value, "value")

    def to_dict(self) -> dict[str, Any]:
        return {"method": self.method, "version": self.version, "value": self.value}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualSignature":
        _validate_keys(data, {"method", "version", "value"})
        return cls(
            method=_required_string(data["method"], "method"),
            version=data["version"],
            value=_required_string(data["value"], "value"),
        )


@dataclass(frozen=True, slots=True)
class ClassificationEvidence:
    """One auditable, document-local reason contributing to a binding."""

    kind: str
    score: float
    source_entity_ids: tuple[str, ...]
    detail: str

    def __post_init__(self) -> None:
        _required_string(self.kind, "kind")
        _confidence(self.score, "score")
        source_ids = _string_tuple(self.source_entity_ids, "sourceEntityIds")
        object.__setattr__(self, "source_entity_ids", source_ids)
        if not source_ids:
            raise SchemaError("sourceEntityIds must not be empty")
        _required_string(self.detail, "detail")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "score": self.score,
            "sourceEntityIds": list(self.source_entity_ids),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ClassificationEvidence":
        _validate_keys(data, {"kind", "score", "sourceEntityIds", "detail"})
        return cls(
            kind=_required_string(data["kind"], "kind"),
            score=_confidence(data["score"], "score"),
            source_entity_ids=_string_tuple(
                data["sourceEntityIds"], "sourceEntityIds"
            ),
            detail=_required_string(data["detail"], "detail"),
        )


class SymbolEntity:
    KIND: ClassVar[str]

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class LegendEntry(SymbolEntity):
    KIND: ClassVar[str] = "legendEntry"
    id: str
    page: int
    region_id: str
    bbox_pdf: BBox
    symbol_bbox_pdf: BBox
    raw_crop: str
    source_kind: str
    source_document: str
    status: str
    position: str | None = None
    name_raw: str | None = None
    name_normalized: str | None = None
    text_bbox_pdf: BBox | None = None
    normalized_crop: str | None = None
    confidence: float = 0.0

    STATUSES: ClassVar[set[str]] = {"extracted", "text_unreadable", "unmatched"}

    def __post_init__(self) -> None:
        _required_string(self.id, "id")
        _page(self.page)
        _required_string(self.region_id, "regionId")
        object.__setattr__(self, "bbox_pdf", _bbox(self.bbox_pdf, "bboxPdf"))
        object.__setattr__(
            self,
            "symbol_bbox_pdf",
            _bbox(self.symbol_bbox_pdf, "symbolBboxPdf"),
        )
        if self.text_bbox_pdf is not None:
            object.__setattr__(
                self,
                "text_bbox_pdf",
                _bbox(self.text_bbox_pdf, "textBboxPdf"),
            )
        _required_string(self.raw_crop, "rawCrop")
        _required_string(self.source_kind, "sourceKind")
        _required_string(self.source_document, "sourceDocument")
        if self.status not in self.STATUSES:
            raise SchemaError(f"unsupported legend entry status {self.status!r}")
        _optional_string(self.position, "position")
        _optional_string(self.name_raw, "nameRaw")
        _optional_string(self.name_normalized, "nameNormalized")
        _optional_string(self.normalized_crop, "normalizedCrop")
        _confidence(self.confidence, "confidence")
        if self.status != "text_unreadable" and not self.name_raw:
            raise SchemaError(f"{self.status} legend entry requires nameRaw")
        if self.status == "text_unreadable" and (
            self.name_raw is not None or self.name_normalized is not None
        ):
            raise SchemaError("text_unreadable legend entry cannot have a name")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "regionId": self.region_id,
            "position": self.position,
            "nameRaw": self.name_raw,
            "nameNormalized": self.name_normalized,
            "bboxPdf": self.bbox_pdf.to_list(),
            "symbolBboxPdf": self.symbol_bbox_pdf.to_list(),
            "textBboxPdf": self.text_bbox_pdf.to_list() if self.text_bbox_pdf else None,
            "rawCrop": self.raw_crop,
            "normalizedCrop": self.normalized_crop,
            "sourceKind": self.source_kind,
            "sourceDocument": self.source_document,
            "status": self.status,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LegendEntry":
        required = {
            "id", "page", "regionId", "position", "nameRaw", "nameNormalized",
            "bboxPdf", "symbolBboxPdf", "textBboxPdf", "rawCrop",
            "normalizedCrop", "sourceKind", "sourceDocument", "status", "confidence",
        }
        _validate_keys(data, required)
        return cls(
            id=_required_string(data["id"], "id"),
            page=_page(data["page"]),
            region_id=_required_string(data["regionId"], "regionId"),
            position=_optional_string(data["position"], "position"),
            name_raw=_optional_string(data["nameRaw"], "nameRaw"),
            name_normalized=_optional_string(data["nameNormalized"], "nameNormalized"),
            bbox_pdf=_bbox(data["bboxPdf"], "bboxPdf"),
            symbol_bbox_pdf=_bbox(data["symbolBboxPdf"], "symbolBboxPdf"),
            text_bbox_pdf=(
                _bbox(data["textBboxPdf"], "textBboxPdf")
                if data["textBboxPdf"] is not None
                else None
            ),
            raw_crop=_required_string(data["rawCrop"], "rawCrop"),
            normalized_crop=_optional_string(data["normalizedCrop"], "normalizedCrop"),
            source_kind=_required_string(data["sourceKind"], "sourceKind"),
            source_document=_required_string(data["sourceDocument"], "sourceDocument"),
            status=_required_string(data["status"], "status"),
            confidence=_confidence(data["confidence"], "confidence"),
        )


@dataclass(frozen=True, slots=True)
class SymbolCandidate(SymbolEntity):
    KIND: ClassVar[str] = "symbolCandidate"
    id: str
    page: int
    bbox_pdf: BBox
    raw_crop: str
    source_kinds: tuple[str, ...]
    status: str = "candidate"
    normalized_crop: str | None = None
    visual_signature: VisualSignature | None = None
    confidence: float = 0.0

    STATUSES: ClassVar[set[str]] = {"candidate", "rejected", "unclassified"}

    def __post_init__(self) -> None:
        _required_string(self.id, "id")
        _page(self.page)
        object.__setattr__(self, "bbox_pdf", _bbox(self.bbox_pdf, "bboxPdf"))
        _required_string(self.raw_crop, "rawCrop")
        source_kinds = _string_tuple(self.source_kinds, "sourceKinds")
        object.__setattr__(self, "source_kinds", source_kinds)
        if not source_kinds:
            raise SchemaError("sourceKinds must not be empty")
        if self.status not in self.STATUSES:
            raise SchemaError(f"unsupported symbol candidate status {self.status!r}")
        _optional_string(self.normalized_crop, "normalizedCrop")
        _confidence(self.confidence, "confidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "bboxPdf": self.bbox_pdf.to_list(),
            "rawCrop": self.raw_crop,
            "normalizedCrop": self.normalized_crop,
            "sourceKinds": list(self.source_kinds),
            "status": self.status,
            "visualSignature": (
                self.visual_signature.to_dict() if self.visual_signature else None
            ),
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SymbolCandidate":
        required = {
            "id", "page", "bboxPdf", "rawCrop", "normalizedCrop", "sourceKinds",
            "status", "visualSignature", "confidence",
        }
        _validate_keys(data, required)
        signature = data["visualSignature"]
        return cls(
            id=_required_string(data["id"], "id"),
            page=_page(data["page"]),
            bbox_pdf=_bbox(data["bboxPdf"], "bboxPdf"),
            raw_crop=_required_string(data["rawCrop"], "rawCrop"),
            normalized_crop=_optional_string(data["normalizedCrop"], "normalizedCrop"),
            source_kinds=_string_tuple(data["sourceKinds"], "sourceKinds"),
            status=_required_string(data["status"], "status"),
            visual_signature=(
                VisualSignature.from_dict(_mapping(signature, "visualSignature"))
                if signature is not None
                else None
            ),
            confidence=_confidence(data["confidence"], "confidence"),
        )


@dataclass(frozen=True, slots=True)
class SymbolType(SymbolEntity):
    KIND: ClassVar[str] = "symbolType"
    id: str
    representative_crop: str
    instance_ids: tuple[str, ...]
    status: str
    matched_legend_entry_id: str | None = None
    candidate_legend_entry_ids: tuple[str, ...] = ()
    visual_signature: VisualSignature | None = None
    classification_evidence: tuple[ClassificationEvidence, ...] = ()

    STATUSES: ClassVar[set[str]] = {
        "confirmed", "probable", "conflicting", "unresolved", "unclassified", "rejected"
    }

    def __post_init__(self) -> None:
        _required_string(self.id, "id")
        _required_string(self.representative_crop, "representativeCrop")
        object.__setattr__(
            self, "instance_ids", _string_tuple(self.instance_ids, "instanceIds")
        )
        _optional_string(self.matched_legend_entry_id, "matchedLegendEntryId")
        object.__setattr__(
            self,
            "candidate_legend_entry_ids",
            _string_tuple(
                self.candidate_legend_entry_ids, "candidateLegendEntryIds"
            ),
        )
        if not isinstance(self.classification_evidence, (list, tuple)):
            raise SchemaError("classificationEvidence must be an array")
        evidence = tuple(self.classification_evidence)
        if any(not isinstance(item, ClassificationEvidence) for item in evidence):
            raise SchemaError(
                "classificationEvidence must contain ClassificationEvidence objects"
            )
        object.__setattr__(self, "classification_evidence", evidence)
        if self.status not in self.STATUSES:
            raise SchemaError(f"unsupported symbol type status {self.status!r}")
        if self.status == "confirmed" and self.matched_legend_entry_id is None:
            raise SchemaError("confirmed symbol type requires matchedLegendEntryId")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "representativeCrop": self.representative_crop,
            "instanceIds": list(self.instance_ids),
            "matchedLegendEntryId": self.matched_legend_entry_id,
            "candidateLegendEntryIds": list(self.candidate_legend_entry_ids),
            "status": self.status,
            "visualSignature": (
                self.visual_signature.to_dict() if self.visual_signature else None
            ),
            "classificationEvidence": [
                item.to_dict() for item in self.classification_evidence
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SymbolType":
        required = {
            "id", "representativeCrop", "instanceIds", "matchedLegendEntryId",
            "candidateLegendEntryIds", "status", "visualSignature",
            "classificationEvidence",
        }
        _validate_keys(data, required)
        signature = data["visualSignature"]
        return cls(
            id=_required_string(data["id"], "id"),
            representative_crop=_required_string(
                data["representativeCrop"], "representativeCrop"
            ),
            instance_ids=_string_tuple(data["instanceIds"], "instanceIds"),
            matched_legend_entry_id=_optional_string(
                data["matchedLegendEntryId"], "matchedLegendEntryId"
            ),
            candidate_legend_entry_ids=_string_tuple(
                data["candidateLegendEntryIds"], "candidateLegendEntryIds"
            ),
            status=_required_string(data["status"], "status"),
            visual_signature=(
                VisualSignature.from_dict(_mapping(signature, "visualSignature"))
                if signature is not None
                else None
            ),
            classification_evidence=tuple(
                ClassificationEvidence.from_dict(
                    _mapping(item, "classificationEvidence[]")
                )
                for item in data["classificationEvidence"]
            )
            if isinstance(data["classificationEvidence"], (list, tuple))
            else (_raise_schema_array("classificationEvidence")),
        )


@dataclass(frozen=True, slots=True)
class SymbolInstance(SymbolEntity):
    KIND: ClassVar[str] = "symbolInstance"
    id: str
    page: int
    bbox_pdf: BBox
    raw_crop: str
    source_kinds: tuple[str, ...]
    status: str
    symbol_type_id: str | None = None
    legend_entry_id: str | None = None
    normalized_crop: str | None = None
    nearby_labels: tuple[str, ...] = ()
    connected_line_ids: tuple[str, ...] = ()
    geometry_confidence: float = 0.0
    classification_confidence: float = 0.0
    classification_evidence: tuple[ClassificationEvidence, ...] = ()

    STATUSES: ClassVar[set[str]] = {
        "confirmed", "probable", "conflicting", "unresolved", "unclassified", "rejected"
    }

    def __post_init__(self) -> None:
        _required_string(self.id, "id")
        _page(self.page)
        object.__setattr__(self, "bbox_pdf", _bbox(self.bbox_pdf, "bboxPdf"))
        _required_string(self.raw_crop, "rawCrop")
        source_kinds = _string_tuple(self.source_kinds, "sourceKinds")
        object.__setattr__(self, "source_kinds", source_kinds)
        if not source_kinds:
            raise SchemaError("sourceKinds must not be empty")
        _optional_string(self.symbol_type_id, "symbolTypeId")
        _optional_string(self.legend_entry_id, "legendEntryId")
        _optional_string(self.normalized_crop, "normalizedCrop")
        object.__setattr__(
            self, "nearby_labels", _string_tuple(self.nearby_labels, "nearbyLabels")
        )
        object.__setattr__(
            self,
            "connected_line_ids",
            _string_tuple(self.connected_line_ids, "connectedLineIds"),
        )
        if not isinstance(self.classification_evidence, (list, tuple)):
            raise SchemaError("classificationEvidence must be an array")
        evidence = tuple(self.classification_evidence)
        if any(not isinstance(item, ClassificationEvidence) for item in evidence):
            raise SchemaError(
                "classificationEvidence must contain ClassificationEvidence objects"
            )
        object.__setattr__(self, "classification_evidence", evidence)
        if self.status not in self.STATUSES:
            raise SchemaError(f"unsupported symbol instance status {self.status!r}")
        _confidence(self.geometry_confidence, "geometryConfidence")
        _confidence(self.classification_confidence, "classificationConfidence")
        if self.status == "confirmed" and (
            self.symbol_type_id is None or self.legend_entry_id is None
        ):
            raise SchemaError(
                "confirmed symbol instance requires symbolTypeId and legendEntryId"
            )
        if self.status == "unclassified" and self.legend_entry_id is not None:
            raise SchemaError("unclassified symbol instance cannot have legendEntryId")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "symbolTypeId": self.symbol_type_id,
            "legendEntryId": self.legend_entry_id,
            "bboxPdf": self.bbox_pdf.to_list(),
            "rawCrop": self.raw_crop,
            "normalizedCrop": self.normalized_crop,
            "nearbyLabels": list(self.nearby_labels),
            "connectedLineIds": list(self.connected_line_ids),
            "sourceKinds": list(self.source_kinds),
            "status": self.status,
            "geometryConfidence": self.geometry_confidence,
            "classificationConfidence": self.classification_confidence,
            "classificationEvidence": [
                item.to_dict() for item in self.classification_evidence
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SymbolInstance":
        required = {
            "id", "page", "symbolTypeId", "legendEntryId", "bboxPdf", "rawCrop",
            "normalizedCrop", "nearbyLabels", "connectedLineIds", "sourceKinds",
            "status", "geometryConfidence", "classificationConfidence",
            "classificationEvidence",
        }
        _validate_keys(data, required)
        return cls(
            id=_required_string(data["id"], "id"),
            page=_page(data["page"]),
            symbol_type_id=_optional_string(data["symbolTypeId"], "symbolTypeId"),
            legend_entry_id=_optional_string(data["legendEntryId"], "legendEntryId"),
            bbox_pdf=_bbox(data["bboxPdf"], "bboxPdf"),
            raw_crop=_required_string(data["rawCrop"], "rawCrop"),
            normalized_crop=_optional_string(data["normalizedCrop"], "normalizedCrop"),
            nearby_labels=_string_tuple(data["nearbyLabels"], "nearbyLabels"),
            connected_line_ids=_string_tuple(
                data["connectedLineIds"], "connectedLineIds"
            ),
            source_kinds=_string_tuple(data["sourceKinds"], "sourceKinds"),
            status=_required_string(data["status"], "status"),
            geometry_confidence=_confidence(
                data["geometryConfidence"], "geometryConfidence"
            ),
            classification_confidence=_confidence(
                data["classificationConfidence"], "classificationConfidence"
            ),
            classification_evidence=tuple(
                ClassificationEvidence.from_dict(
                    _mapping(item, "classificationEvidence[]")
                )
                for item in data["classificationEvidence"]
            )
            if isinstance(data["classificationEvidence"], (list, tuple))
            else (_raise_schema_array("classificationEvidence")),
        )


@dataclass(frozen=True, slots=True)
class SymbolConflict(SymbolEntity):
    KIND: ClassVar[str] = "conflict"
    id: str
    page: int
    kind: str
    entity_ids: tuple[str, ...]
    candidate_legend_entry_ids: tuple[str, ...]
    reason: str
    status: str = "unresolved"

    STATUSES: ClassVar[set[str]] = {"unresolved", "resolved", "dismissed"}

    def __post_init__(self) -> None:
        _required_string(self.id, "id")
        _page(self.page)
        _required_string(self.kind, "kind")
        entity_ids = _string_tuple(self.entity_ids, "entityIds")
        object.__setattr__(self, "entity_ids", entity_ids)
        if not entity_ids:
            raise SchemaError("entityIds must not be empty")
        object.__setattr__(
            self,
            "candidate_legend_entry_ids",
            _string_tuple(
                self.candidate_legend_entry_ids, "candidateLegendEntryIds"
            ),
        )
        _required_string(self.reason, "reason")
        if self.status not in self.STATUSES:
            raise SchemaError(f"unsupported conflict status {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "kind": self.kind,
            "entityIds": list(self.entity_ids),
            "candidateLegendEntryIds": list(self.candidate_legend_entry_ids),
            "reason": self.reason,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SymbolConflict":
        required = {
            "id", "page", "kind", "entityIds", "candidateLegendEntryIds",
            "reason", "status",
        }
        _validate_keys(data, required)
        return cls(
            id=_required_string(data["id"], "id"),
            page=_page(data["page"]),
            kind=_required_string(data["kind"], "kind"),
            entity_ids=_string_tuple(data["entityIds"], "entityIds"),
            candidate_legend_entry_ids=_string_tuple(
                data["candidateLegendEntryIds"], "candidateLegendEntryIds"
            ),
            reason=_required_string(data["reason"], "reason"),
            status=_required_string(data["status"], "status"),
        )


@dataclass(frozen=True, slots=True)
class PageSymbolsSummary(SymbolEntity):
    KIND: ClassVar[str] = "summary"
    page: int
    legend_entry_count: int = 0
    symbol_type_count: int = 0
    symbol_instance_count: int = 0
    unclassified_count: int = 0
    unmatched_legend_entry_count: int = 0
    conflict_count: int = 0
    status: str = "complete"
    anomaly_codes: tuple[str, ...] = ()

    STATUSES: ClassVar[set[str]] = {
        "complete", "no_valid_legend", "partial", "failed", "not_run"
    }

    def __post_init__(self) -> None:
        _page(self.page)
        for name in (
            "legend_entry_count", "symbol_type_count", "symbol_instance_count",
            "unclassified_count", "unmatched_legend_entry_count", "conflict_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SchemaError(f"{name} must be a non-negative integer")
        if self.status not in self.STATUSES:
            raise SchemaError(f"unsupported summary status {self.status!r}")
        anomaly_codes = _string_tuple(self.anomaly_codes, "anomalyCodes")
        object.__setattr__(self, "anomaly_codes", anomaly_codes)
        if self.status == "complete" and anomaly_codes:
            raise SchemaError("complete summary cannot contain anomalyCodes")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "page": self.page,
            "legendEntryCount": self.legend_entry_count,
            "symbolTypeCount": self.symbol_type_count,
            "symbolInstanceCount": self.symbol_instance_count,
            "unclassifiedCount": self.unclassified_count,
            "unmatchedLegendEntryCount": self.unmatched_legend_entry_count,
            "conflictCount": self.conflict_count,
            "status": self.status,
        }
        if self.anomaly_codes:
            result["anomalyCodes"] = list(self.anomaly_codes)
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PageSymbolsSummary":
        required = {
            "page", "legendEntryCount", "symbolTypeCount", "symbolInstanceCount",
            "unclassifiedCount", "unmatchedLegendEntryCount", "conflictCount", "status",
        }
        _validate_keys(data, required, {"anomalyCodes"})
        return cls(
            page=_page(data["page"]),
            legend_entry_count=data["legendEntryCount"],
            symbol_type_count=data["symbolTypeCount"],
            symbol_instance_count=data["symbolInstanceCount"],
            unclassified_count=data["unclassifiedCount"],
            unmatched_legend_entry_count=data["unmatchedLegendEntryCount"],
            conflict_count=data["conflictCount"],
            status=_required_string(data["status"], "status"),
            anomaly_codes=_string_tuple(data.get("anomalyCodes", ()), "anomalyCodes"),
        )


EntityT = TypeVar("EntityT", bound=SymbolEntity)

# Short compatibility name for callers that do not need to distinguish the
# runtime conflict entity from future document-catalog conflicts.
Conflict = SymbolConflict
