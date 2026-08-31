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


_PROJECT_CONFIDENCE = 1.0
_LOCAL_LEGEND_KINDS = {"dwg_vector_legend"}
_OPEN_STATUSES = {"unresolved", "unclassified"}
_PROJECT_EVIDENCE = "project_exact_block_definition"


def catalog_from_legend_entries(
    payload: dict[str, Any],
    *,
    source_file: str,
) -> dict[str, Any]:
    """Wrap a ready sheet ``legend_entries.json`` as a project catalog.

    Only rows with a block definition are kept. Hatch, wells and other
    rows without ``referenceSignatures`` stay out of the index.
    """

    items = payload.get("items") or payload.get("entries") or []
    entries: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        refs = [
            str(signature)
            for signature in item.get("referenceSignatures") or []
            if str(signature).startswith("blockdef-")
        ]
        if not refs:
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        entries.append(
            {
                "id": str(item["id"]),
                "label": label,
                "status": "consistent",
                "independentSourceFileCount": 1,
                "referenceSignatures": refs,
                "sourceFiles": [source_file],
            }
        )
    return {"entries": entries}


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
            text = str(signature)
            if not text.startswith("blockdef-"):
                continue
            candidates[text].append(entry)
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
        if instance.status in _OPEN_STATUSES:
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


def _has_project_evidence(binding: SymbolBinding) -> bool:
    return any(item.kind == _PROJECT_EVIDENCE for item in binding.evidence)


def _strip_project_matches(result: PageResult) -> None:
    """Drop a previous project apply so the same sidecar can be lifted again."""

    project_legend_ids = {
        entry.id
        for entry in result.legend_entries
        if entry.source_kind == "project_legend_catalog"
    }
    keep_bindings: list[SymbolBinding] = []
    reset_ids: set[str] = set()
    for binding in result.symbol_bindings:
        if _has_project_evidence(binding) or binding.legend_entry_id in project_legend_ids:
            reset_ids.add(binding.instance_id)
            continue
        keep_bindings.append(binding)
    result.legend_entries = [
        entry
        for entry in result.legend_entries
        if entry.id not in project_legend_ids
    ]
    result.symbol_bindings = keep_bindings
    result.symbol_instances = [
        replace(
            instance,
            status="unresolved",
            legend_entry_id=None,
            symbol_type_id=None,
            confidence=0.0,
        )
        if (
            instance.id in reset_ids
            and instance.role == "field_candidate"
            and instance.status in {"probable", "confirmed"}
        )
        else instance
        for instance in result.symbol_instances
    ]
    result.anomaly_codes = [
        code
        for code in result.anomaly_codes
        if code
        not in {
            "PROJECT_LEGEND_EXACT_APPLIED",
            "PROJECT_LEGEND_NO_EXACT_BINDINGS",
            "PROJECT_LEGEND_STAGE_NOT_RUN",
        }
    ]


def resolve_project_exact_blocks(
    result: PageResult,
    catalog: dict[str, Any],
) -> PageResult:
    """Confirm field INSERTs that share a unique catalog block definition.

    Local H4 bindings stay authoritative. A local legend on this sheet is not
    required. Hatch, wells and other geometry without a catalog block stay
    unread. No IGR exemplar is created.
    """

    _strip_project_matches(result)
    signature_index = _project_signature_index(catalog)
    local_signatures = {
        signature
        for entry in result.legend_entries
        if entry.source_kind in _LOCAL_LEGEND_KINDS
        for signature in entry.reference_signatures
    }
    used_entries: dict[str, LegendEntry] = {}
    added_bindings: list[SymbolBinding] = []
    updated_instances: list[SymbolInstance] = []

    for instance in result.symbol_instances:
        if (
            instance.role != "field_candidate"
            or instance.status not in _OPEN_STATUSES
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
            status="confirmed",
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
                status="confirmed",
                confidence=_PROJECT_CONFIDENCE,
                evidence=(
                    Evidence(
                        kind=_PROJECT_EVIDENCE,
                        score=_PROJECT_CONFIDENCE,
                        source_ids=(legend_entry_id, project_entry_id),
                        detail=(
                            "Field INSERT has the same unique catalog block "
                            f"definition as project legend entry {project_entry_id}. "
                            "Local legend on this sheet is not required."
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
