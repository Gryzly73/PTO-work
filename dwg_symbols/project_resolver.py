"""Conservative cross-file resolver backed by the project legend catalog."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from .schema import (
    Evidence,
    LegendEntry,
    PageResult,
    SymbolBinding,
    SymbolInstance,
    UnknownSymbolCluster,
    stable_id,
)


_PROJECT_CONFIDENCE = 0.75


def _project_signature_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return only unique, non-conflicting, independently sourced signatures."""

    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in catalog.get("entries", []):
        if (
            entry.get("status") != "consistent"
            or int(entry.get("independentSourceFileCount", 0)) < 1
        ):
            continue
        for signature in entry.get("referenceSignatures", []):
            candidates[str(signature)].append(entry)
    return {
        signature: entries[0]
        for signature, entries in candidates.items()
        if len(entries) == 1
    }


def _unknown_clusters(
    result: PageResult,
    instances: list[SymbolInstance],
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
            reason="NO_PROJECT_LEGEND_BLOCK_MATCH",
            representative_instance_id=items[0].id,
        )
        for signature, items in sorted(grouped.items())
    ]


def resolve_project_exact_blocks(
    result: PageResult,
    catalog: dict[str, Any],
) -> PageResult:
    """Add probable bindings for exact block signatures proven elsewhere.

    Local H4 bindings remain unchanged and authoritative. Project evidence can
    classify only still-unresolved field INSERTs and never produces confirmed.
    """

    signature_index = _project_signature_index(catalog)
    local_signatures = {
        signature
        for entry in result.legend_entries
        for signature in entry.reference_signatures
    }
    used_entries: dict[str, LegendEntry] = {}
    added_bindings: list[SymbolBinding] = []
    updated_instances: list[SymbolInstance] = []

    for instance in result.symbol_instances:
        if (
            instance.role != "field_candidate"
            or instance.status not in {"unresolved", "unclassified"}
            or instance.source_kind != "dwg_insert_candidate"
            or instance.signature in local_signatures
        ):
            updated_instances.append(instance)
            continue
        project_entry = signature_index.get(instance.signature)
        if project_entry is None:
            updated_instances.append(instance)
            continue

        project_entry_id = str(project_entry["id"])
        legend_entry_id = stable_id(
            "LE",
            result.document_id,
            result.page,
            "project",
            project_entry_id,
        )
        if legend_entry_id not in used_entries:
            used_entries[legend_entry_id] = LegendEntry(
                id=legend_entry_id,
                page=result.page,
                label=str(project_entry["label"]),
                status="extracted",
                source_kind="project_legend_catalog",
                reference_signatures=tuple(
                    sorted(str(item) for item in project_entry["referenceSignatures"])
                ),
                source_texts=tuple(
                    str(item) for item in project_entry.get("sourceFiles", [])
                ),
                confidence=_PROJECT_CONFIDENCE,
            )

        classified = replace(
            instance,
            status="probable",
            legend_entry_id=legend_entry_id,
            symbol_type_id=stable_id("ST", "project", project_entry_id),
            confidence=_PROJECT_CONFIDENCE,
        )
        updated_instances.append(classified)
        added_bindings.append(
            SymbolBinding(
                id=stable_id(
                    "SB",
                    result.document_id,
                    result.page,
                    instance.id,
                    legend_entry_id,
                    "project",
                ),
                page=result.page,
                instance_id=instance.id,
                legend_entry_id=legend_entry_id,
                status="probable",
                confidence=_PROJECT_CONFIDENCE,
                evidence=(
                    Evidence(
                        kind="project_exact_block_definition",
                        score=_PROJECT_CONFIDENCE,
                        source_ids=(legend_entry_id, project_entry_id),
                        detail=(
                            "The unresolved field INSERT has the same normalized "
                            "block definition as one unique, non-conflicting project "
                            f"legend entry {project_entry_id}. Cross-file evidence is "
                            "limited to probable until human review."
                        ),
                    ),
                ),
            )
        )

    result.legend_entries.extend(used_entries.values())
    result.symbol_instances = updated_instances
    result.symbol_bindings.extend(added_bindings)
    result.unknown_symbols = _unknown_clusters(result, updated_instances)
    result.anomaly_codes = [
        code for code in result.anomaly_codes if code != "PROJECT_LEGEND_STAGE_NOT_RUN"
    ]
    result.anomaly_codes.append(
        "PROJECT_LEGEND_EXACT_APPLIED"
        if added_bindings
        else "PROJECT_LEGEND_NO_EXACT_BINDINGS"
    )
    result.anomaly_codes = sorted(set(result.anomaly_codes))
    result.validate()
    return result
