"""File-level legend catalog built from every sheet of one DWG document."""

from __future__ import annotations

from collections import defaultdict
import re
from pathlib import Path
from typing import Any, Iterable

from .artifacts import _atomic_json, write_page_result
from .context_resolver import resolve_layer_context
from .geometry_resolver import resolve_geometry_profiles
from .legends import extract_legend_page, write_legend_crops
from .resolver import resolve_exact_blocks
from .schema import LegendEntry, PageResult, stable_id


def _normalise_label(label: str) -> str:
    """Make only conservative spacing/case changes for label deduplication."""

    value = " ".join(
        re.sub(r"[^\w]+", " ", label.casefold().replace("ё", "е")).split()
    )
    # CAD authors interchange "120 мм" and "120мм" freely. Keeping these as
    # separate labels creates false project conflicts for identical exemplars.
    return re.sub(r"(?<=\d)\s+(?=(?:мм|см|дм|м)\b)", "", value)


def _source_record(entry: LegendEntry) -> dict[str, Any]:
    return {
        "page": entry.page,
        "legendEntryId": entry.id,
        "label": entry.label,
        "status": entry.status,
        "sourceKind": entry.source_kind,
        "cropPath": entry.crop_path,
        "vectorSignature": entry.signature,
        "referenceSignatures": list(entry.reference_signatures),
        "geometrySignatures": list(entry.geometry_signatures),
        "confidence": entry.confidence,
    }


def build_file_legend_catalog(results: Iterable[PageResult]) -> dict[str, Any]:
    """Combine page legends and propose fail-closed cross-sheet exact matches.

    Labels are merged only after case/spacing/punctuation normalisation. Reused
    block signatures with different labels remain explicit conflicts. Candidate
    matches are recommendations and do not mutate page sidecars.
    """

    pages = sorted(results, key=lambda item: item.page)
    if not pages:
        raise ValueError("file legend catalog requires at least one page result")
    document_ids = {item.document_id for item in pages}
    document_paths = {item.document_path for item in pages}
    if len(document_ids) != 1 or len(document_paths) != 1:
        raise ValueError("all page results must belong to one DWG document")
    document_id = pages[0].document_id

    grouped: dict[str, list[LegendEntry]] = defaultdict(list)
    for result in pages:
        for entry in result.legend_entries:
            if entry.label:
                grouped[_normalise_label(entry.label)].append(entry)

    entries: list[dict[str, Any]] = []
    for normalised_label, source_entries in sorted(grouped.items()):
        labels = sorted({entry.label for entry in source_entries if entry.label})
        catalog_id = stable_id("FLE", document_id, normalised_label)
        entries.append(
            {
                "id": catalog_id,
                "label": labels[0],
                "labelVariants": labels,
                "normalisedLabel": normalised_label,
                "status": "consistent",
                "sourcePages": sorted({entry.page for entry in source_entries}),
                "sources": [
                    _source_record(entry)
                    for entry in sorted(source_entries, key=lambda item: (item.page, item.id))
                ],
                "vectorSignatures": sorted(
                    {entry.signature for entry in source_entries if entry.signature}
                ),
                "referenceSignatures": sorted(
                    {
                        signature
                        for entry in source_entries
                        for signature in entry.reference_signatures
                    }
                ),
                "geometrySignatures": sorted(
                    {
                        signature
                        for entry in source_entries
                        for signature in entry.geometry_signatures
                    }
                ),
            }
        )

    entry_by_id = {entry["id"]: entry for entry in entries}
    signature_entries: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        for signature in entry["referenceSignatures"]:
            signature_entries[signature].add(entry["id"])

    conflicts: list[dict[str, Any]] = []
    conflicting_entry_ids: set[str] = set()
    for signature, entry_ids in sorted(signature_entries.items()):
        if len(entry_ids) < 2:
            continue
        ordered_ids = sorted(entry_ids)
        conflicting_entry_ids.update(ordered_ids)
        conflicts.append(
            {
                "kind": "reference_signature",
                "signature": signature,
                "catalogEntryIds": ordered_ids,
                "labels": [entry_by_id[item_id]["label"] for item_id in ordered_ids],
                "resolution": "blocked",
            }
        )
    for entry_id in conflicting_entry_ids:
        entry_by_id[entry_id]["status"] = "conflicting"

    resolvable_signatures = {
        signature: next(iter(entry_ids))
        for signature, entry_ids in signature_entries.items()
        if len(entry_ids) == 1 and next(iter(entry_ids)) not in conflicting_entry_ids
    }
    candidates: list[dict[str, Any]] = []
    for result in pages:
        for instance in result.symbol_instances:
            if (
                instance.role != "field_candidate"
                or instance.status not in {"unresolved", "unclassified"}
                or instance.source_kind != "dwg_insert_candidate"
            ):
                continue
            catalog_entry_id = resolvable_signatures.get(instance.signature)
            if catalog_entry_id is None:
                continue
            catalog_entry = entry_by_id[catalog_entry_id]
            source_pages = catalog_entry["sourcePages"]
            evidence_pages = [page for page in source_pages if page != result.page]
            if not evidence_pages:
                continue
            candidates.append(
                {
                    "page": result.page,
                    "instanceId": instance.id,
                    "sourceKey": f"{instance.source_space}|{instance.source_handle}",
                    "catalogEntryId": catalog_entry_id,
                    "label": catalog_entry["label"],
                    "status": "probable",
                    "confidence": 0.9,
                    "evidence": {
                        "kind": "file_exact_block_definition",
                        "signature": instance.signature,
                        "legendSourcePages": evidence_pages,
                        "detail": (
                            "The unresolved INSERT has the same document-local block "
                            "definition as a unique legend entry on another sheet."
                        ),
                    },
                }
            )

    local_bindings = sum(len(result.symbol_bindings) for result in pages)
    legend_occurrences = sum(len(result.legend_entries) for result in pages)
    return {
        "schemaVersion": 1,
        "documentId": document_id,
        "documentPath": pages[0].document_path,
        "pageCount": len(pages),
        "counts": {
            "legendOccurrences": legend_occurrences,
            "uniqueLegendEntries": len(entries),
            "duplicateLegendOccurrences": legend_occurrences - len(entries),
            "conflicts": len(conflicts),
            "localBindings": local_bindings,
            "crossSheetCandidates": len(candidates),
        },
        "entries": entries,
        "conflicts": conflicts,
        "crossSheetCandidates": candidates,
        "policy": {
            "deduplication": "normalised_label",
            "automaticCrossSheetBinding": False,
            "candidateRule": (
                "unique document-local block signature proven by a legend entry "
                "on another sheet"
            ),
        },
    }


