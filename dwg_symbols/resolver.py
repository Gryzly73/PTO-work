"""H4a: fail-closed exact block-definition resolver."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Iterable

from .schema import (
    Evidence,
    PageResult,
    SymbolBinding,
    SymbolInstance,
    UnknownSymbolCluster,
    stable_id,
)


def _inside_legend_symbol(instance: SymbolInstance, bbox: tuple[float, ...]) -> bool:
    return (
        instance.position.space == "paper"
        and instance.position.units == "mm"
        and bbox[0] <= instance.position.x < bbox[2]
        and bbox[1] <= instance.position.y < bbox[3]
    )


def _unknown_clusters(
    result: PageResult,
    instances: Iterable[SymbolInstance],
    conflicting_signatures: set[str],
) -> list[UnknownSymbolCluster]:
    grouped: dict[str, list[SymbolInstance]] = defaultdict(list)
    for instance in instances:
        if instance.status in {"unresolved", "unclassified"}:
            grouped[instance.signature].append(instance)
    return [
        UnknownSymbolCluster(
            id=stable_id("US", result.document_id, result.page, signature),
            page=result.page,
            signature=signature,
            instance_ids=tuple(item.id for item in items),
            reason=(
                "AMBIGUOUS_LEGEND_BLOCK_SIGNATURE"
                if signature in conflicting_signatures
                else "NO_EXACT_LEGEND_BLOCK_MATCH_H4"
            ),
            representative_instance_id=items[0].id,
        )
        for signature, items in sorted(grouped.items())
    ]


def resolve_exact_blocks(result: PageResult) -> PageResult:
    """Bind field INSERTs only when one legend row proves one block signature."""

    if result.symbol_bindings:
        raise ValueError("exact resolver expects an unbound H3 PageResult")

    anomalies: list[str] = [
        code for code in result.anomaly_codes if code != "BLOCK_BASELINE_ONLY"
    ]
    anomalies.append("H4_EXACT_BLOCK_RESOLVER_ONLY")

    entry_hits: dict[str, list[SymbolInstance]] = {}
    instance_entries: dict[str, list[str]] = defaultdict(list)
    legends_by_id = {entry.id: entry for entry in result.legend_entries}
    for entry in result.legend_entries:
        if entry.symbol_bbox is None:
            entry_hits[entry.id] = []
            continue
        hits = [
            instance
            for instance in result.symbol_instances
            if _inside_legend_symbol(instance, entry.symbol_bbox)
        ]
        entry_hits[entry.id] = hits
        for instance in hits:
            instance_entries[instance.id].append(entry.id)

    overlap_ids = {
        instance_id
        for instance_id, entry_ids in instance_entries.items()
        if len(entry_ids) > 1
    }
    if overlap_ids:
        anomalies.append("H4_LEGEND_EXEMPLAR_OVERLAP")

    updated_legends = []
    entry_signatures: dict[str, set[str]] = {}
    for entry in result.legend_entries:
        signatures = {
            instance.signature
            for instance in entry_hits[entry.id]
            if instance.id not in overlap_ids
        }
        entry_signatures[entry.id] = signatures
        updated_legends.append(
            replace(entry, reference_signatures=tuple(sorted(signatures)))
        )
        if len(signatures) > 1:
            anomalies.append("H4_COMPOSITE_LEGEND_ENTRY")

    signature_entries: dict[str, list[str]] = defaultdict(list)
    for entry_id, signatures in entry_signatures.items():
        if len(signatures) == 1:
            signature_entries[next(iter(signatures))].append(entry_id)
    conflicting_signatures = {
        signature
        for signature, entry_ids in signature_entries.items()
        if len(entry_ids) > 1
    }
    if conflicting_signatures:
        anomalies.append("H4_BLOCK_SIGNATURE_LEGEND_CONFLICT")

    resolvable = {
        signature: entry_ids[0]
        for signature, entry_ids in signature_entries.items()
        if len(entry_ids) == 1
    }
    exemplar_ids = {
        instance.id
        for hits in entry_hits.values()
        for instance in hits
        if instance.id not in overlap_ids
    }
    exemplar_entry = {
        instance.id: entry_id
        for entry_id, hits in entry_hits.items()
        for instance in hits
        if instance.id not in overlap_ids
    }

    updated_instances: list[SymbolInstance] = []
    bindings: list[SymbolBinding] = []
    for instance in result.symbol_instances:
        if instance.id in overlap_ids:
            # It is visibly inside the legend, but row ownership is ambiguous.
            # Never reinterpret such an exemplar as a field occurrence.
            updated_instances.append(instance)
            continue
        if instance.id in exemplar_ids:
            entry_id = exemplar_entry[instance.id]
            updated_instances.append(
                replace(
                    instance,
                    status="reference",
                    role="legend_exemplar",
                    legend_entry_id=entry_id,
                    classification_reason=None,
                    symbol_type_id=stable_id(
                        "ST", result.document_id, entry_id
                    ),
                    confidence=1.0,
                )
            )
            continue
        entry_id = resolvable.get(instance.signature)
        if entry_id is None:
            updated_instances.append(instance)
            continue
        exemplar_source_ids = tuple(
            item.id for item in entry_hits[entry_id] if item.id not in overlap_ids
        )
        classified = replace(
            instance,
            status="confirmed",
            role="field_candidate",
            legend_entry_id=entry_id,
            classification_reason=None,
            symbol_type_id=stable_id("ST", result.document_id, entry_id),
            confidence=1.0,
        )
        updated_instances.append(classified)
        bindings.append(
            SymbolBinding(
                id=stable_id("SB", result.document_id, result.page, instance.id, entry_id),
                page=result.page,
                instance_id=instance.id,
                legend_entry_id=entry_id,
                status="confirmed",
                confidence=1.0,
                evidence=(
                    Evidence(
                        kind="exact_block_definition",
                        score=1.0,
                        source_ids=(entry_id, *exemplar_source_ids),
                        detail=(
                            "Field INSERT and legend exemplar have the same "
                            f"document-local block signature {instance.signature}."
                        ),
                    ),
                ),
            )
        )

    if not exemplar_ids:
        anomalies.append("H4_NO_LEGEND_BLOCK_EXEMPLARS")
    if not bindings:
        anomalies.append("H4_NO_EXACT_BLOCK_BINDINGS")

    result.legend_entries = updated_legends
    result.symbol_instances = updated_instances
    result.symbol_bindings = bindings
    result.unknown_symbols = _unknown_clusters(
        result, updated_instances, conflicting_signatures
    )
    result.anomaly_codes = sorted(set(anomalies))
    result.validate()
    return result
