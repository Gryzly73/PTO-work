"""Deterministic document-level catalog built from completed page sidecars."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .artifacts import PageArtifacts, atomic_write_json, load_page_artifacts
from .schema import ClassificationEvidence, SchemaError

CATALOG_SCHEMA_VERSION = 1
CATALOG_FILENAME = "document_symbol_catalog.json"
_PAGE_DIRECTORY = re.compile(r"page_(\d{4,})$")


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{name} must be a non-empty string")
    return value


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise SchemaError(f"{name} must be an array")
    result = tuple(_required_string(item, f"{name}[]") for item in value)
    if len(set(result)) != len(result):
        raise SchemaError(f"{name} must not contain duplicates")
    return result


def _catalog_id(*identity: object) -> str:
    payload = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"PSC-{hashlib.sha256(payload).hexdigest()[:12].upper()}"


@dataclass(frozen=True, slots=True)
class CatalogVariant:
    id: str
    document: str
    page: int
    legend_entry_id: str
    symbol_type_ids: tuple[str, ...]
    instance_ids: tuple[str, ...]
    crop: str
    provenance: tuple[ClassificationEvidence, ...] = ()

    def __post_init__(self) -> None:
        _required_string(self.id, "id")
        _required_string(self.document, "document")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 1:
            raise SchemaError("page must be a positive integer")
        _required_string(self.legend_entry_id, "legendEntryId")
        object.__setattr__(
            self, "symbol_type_ids", _string_tuple(self.symbol_type_ids, "symbolTypeIds")
        )
        object.__setattr__(
            self, "instance_ids", _string_tuple(self.instance_ids, "instanceIds")
        )
        _required_string(self.crop, "crop")
        evidence = tuple(self.provenance)
        if any(not isinstance(item, ClassificationEvidence) for item in evidence):
            raise SchemaError("provenance must contain ClassificationEvidence objects")
        object.__setattr__(self, "provenance", evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "document": self.document,
            "page": self.page,
            "legendEntryId": self.legend_entry_id,
            "symbolTypeIds": list(self.symbol_type_ids),
            "instanceIds": list(self.instance_ids),
            "crop": self.crop,
            "provenance": [item.to_dict() for item in self.provenance],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CatalogVariant":
        expected = {
            "id", "document", "page", "legendEntryId", "symbolTypeIds",
            "instanceIds", "crop", "provenance",
        }
        if set(data) != expected:
            raise SchemaError("invalid catalog variant fields")
        provenance = data["provenance"]
        if not isinstance(provenance, list):
            raise SchemaError("provenance must be an array")
        return cls(
            id=_required_string(data["id"], "id"),
            document=_required_string(data["document"], "document"),
            page=data["page"],
            legend_entry_id=_required_string(data["legendEntryId"], "legendEntryId"),
            symbol_type_ids=_string_tuple(data["symbolTypeIds"], "symbolTypeIds"),
            instance_ids=_string_tuple(data["instanceIds"], "instanceIds"),
            crop=_required_string(data["crop"], "crop"),
            provenance=tuple(ClassificationEvidence.from_dict(item) for item in provenance),
        )


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    catalog_id: str
    canonical_name: str
    aliases: tuple[str, ...]
    variants: tuple[CatalogVariant, ...]
    review_status: str = "auto_extracted"
    applied_decision_ids: tuple[str, ...] = ()

    REVIEW_STATUSES = {
        "auto_extracted", "needs_review", "human_confirmed", "deprecated", "conflicting"
    }

    def __post_init__(self) -> None:
        _required_string(self.catalog_id, "catalogId")
        _required_string(self.canonical_name, "canonicalName")
        object.__setattr__(self, "aliases", _string_tuple(self.aliases, "aliases"))
        variants = tuple(self.variants)
        if not variants or any(not isinstance(item, CatalogVariant) for item in variants):
            raise SchemaError("catalog entry requires CatalogVariant objects")
        if len({item.id for item in variants}) != len(variants):
            raise SchemaError("catalog entry contains duplicate variant IDs")
        object.__setattr__(self, "variants", variants)
        if self.review_status not in self.REVIEW_STATUSES:
            raise SchemaError(f"unsupported reviewStatus {self.review_status!r}")
        object.__setattr__(
            self,
            "applied_decision_ids",
            _string_tuple(self.applied_decision_ids, "appliedDecisionIds"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalogId": self.catalog_id,
            "canonicalName": self.canonical_name,
            "aliases": list(self.aliases),
            "variants": [item.to_dict() for item in self.variants],
            "reviewStatus": self.review_status,
            "appliedDecisionIds": list(self.applied_decision_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CatalogEntry":
        expected = {
            "catalogId", "canonicalName", "aliases", "variants", "reviewStatus",
            "appliedDecisionIds",
        }
        if set(data) != expected or not isinstance(data["variants"], list):
            raise SchemaError("invalid catalog entry fields")
        return cls(
            catalog_id=_required_string(data["catalogId"], "catalogId"),
            canonical_name=_required_string(data["canonicalName"], "canonicalName"),
            aliases=_string_tuple(data["aliases"], "aliases"),
            variants=tuple(CatalogVariant.from_dict(item) for item in data["variants"]),
            review_status=_required_string(data["reviewStatus"], "reviewStatus"),
            applied_decision_ids=_string_tuple(
                data["appliedDecisionIds"], "appliedDecisionIds"
            ),
        )


@dataclass(frozen=True, slots=True)
class CatalogUnresolved:
    id: str
    page: int
    status: str
    entity_ids: tuple[str, ...]
    candidate_legend_entry_ids: tuple[str, ...] = ()
    reason: str = "not confirmed by document evidence"
    applied_decision_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_string(self.id, "id")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 1:
            raise SchemaError("page must be a positive integer")
        _required_string(self.status, "status")
        entity_ids = _string_tuple(self.entity_ids, "entityIds")
        if not entity_ids:
            raise SchemaError("entityIds must not be empty")
        object.__setattr__(self, "entity_ids", entity_ids)
        object.__setattr__(
            self,
            "candidate_legend_entry_ids",
            _string_tuple(self.candidate_legend_entry_ids, "candidateLegendEntryIds"),
        )
        _required_string(self.reason, "reason")
        object.__setattr__(
            self,
            "applied_decision_ids",
            _string_tuple(self.applied_decision_ids, "appliedDecisionIds"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "status": self.status,
            "entityIds": list(self.entity_ids),
            "candidateLegendEntryIds": list(self.candidate_legend_entry_ids),
            "reason": self.reason,
            "appliedDecisionIds": list(self.applied_decision_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CatalogUnresolved":
        expected = {
            "id", "page", "status", "entityIds", "candidateLegendEntryIds",
            "reason", "appliedDecisionIds",
        }
        if set(data) != expected:
            raise SchemaError("invalid unresolved catalog fields")
        return cls(
            id=_required_string(data["id"], "id"),
            page=data["page"],
            status=_required_string(data["status"], "status"),
            entity_ids=_string_tuple(data["entityIds"], "entityIds"),
            candidate_legend_entry_ids=_string_tuple(
                data["candidateLegendEntryIds"], "candidateLegendEntryIds"
            ),
            reason=_required_string(data["reason"], "reason"),
            applied_decision_ids=_string_tuple(
                data["appliedDecisionIds"], "appliedDecisionIds"
            ),
        )


@dataclass(frozen=True, slots=True)
class DocumentSymbolCatalog:
    document: str
    entries: tuple[CatalogEntry, ...] = ()
    unresolved: tuple[CatalogUnresolved, ...] = ()
    applied_review_decision_ids: tuple[str, ...] = ()
    schema_version: int = CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _required_string(self.document, "document")
        if self.schema_version != CATALOG_SCHEMA_VERSION:
            raise SchemaError(
                f"unsupported catalog schemaVersion {self.schema_version!r}"
            )
        entries = tuple(self.entries)
        unresolved = tuple(self.unresolved)
        if any(not isinstance(item, CatalogEntry) for item in entries):
            raise SchemaError("entries must contain CatalogEntry objects")
        if any(not isinstance(item, CatalogUnresolved) for item in unresolved):
            raise SchemaError("unresolved must contain CatalogUnresolved objects")
        if len({item.catalog_id for item in entries}) != len(entries):
            raise SchemaError("catalog contains duplicate catalog IDs")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "unresolved", unresolved)
        object.__setattr__(
            self,
            "applied_review_decision_ids",
            _string_tuple(
                self.applied_review_decision_ids, "appliedReviewDecisionIds"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "kind": "documentSymbolCatalog",
            "document": self.document,
            "entries": [item.to_dict() for item in self.entries],
            "unresolved": [item.to_dict() for item in self.unresolved],
            "appliedReviewDecisionIds": list(self.applied_review_decision_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DocumentSymbolCatalog":
        expected = {
            "schemaVersion", "kind", "document", "entries", "unresolved",
            "appliedReviewDecisionIds",
        }
        if set(data) != expected or data["kind"] != "documentSymbolCatalog":
            raise SchemaError("invalid document catalog envelope")
        if not isinstance(data["entries"], list) or not isinstance(data["unresolved"], list):
            raise SchemaError("entries and unresolved must be arrays")
        return cls(
            schema_version=data["schemaVersion"],
            document=_required_string(data["document"], "document"),
            entries=tuple(CatalogEntry.from_dict(item) for item in data["entries"]),
            unresolved=tuple(
                CatalogUnresolved.from_dict(item) for item in data["unresolved"]
            ),
            applied_review_decision_ids=_string_tuple(
                data["appliedReviewDecisionIds"], "appliedReviewDecisionIds"
            ),
        )


def discover_page_numbers(output_root: str | Path) -> tuple[int, ...]:
    symbols_root = Path(output_root) / "symbols"
    if not symbols_root.is_dir():
        return ()
    pages = []
    for path in symbols_root.iterdir():
        match = _PAGE_DIRECTORY.fullmatch(path.name)
        if path.is_dir() and match:
            pages.append(int(match.group(1)))
    return tuple(sorted(set(pages)))


def load_document_sidecars(
    output_root: str | Path, pages: Iterable[int] | None = None
) -> dict[int, PageArtifacts]:
    page_numbers = discover_page_numbers(output_root) if pages is None else tuple(pages)
    return {
        page: load_page_artifacts(output_root, page)
        for page in sorted(set(page_numbers))
    }


def _variant_for_group(
    document: str,
    page: int,
    legend_entry_id: str,
    crop: str,
    symbol_types: Sequence[Any],
    instances: Sequence[Any],
) -> CatalogVariant:
    type_ids = tuple(sorted(item.id for item in symbol_types))
    instance_ids = tuple(sorted(item.id for item in instances))
    evidence_by_json = {
        json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True): item
        for symbol_type in symbol_types
        for item in symbol_type.classification_evidence
    }
    evidence = tuple(evidence_by_json[key] for key in sorted(evidence_by_json))
    variant_id = _catalog_id("variant", document, page, legend_entry_id, type_ids)
    return CatalogVariant(
        id=variant_id.replace("PSC-", "PSV-", 1),
        document=document,
        page=page,
        legend_entry_id=legend_entry_id,
        symbol_type_ids=type_ids,
        instance_ids=instance_ids,
        crop=crop,
        provenance=evidence,
    )


def build_catalog(
    artifacts_by_page: Mapping[int, PageArtifacts], *, document: str
) -> DocumentSymbolCatalog:
    """Aggregate confirmed post-S6 bindings without changing page artifacts."""

    _required_string(document, "document")
    grouped: dict[str, dict[str, Any]] = {}
    unresolved: dict[str, CatalogUnresolved] = {}

    for page, artifacts in sorted(artifacts_by_page.items()):
        if artifacts.page != page:
            raise SchemaError("artifact map key does not match artifact page")
        entries = {item.id: item for item in artifacts.legend_entries}
        instances_by_type: dict[str, list[Any]] = {}
        for instance in artifacts.symbol_instances:
            if instance.symbol_type_id:
                instances_by_type.setdefault(instance.symbol_type_id, []).append(instance)

        confirmed_by_legend: dict[str, list[Any]] = {}
        for symbol_type in sorted(artifacts.symbol_types, key=lambda item: item.id):
            legend_id = symbol_type.matched_legend_entry_id
            members = instances_by_type.get(symbol_type.id, [])
            confirmed_members = [
                item
                for item in members
                if item.status == "confirmed" and item.legend_entry_id == legend_id
            ]
            if (
                symbol_type.status == "confirmed"
                and legend_id in entries
                and confirmed_members
            ):
                confirmed_by_legend.setdefault(legend_id, []).append(symbol_type)
                for member in members:
                    if member not in confirmed_members:
                        unresolved[member.id] = CatalogUnresolved(
                            id=member.id,
                            page=page,
                            status=member.status,
                            entity_ids=(member.id,),
                            candidate_legend_entry_ids=(
                                (member.legend_entry_id,)
                                if member.legend_entry_id is not None
                                else ()
                            ),
                        )
                continue
            entity_ids = (symbol_type.id, *(item.id for item in members))
            unresolved[symbol_type.id] = CatalogUnresolved(
                id=symbol_type.id,
                page=page,
                status=symbol_type.status,
                entity_ids=tuple(dict.fromkeys(entity_ids)),
                candidate_legend_entry_ids=symbol_type.candidate_legend_entry_ids,
            )

        for legend_id, symbol_types in sorted(confirmed_by_legend.items()):
            legend = entries[legend_id]
            name = legend.name_raw or legend.name_normalized
            if not name:
                for symbol_type in symbol_types:
                    unresolved[symbol_type.id] = CatalogUnresolved(
                        id=symbol_type.id,
                        page=page,
                        status="unresolved",
                        entity_ids=(symbol_type.id, *symbol_type.instance_ids),
                        candidate_legend_entry_ids=(legend_id,),
                        reason="confirmed binding has no document-provided name",
                    )
                continue
            normalized_name = (legend.name_normalized or name).strip().casefold()
            members = [
                item
                for symbol_type in symbol_types
                for item in instances_by_type.get(symbol_type.id, [])
                if item.status == "confirmed" and item.legend_entry_id == legend_id
            ]
            variant = _variant_for_group(
                document,
                page,
                legend_id,
                legend.normalized_crop or legend.raw_crop,
                symbol_types,
                members,
            )
            bucket = grouped.setdefault(
                normalized_name,
                {"names": set(), "aliases": set(), "variants": []},
            )
            bucket["names"].add(name)
            if legend.name_raw and legend.name_raw != name:
                bucket["aliases"].add(legend.name_raw)
            bucket["variants"].append(variant)

        represented_unresolved = {
            entity_id
            for item in unresolved.values()
            for entity_id in item.entity_ids
        }
        for conflict in artifacts.conflicts:
            if conflict.id not in unresolved:
                unresolved[conflict.id] = CatalogUnresolved(
                    id=conflict.id,
                    page=page,
                    status=conflict.status,
                    entity_ids=conflict.entity_ids,
                    candidate_legend_entry_ids=conflict.candidate_legend_entry_ids,
                    reason=conflict.reason,
                )
                represented_unresolved.update(conflict.entity_ids)
        for instance in artifacts.unclassified_symbols:
            if instance.id not in represented_unresolved:
                unresolved[instance.id] = CatalogUnresolved(
                    id=instance.id,
                    page=page,
                    status=instance.status,
                    entity_ids=(instance.id,),
                )
        for legend in artifacts.unmatched_legend_entries:
            if legend.id not in unresolved:
                unresolved[legend.id] = CatalogUnresolved(
                    id=legend.id,
                    page=page,
                    status="unmatched",
                    entity_ids=(legend.id,),
                    candidate_legend_entry_ids=(legend.id,),
                    reason="document legend entry has no confirmed symbol instance",
                )

    catalog_entries = []
    for normalized_name, bucket in sorted(grouped.items()):
        names = sorted(bucket["names"], key=lambda value: (value.casefold(), value))
        canonical_name = names[0]
        aliases = tuple(
            sorted(
                (set(names[1:]) | bucket["aliases"]) - {canonical_name},
                key=lambda value: (value.casefold(), value),
            )
        )
        variants = tuple(
            sorted(bucket["variants"], key=lambda item: (item.page, item.id))
        )
        catalog_entries.append(
            CatalogEntry(
                catalog_id=_catalog_id("entry", document, normalized_name),
                canonical_name=canonical_name,
                aliases=aliases,
                variants=variants,
            )
        )
    return DocumentSymbolCatalog(
        document=document,
        entries=tuple(sorted(catalog_entries, key=lambda item: item.catalog_id)),
        unresolved=tuple(
            sorted(unresolved.values(), key=lambda item: (item.page, item.id))
        ),
    )


def catalog_path(output_root: str | Path) -> Path:
    return Path(output_root) / "symbols" / CATALOG_FILENAME


def write_document_catalog(
    output_root: str | Path, catalog: DocumentSymbolCatalog
) -> Path:
    destination = catalog_path(output_root)
    atomic_write_json(destination, catalog.to_dict())
    return destination


def load_document_catalog(output_root: str | Path) -> DocumentSymbolCatalog:
    source = catalog_path(output_root)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError(f"cannot read {source}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise SchemaError("document catalog root must be an object")
    return DocumentSymbolCatalog.from_dict(data)


def finalize_document_catalog(
    output_root: str | Path,
    document: str,
    *,
    pages: Iterable[int] | None = None,
    apply_review: bool = True,
) -> DocumentSymbolCatalog:
    catalog = build_catalog(
        load_document_sidecars(output_root, pages),
        document=document,
    )
    if apply_review:
        from .review import apply_review_decisions, load_review_decisions

        decisions = load_review_decisions(output_root, missing_ok=True)
        catalog = apply_review_decisions(catalog, decisions.decisions)
    write_document_catalog(output_root, catalog)
    return catalog