def _report_markdown(catalog: dict[str, Any], pages: list[dict[str, Any]]) -> str:
    counts = catalog["counts"]
    lines = [
        "# Объединённая легенда DWG-файла",
        "",
        f"- Файл: `{catalog['documentPath']}`",
        f"- Обработано листов: {catalog['pageCount']}",
        f"- Пунктов легенд на листах: {counts['legendOccurrences']}",
        f"- Уникальных пунктов после объединения: {counts['uniqueLegendEntries']}",
        f"- Повторных вхождений: {counts['duplicateLegendOccurrences']}",
        f"- Конфликтов сигнатур: {counts['conflicts']}",
        f"- Локальных bindings H4c: {counts['localBindings']}",
        f"- Новых межлистовых кандидатов: {counts['crossSheetCandidates']}",
        "",
        "> Межлистовые совпадения имеют статус `probable` и не изменяют листовые "
        "sidecar-файлы без ручной проверки.",
        "",
        "## Обработанные листы",
        "",
    ]
    for page in pages:
        lines.append(
            f"- Лист {page['page']}: `{page['name']}` — "
            f"{page['legendEntries']} пунктов легенды, "
            f"{page['bindings']} локальных bindings."
        )
    lines.extend(["", "## Уникальные пункты", ""])
    for entry in catalog["entries"]:
        sources = ", ".join(str(page) for page in entry["sourcePages"])
        lines.extend(
            [
                f"### {entry['label']}",
                "",
                f"- Статус: `{entry['status']}`",
                f"- Листы-источники: {sources}",
                f"- Векторных сигнатур: {len(entry['vectorSignatures'])}",
                f"- Блочных сигнатур: {len(entry['referenceSignatures'])}",
                f"- Геометрических сигнатур: {len(entry['geometrySignatures'])}",
                "",
            ]
        )
    if catalog["conflicts"]:
        lines.extend(["## Конфликты", ""])
        for conflict in catalog["conflicts"]:
            lines.append(
                f"- `{conflict['signature']}`: "
                + " / ".join(conflict["labels"])
                + " — автоматическое применение заблокировано."
            )
        lines.append("")
    if catalog["crossSheetCandidates"]:
        lines.extend(["## Межлистовые кандидаты", ""])
        for candidate in catalog["crossSheetCandidates"]:
            lines.append(
                f"- Лист {candidate['page']}, `{candidate['sourceKey']}` → "
                f"«{candidate['label']}» (`probable`)."
            )
        lines.append("")
    return "\n".join(lines)


def review_file_legends(
    drawing: str | Path,
    output: str | Path,
    fixture_id: str,
) -> dict[str, Any]:
    """Run H2-H4c for every sheet and write one file-level legend catalog."""

    from dwg_sheets import sheets_for

    source = Path(drawing)
    destination = Path(output) / fixture_id
    _, sheets = sheets_for(source)
    if not sheets:
        raise ValueError(f"{source}: DWG contains no sheets")

    results: list[PageResult] = []
    page_summaries: list[dict[str, Any]] = []
    for page, sheet in enumerate(sheets, start=1):
        extraction = extract_legend_page(source, page)
        result = resolve_exact_blocks(extraction.page_result)
        result = resolve_geometry_profiles(result, extraction.primitives)
        result = resolve_layer_context(result, extraction.primitives)
        page_dir = write_page_result(destination, result)
        write_legend_crops(page_dir, extraction.crops)
        results.append(result)
        page_summaries.append(
            {
                "page": page,
                "name": sheet.name,
                "layoutName": sheet.layout_name,
                "legendEntries": len(result.legend_entries),
                "bindings": len(result.symbol_bindings),
                "unknownClusters": len(result.unknown_symbols),
                "completeness": result.completeness,
                "anomalyCodes": sorted(set(result.anomaly_codes)),
            }
        )

    catalog = build_file_legend_catalog(results)
    catalog["pages"] = page_summaries
    _atomic_json(destination / "file_legend_catalog.json", catalog)
    report = _report_markdown(catalog, page_summaries)
    report_path = destination / "FILE_LEGEND_REPORT.md"
    report_path.write_text(report + "\n", encoding="utf-8", newline="\n")
    return catalog
