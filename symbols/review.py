"""Append-only human review decisions and deterministic catalog projection."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .artifacts import atomic_write_json
from .catalog import (
    CATALOG_SCHEMA_VERSION,
    CatalogEntry,
    CatalogUnresolved,
    CatalogVariant,
    DocumentSymbolCatalog,
)
from .schema import ClassificationEvidence, SchemaError

REVIEW_SCHEMA_VERSION = 1
REVIEW_FILENAME = "review_decisions.json"
DECISION_KINDS = {
    "confirm_mapping",
    "remove_mapping",
    "split_type",
    "merge_type",
    "not_a_symbol",
    "unresolved",
}


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


def _safe_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError("payload must be an object")
    result = dict(value)
    try:
        json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"payload must be JSON-safe: {exc}") from exc
    return result


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    id: str
    kind: str
    target_entity_ids: tuple[str, ...]
    payload: Mapping[str, Any]
    actor: str
    decided_at: str
    reason: str
    supersedes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_string(self.id, "id")
        if self.kind not in DECISION_KINDS:
            raise SchemaError(f"unsupported review decision kind {self.kind!r}")
        targets = _string_tuple(self.target_entity_ids, "targetEntityIds")
        if not targets:
            raise SchemaError("targetEntityIds must not be empty")
        object.__setattr__(self, "target_entity_ids", targets)
        object.__setattr__(self, "payload", _safe_payload(self.payload))
        _required_string(self.actor, "actor")
        _required_string(self.decided_at, "decidedAt")
        _required_string(self.reason, "reason")
        object.__setattr__(
            self, "supersedes", _string_tuple(self.supersedes, "supersedes")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "targetEntityIds": list(self.target_entity_ids),
            "payload": dict(self.payload),
            "actor": self.actor,
            "decidedAt": self.decided_at,
            "reason": self.reason,
            "supersedes": list(self.supersedes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewDecision":
        expected = {
            "id", "kind", "targetEntityIds", "payload", "actor", "decidedAt",
            "reason", "supersedes",
        }
        if set(data) != expected:
            raise SchemaError("invalid review decision fields")
        return cls(
            id=_required_string(data["id"], "id"),
            kind=_required_string(data["kind"], "kind"),
            target_entity_ids=_string_tuple(
                data["targetEntityIds"], "targetEntityIds"
            ),
            payload=_safe_payload(data["payload"]),
            actor=_required_string(data["actor"], "actor"),
            decided_at=_required_string(data["decidedAt"], "decidedAt"),
            reason=_required_string(data["reason"], "reason"),
            supersedes=_string_tuple(data["supersedes"], "supersedes"),
        )


@dataclass(frozen=True, slots=True)
class ReviewDecisionLog:
    decisions: tuple[ReviewDecision, ...] = ()
    schema_version: int = REVIEW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != REVIEW_SCHEMA_VERSION:
            raise SchemaError(
                f"unsupported review schemaVersion {self.schema_version!r}"
            )
        decisions = tuple(self.decisions)
        if any(not isinstance(item, ReviewDecision) for item in decisions):
            raise SchemaError("decisions must contain ReviewDecision objects")
        if len({item.id for item in decisions}) != len(decisions):
            raise SchemaError("review decision IDs must be unique")
        object.__setattr__(
            self,
            "decisions",
            tuple(sorted(decisions, key=lambda item: (item.decided_at, item.id))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "kind": "reviewDecisionLog",
            "decisions": [item.to_dict() for item in self.decisions],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewDecisionLog":
        if set(data) != {"schemaVersion", "kind", "decisions"}:
            raise SchemaError("invalid review decision log envelope")
        if data["kind"] != "reviewDecisionLog" or not isinstance(
            data["decisions"], list
        ):
            raise SchemaError("invalid review decision log")
        return cls(
            schema_version=data["schemaVersion"],
            decisions=tuple(
                ReviewDecision.from_dict(item) for item in data["decisions"]
            ),
        )


def make_decision_id(
    kind: str,
    target_entity_ids: Iterable[str],
    *,
    actor: str,
    decided_at: str,
    payload: Mapping[str, Any] | None = None,
) -> str:
    value = [
        kind,
        sorted(target_entity_ids),
        actor,
        decided_at,
        dict(payload or {}),
    ]
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"RD-{hashlib.sha256(encoded).hexdigest()[:12]}"


def review_path(output_root: str | Path) -> Path:
    return Path(output_root) / "symbols" / REVIEW_FILENAME


def load_review_decisions(
    output_root: str | Path, *, missing_ok: bool = False
) -> ReviewDecisionLog:
    source = review_path(output_root)
    if missing_ok and not source.exists():
        return ReviewDecisionLog()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError(f"cannot read {source}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise SchemaError("review decision log root must be an object")
    return ReviewDecisionLog.from_dict(data)


def write_review_decisions(
    output_root: str | Path, log: ReviewDecisionLog
) -> Path:
    destination = review_path(output_root)
    atomic_write_json(destination, log.to_dict())
    return destination


def append_review_decision(
    output_root: str | Path, decision: ReviewDecision
) -> ReviewDecisionLog:
    """Append one immutable decision and atomically rewrite the ordered audit log."""

    log = load_review_decisions(output_root, missing_ok=True)
    if decision.id in {item.id for item in log.decisions}:
        raise SchemaError(f"duplicate review decision ID {decision.id!r}")
    updated = ReviewDecisionLog((*log.decisions, decision))
    write_review_decisions(output_root, updated)
    return updated


def _entry_targets(entry: CatalogEntry) -> set[str]:
    return {
        entry.catalog_id,
        *(variant.id for variant in entry.variants),
        *(identifier for variant in entry.variants for identifier in variant.symbol_type_ids),
        *(identifier for variant in entry.variants for identifier in variant.instance_ids),
        *(variant.legend_entry_id for variant in entry.variants),
    }


def _with_decision(entry: CatalogEntry, decision_id: str) -> CatalogEntry:
    return replace(
        entry,
        applied_decision_ids=tuple(
            dict.fromkeys((*entry.applied_decision_ids, decision_id))
        ),
    )


def _human_variant(decision: ReviewDecision, document: str) -> CatalogVariant:
    payload = decision.payload
    page = payload.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise SchemaError("confirm_mapping payload.page must be a positive integer")
    legend_id = _required_string(payload.get("legendEntryId"), "payload.legendEntryId")
    crop = _required_string(payload.get("crop"), "payload.crop")
    symbol_type_ids = _string_tuple(
        payload.get("symbolTypeIds", decision.target_entity_ids), "payload.symbolTypeIds"
    )
    instance_ids = _string_tuple(
        payload.get("instanceIds", ()), "payload.instanceIds"
    )
    variant_id = payload.get("variantId") or f"PSV-{decision.id}"
    evidence = ClassificationEvidence(
        kind="human_review",
        score=1.0,
        source_entity_ids=decision.target_entity_ids,
        detail=f"{decision.actor}: {decision.reason}",
    )
    return CatalogVariant(
        id=_required_string(variant_id, "payload.variantId"),
        document=document,
        page=page,
        legend_entry_id=legend_id,
        symbol_type_ids=symbol_type_ids,
        instance_ids=instance_ids,
        crop=crop,
        provenance=(evidence,),
    )


def _apply_confirm(
    entries: list[CatalogEntry],
    unresolved: list[CatalogUnresolved],
    decision: ReviewDecision,
    document: str,
) -> tuple[list[CatalogEntry], list[CatalogUnresolved]]:
    variant = _human_variant(decision, document)
    name = _required_string(decision.payload.get("canonicalName"), "payload.canonicalName")
    requested_id = decision.payload.get("catalogId")
    target_entry = next(
        (
            entry
            for entry in entries
            if (requested_id and entry.catalog_id == requested_id)
            or bool(_entry_targets(entry) & set(decision.target_entity_ids))
        ),
        None,
    )
    if target_entry is None:
        catalog_id = _required_string(
            requested_id or f"PSC-{decision.id}", "payload.catalogId"
        )
        entries.append(
            CatalogEntry(
                catalog_id=catalog_id,
                canonical_name=name,
                aliases=tuple(decision.payload.get("aliases", ())),
                variants=(variant,),
                review_status="human_confirmed",
                applied_decision_ids=(decision.id,),
            )
        )
    else:
        entries = [
            replace(
                entry,
                canonical_name=name,
                aliases=tuple(decision.payload.get("aliases", entry.aliases)),
                variants=tuple(
                    sorted(
                        {
                            item.id: item
                            for item in (*entry.variants, variant)
                        }.values(),
                        key=lambda item: (item.page, item.id),
                    )
                ),
                review_status="human_confirmed",
                applied_decision_ids=tuple(
                    dict.fromkeys((*entry.applied_decision_ids, decision.id))
                ),
            )
            if entry.catalog_id == target_entry.catalog_id
            else entry
            for entry in entries
        ]
    targets = set(decision.target_entity_ids)
    unresolved = [
        item for item in unresolved if not (set(item.entity_ids) & targets)
    ]
    return entries, unresolved


def _apply_remove(
    entries: list[CatalogEntry],
    unresolved: list[CatalogUnresolved],
    decision: ReviewDecision,
    *,
    status: str,
) -> tuple[list[CatalogEntry], list[CatalogUnresolved]]:
    targets = set(decision.target_entity_ids)
    unresolved = [
        replace(
            item,
            status=status,
            reason=decision.reason,
            applied_decision_ids=tuple(
                dict.fromkeys((*item.applied_decision_ids, decision.id))
            ),
        )
        if item.id in targets or set(item.entity_ids) & targets
        else item
        for item in unresolved
    ]
    kept: list[CatalogEntry] = []
    removed_variants: list[CatalogVariant] = []
    for entry in entries:
        if entry.catalog_id in targets:
            removed_variants.extend(entry.variants)
            continue
        variants = tuple(
            variant
            for variant in entry.variants
            if not (
                {
                    variant.id,
                    variant.legend_entry_id,
                    *variant.symbol_type_ids,
                    *variant.instance_ids,
                }
                & targets
            )
        )
        removed_variants.extend(
            variant for variant in entry.variants if variant not in variants
        )
        if variants:
            kept.append(
                _with_decision(replace(entry, variants=variants), decision.id)
                if variants != entry.variants
                else entry
            )
    for variant in removed_variants:
        unresolved.append(
            CatalogUnresolved(
                id=f"{status}:{variant.id}",
                page=variant.page,
                status=status,
                entity_ids=tuple(
                    dict.fromkeys(
                        (
                            variant.id,
                            *variant.symbol_type_ids,
                            *variant.instance_ids,
                        )
                    )
                ),
                candidate_legend_entry_ids=(variant.legend_entry_id,),
                reason=decision.reason,
                applied_decision_ids=(decision.id,),
            )
        )
    return kept, unresolved


def _apply_merge(entries: list[CatalogEntry], decision: ReviewDecision) -> list[CatalogEntry]:
    targets = set(decision.target_entity_ids)
    selected = [entry for entry in entries if _entry_targets(entry) & targets]
    if len(selected) < 2:
        return entries
    destination_id = _required_string(
        decision.payload.get("catalogId", selected[0].catalog_id),
        "payload.catalogId",
    )
    name = _required_string(
        decision.payload.get("canonicalName", selected[0].canonical_name),
        "payload.canonicalName",
    )
    variants = tuple(
        sorted(
            {
                variant.id: variant
                for entry in selected
                for variant in entry.variants
            }.values(),
            key=lambda item: (item.page, item.id),
        )
    )
    aliases = tuple(
        sorted(
            {
                *(entry.canonical_name for entry in selected),
                *(alias for entry in selected for alias in entry.aliases),
            }
            - {name},
            key=lambda value: (value.casefold(), value),
        )
    )
    merged = CatalogEntry(
        catalog_id=destination_id,
        canonical_name=name,
        aliases=aliases,
        variants=variants,
        review_status="human_confirmed",
        applied_decision_ids=tuple(
            dict.fromkeys(
                (
                    *(identifier for entry in selected for identifier in entry.applied_decision_ids),
                    decision.id,
                )
            )
        ),
    )
    return [entry for entry in entries if entry not in selected] + [merged]


def _apply_split(entries: list[CatalogEntry], decision: ReviewDecision) -> list[CatalogEntry]:
    source = next(
        (
            entry
            for entry in entries
            if _entry_targets(entry) & set(decision.target_entity_ids)
        ),
        None,
    )
    groups = decision.payload.get("groups")
    if source is None or not isinstance(groups, list) or not groups:
        return entries
    by_id = {variant.id: variant for variant in source.variants}
    assigned: set[str] = set()
    replacements: list[CatalogEntry] = []
    for index, group in enumerate(groups, 1):
        if not isinstance(group, Mapping):
            raise SchemaError("split_type payload.groups[] must be an object")
        variant_ids = _string_tuple(group.get("variantIds", ()), "group.variantIds")
        if assigned & set(variant_ids):
            raise SchemaError("split_type variant may occur in only one group")
        variants = tuple(by_id[item] for item in variant_ids if item in by_id)
        if not variants:
            continue
        assigned.update(item.id for item in variants)
        replacements.append(
            CatalogEntry(
                catalog_id=_required_string(
                    group.get("catalogId", f"{source.catalog_id}-S{index}"),
                    "group.catalogId",
                ),
                canonical_name=_required_string(
                    group.get("canonicalName", source.canonical_name),
                    "group.canonicalName",
                ),
                aliases=tuple(group.get("aliases", ())),
                variants=variants,
                review_status="human_confirmed",
                applied_decision_ids=tuple(
                    dict.fromkeys((*source.applied_decision_ids, decision.id))
                ),
            )
        )
    remaining = tuple(item for item in source.variants if item.id not in assigned)
    if remaining:
        replacements.append(
            _with_decision(replace(source, variants=remaining), decision.id)
        )
    return [entry for entry in entries if entry.catalog_id != source.catalog_id] + replacements


def apply_review_decisions(
    catalog: DocumentSymbolCatalog,
    decisions: Iterable[ReviewDecision],
) -> DocumentSymbolCatalog:
    """Project human decisions over a catalog; page sidecars are never written."""

    ordered = ReviewDecisionLog(tuple(decisions)).decisions
    entries = list(catalog.entries)
    unresolved = list(catalog.unresolved)
    applied_ids = list(catalog.applied_review_decision_ids)
    superseded = {identifier for item in ordered for identifier in item.supersedes}

    for decision in ordered:
        if decision.id in superseded:
            continue
        if decision.kind == "confirm_mapping":
            entries, unresolved = _apply_confirm(
                entries, unresolved, decision, catalog.document
            )
        elif decision.kind == "remove_mapping":
            entries, unresolved = _apply_remove(
                entries, unresolved, decision, status="mapping_removed"
            )
        elif decision.kind == "not_a_symbol":
            entries, unresolved = _apply_remove(
                entries, unresolved, decision, status="not_a_symbol"
            )
        elif decision.kind == "unresolved":
            entries, unresolved = _apply_remove(
                entries, unresolved, decision, status="unresolved"
            )
        elif decision.kind == "merge_type":
            entries = _apply_merge(entries, decision)
        elif decision.kind == "split_type":
            entries = _apply_split(entries, decision)
        applied_ids.append(decision.id)

    return DocumentSymbolCatalog(
        schema_version=CATALOG_SCHEMA_VERSION,
        document=catalog.document,
        entries=tuple(sorted(entries, key=lambda item: item.catalog_id)),
        unresolved=tuple(
            sorted(
                {item.id: item for item in unresolved}.values(),
                key=lambda item: (item.page, item.id),
            )
        ),
        applied_review_decision_ids=tuple(dict.fromkeys(applied_ids)),
    )
