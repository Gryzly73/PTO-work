"""Fail-closed join: unique block name → one local legend row.

H4 ``exact_block_definition`` is not used. A field INSERT is confirmed only when
its block name is in the map and exactly one extracted legend row matches the
type phrase. Ambiguous or unmapped names stay unresolved. Legend-cell sample
numbers (123, 329) are not copied onto the field.
"""

from __future__ import annotations

from collections import defaultdict
import re
from dataclasses import replace

from .schema import (
    Evidence,
    LegendEntry,
    PageResult,
    SymbolBinding,
    SymbolInstance,
    UnknownSymbolCluster,
    stable_id,
)


NAMED_BLOCK_EVIDENCE = "named_block_legend"
_OPEN_STATUSES = {"unresolved", "unclassified"}
_LEADING_SAMPLE_NO = re.compile(r"^\d+\s+")

# Ask D3 table: only these names have one unique legend row. CPE/CME/CCE/CGE
# and PR_TR are omitted on purpose — they stay unresolved.
_BLOCK_NEEDLES: dict[str, str] = {
    "pr_kv": "образец грунта с ненарушенной структурой",
    "pr_vd": "проба воды",
}


def field_legend_type_label(label: str) -> str:
    """Type text for a field occurrence: drop the legend-cell sample number."""

    return _LEADING_SAMPLE_NO.sub("", (label or "").strip())


def _folded(value: str | None) -> str:
    return (value or "").strip().casefold()


def _is_open_field(instance: SymbolInstance) -> bool:
    return instance.role == "field_candidate" and instance.status in _OPEN_STATUSES


def _unique_entry_for_needle(
    entries: list[LegendEntry],
    needle: str,
) -> LegendEntry | None:
    hits = [
        entry
        for entry in entries
        if entry.status == "extracted"
        and entry.label
        and needle in entry.label.casefold()
    ]
    if len(hits) != 1:
        return None
    return hits[0]


def _legend_index(entries: list[LegendEntry]) -> dict[str, LegendEntry]:
    """block name → the one matching legend row, or omitted if not unique."""

    index: dict[str, LegendEntry] = {}
    for block, needle in _BLOCK_NEEDLES.items():
        entry = _unique_entry_for_needle(entries, needle)
        if entry is not None:
            index[block] = entry
    return index


def _rebuild_unknown(
    result: PageResult,
    instances: list[SymbolInstance],
) -> list[UnknownSymbolCluster]:
    previous = {cluster.signature: cluster for cluster in result.unknown_symbols}
    grouped: dict[str, list[SymbolInstance]] = defaultdict(list)
    for instance in instances:
        if _is_open_field(instance):
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
                    old.reason
                    if old is not None
                    else "NO_EXACT_LEGEND_BLOCK_MATCH_H4"
                ),
                representative_instance_id=representative,
            )
        )
    return clusters


def resolve_named_block_legend(result: PageResult) -> PageResult:
    """Bind open field INSERTs whose block name has exactly one legend row."""

    index = _legend_index(result.legend_entries)
    if not index:
        return result

    updated_instances: list[SymbolInstance] = []
    added: list[SymbolBinding] = []
    for instance in result.symbol_instances:
        if not _is_open_field(instance):
            updated_instances.append(instance)
            continue
        entry = index.get(_folded(instance.block_name))
        if entry is None:
            updated_instances.append(instance)
            continue
        type_label = field_legend_type_label(entry.label or "")
        classified = replace(
            instance,
            status="confirmed",
            role="field_candidate",
            legend_entry_id=entry.id,
            classification_reason=None,
            symbol_type_id=stable_id("ST", result.document_id, entry.id),
            confidence=1.0,
        )
        updated_instances.append(classified)
        added.append(
            SymbolBinding(
                id=stable_id(
                    "SB",
                    result.document_id,
                    result.page,
                    instance.id,
                    entry.id,
                    NAMED_BLOCK_EVIDENCE,
                ),
                page=result.page,
                instance_id=instance.id,
                legend_entry_id=entry.id,
                status="confirmed",
                confidence=1.0,
                evidence=(
                    Evidence(
                        kind=NAMED_BLOCK_EVIDENCE,
                        score=1.0,
                        source_ids=(entry.id,),
                        detail=(
                            f"Field INSERT block {instance.block_name} uniquely "
                            f"matches one legend row {entry.id} ({type_label})."
                        ),
                    ),
                ),
            )
        )

    if not added:
        return result

    result.symbol_instances = updated_instances
    result.symbol_bindings.extend(added)
    result.unknown_symbols = _rebuild_unknown(result, updated_instances)
    result.validate()
    return result
