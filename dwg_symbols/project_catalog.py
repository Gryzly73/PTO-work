"""Project-wide legend catalog with source provenance and conflict blocking."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import shutil
from typing import Any, Callable, Iterable

from .artifacts import _atomic_json, write_page_result
from .context_resolver import resolve_layer_context
from .file_catalog import _normalise_label, build_file_legend_catalog
from .geometry_resolver import resolve_geometry_profiles
from .legends import extract_legend_page, has_legend_heading, write_legend_crops
from .resolver import resolve_exact_blocks
from .schema import PageResult, stable_id


_DRAWING_SUFFIXES = {".dwg", ".dxf"}


def _discipline(relative_path: Path) -> str:
    return relative_path.parts[0] if len(relative_path.parts) > 1 else "project-root"


def _is_external_reference(relative_path: Path) -> bool:
    return any("внешн" in part.casefold() and "ссыл" in part.casefold() for part in relative_path.parts)


def _artifact_crop(file_id: str, source: dict[str, Any]) -> str | None:
    crop = source.get("cropPath")
    page = source.get("page")
    if not crop or not isinstance(page, int):
        return None
    return f"documents/{file_id}/dwg_symbols/page_{page:04d}/{crop}"


def _source_fingerprint(source: Path) -> dict[str, int]:
    stat = source.stat()
    return {"sizeBytes": stat.st_size, "mtimeNs": stat.st_mtime_ns}


def build_project_legend_catalog(
    documents: Iterable[dict[str, Any]],
    *,
    package_root: str,
) -> dict[str, Any]:
    """Merge file catalogs while preserving every source and blocking conflicts."""

    records = list(documents)
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for record in records:
        catalog = record.get("catalog")
        if not isinstance(catalog, dict):
            continue
        for entry in catalog.get("entries", []):
            label = entry.get("label")
            if isinstance(label, str) and label.strip():
                grouped[_normalise_label(label)].append((record, entry))

    entries: list[dict[str, Any]] = []
    for normalised_label, occurrences in sorted(grouped.items()):
        labels = sorted(
            {
                variant
                for _, entry in occurrences
                for variant in entry.get("labelVariants", [entry.get("label")])
                if isinstance(variant, str) and variant.strip()
            }
        )
        independent_files = {
            record["relativePath"]
            for record, _ in occurrences
            if not record["externalReference"]
        }
        external_files = {
            record["relativePath"]
            for record, _ in occurrences
            if record["externalReference"]
        }
        project_entry_id = stable_id("PLE", package_root, normalised_label)
        sources: list[dict[str, Any]] = []
        for record, entry in sorted(
            occurrences,
            key=lambda item: (item[0]["relativePath"], item[1]["id"]),
        ):
            source_rows = []
            for source in entry.get("sources", []):
                source_rows.append(
                    {
                        **source,
                        "artifactCropPath": _artifact_crop(record["fileId"], source),
                    }
                )
            sources.append(
                {
                    "fileId": record["fileId"],
                    "relativePath": record["relativePath"],
                    "discipline": record["discipline"],
                    "externalReference": record["externalReference"],
                    "fileLegendEntryId": entry["id"],
                    "sourcePages": entry.get("sourcePages", []),
                    "sources": source_rows,
                }
            )
        entries.append(
            {
                "id": project_entry_id,
                "label": labels[0],
                "labelVariants": labels,
                "normalisedLabel": normalised_label,
                "status": "consistent",
                "disciplines": sorted({record["discipline"] for record, _ in occurrences}),
                "sourceFiles": sorted({record["relativePath"] for record, _ in occurrences}),
                "sourceFileCount": len({record["relativePath"] for record, _ in occurrences}),
                "independentSourceFileCount": len(independent_files),
                "externalReferenceFileCount": len(external_files),
                "sourceOccurrenceCount": sum(
                    len(entry.get("sources", [])) for _, entry in occurrences
                ),
                "vectorSignatures": sorted(
                    {
                        signature
                        for _, entry in occurrences
                        for signature in entry.get("vectorSignatures", [])
                    }
                ),
                "referenceSignatures": sorted(
                    {
                        signature
                        for _, entry in occurrences
                        for signature in entry.get("referenceSignatures", [])
                    }
                ),
                "geometrySignatures": sorted(
                    {
                        signature
                        for _, entry in occurrences
                        for signature in entry.get("geometrySignatures", [])
                    }
                ),
                "sources": sources,
            }
        )

    entry_by_id = {entry["id"]: entry for entry in entries}
    conflicts: list[dict[str, Any]] = []
    conflicting_entry_ids: set[str] = set()
    signature_fields = (
        ("reference_signature", "referenceSignatures"),
        ("geometry_signature", "geometrySignatures"),
        ("vector_signature", "vectorSignatures"),
    )
    for kind, field in signature_fields:
        signature_entries: dict[str, set[str]] = defaultdict(set)
        for entry in entries:
            for signature in entry[field]:
                signature_entries[signature].add(entry["id"])
        for signature, entry_ids in sorted(signature_entries.items()):
            if len(entry_ids) < 2:
                continue
            ordered_ids = sorted(entry_ids)
            conflicting_entry_ids.update(ordered_ids)
            conflicts.append(
                {
                    "kind": kind,
                    "signature": signature,
                    "projectEntryIds": ordered_ids,
                    "labels": [entry_by_id[item_id]["label"] for item_id in ordered_ids],
                    "resolution": "blocked",
                }
            )

    for entry in entries:
        if entry["id"] in conflicting_entry_ids:
            entry["status"] = "conflicting"
        elif entry["independentSourceFileCount"] == 0:
            entry["status"] = "external_only"
        elif not any(
            entry[field]
            for field in ("vectorSignatures", "referenceSignatures", "geometrySignatures")
        ):
            entry["status"] = "text_only"

    public_documents = [
        {key: value for key, value in record.items() if key != "catalog"}
        for record in records
    ]
    statuses = Counter(record.get("status", "unknown") for record in public_documents)
    discipline_documents = Counter(record["discipline"] for record in public_documents)
    discipline_entries = Counter(
        discipline for entry in entries for discipline in entry["disciplines"]
    )
    legend_occurrences = sum(
        int((record.get("catalog") or {}).get("counts", {}).get("legendOccurrences", 0))
        for record in records
    )
    pages_discovered = sum(int(record.get("pageCount", 0)) for record in records)
    legend_pages = sum(len(record.get("legendHeadingPages", [])) for record in records)
    conflict_kinds = Counter(conflict["kind"] for conflict in conflicts)
    return {
        "schemaVersion": 1,
        "packageRoot": package_root,
        "completeness": "partial" if statuses.get("error") or statuses.get("partial") else "complete",
        "counts": {
            "documents": len(records),
            "pagesDiscovered": pages_discovered,
            "legendPages": legend_pages,
            "legendOccurrences": legend_occurrences,
            "uniqueProjectEntries": len(entries),
            "conflicts": len(conflicts),
            "documentsWithLegend": sum(
                1
                for record in records
                if (record.get("catalog") or {}).get("entries")
            ),
            "externalOnlyEntries": sum(
                1 for entry in entries if entry["status"] == "external_only"
            ),
        },
        "documentStatuses": dict(sorted(statuses.items())),
        "disciplineDocuments": dict(sorted(discipline_documents.items())),
        "disciplineEntries": dict(sorted(discipline_entries.items())),
        "conflictsByKind": dict(sorted(conflict_kinds.items())),
        "entries": entries,
        "conflicts": conflicts,
        "documents": public_documents,
        "applicationPolicy": {
            "priority": ["sheet", "file", "discipline", "project", "external_standard"],
            "automaticCrossFileBinding": False,
            "maximumUnreviewedCrossFileStatus": "probable",
            "conflictingSignaturesBlocked": True,
            "note": (
                "Project entries are machine-extracted evidence, not human-verified "
                "ground truth. Local sheet and file legends remain authoritative."
            ),
        },
    }


def _write_project_report(path: Path, catalog: dict[str, Any]) -> None:
    counts = catalog["counts"]
    lines = [
        "# Общая легенда DWG-проекта",
        "",
        f"- Корень комплекта: `{catalog['packageRoot']}`",
        f"- Статус комплектности: `{catalog['completeness']}`",
        f"- Обнаружено DWG/DXF: {counts['documents']}",
        f"- Обнаружено листов/представлений конвейера: {counts['pagesDiscovered']}",
        f"- Листов с заголовком легенды: {counts['legendPages']}",
        f"- Извлечено вхождений пунктов: {counts['legendOccurrences']}",
        f"- Уникальных проектных пунктов: {counts['uniqueProjectEntries']}",
        f"- Файлов с извлечённой легендой: {counts['documentsWithLegend']}",
        f"- Пунктов только из `Внешние ссылки`: {counts['externalOnlyEntries']}",
        f"- Конфликтов сигнатур: {counts['conflicts']}",
        "",
        "> Каталог построен автоматически. Он не является ground truth. "
        "Легенда текущего листа и файла имеет приоритет; межфайловое совпадение "
        "без ручной проверки может иметь статус не выше `probable`.",
        "",
        "> Число представлений включает не только штатные layout, но и листы, "
        "восстановленные конвейером по рамкам или отдельным блокам без привязки.",
        "",
        "## Разделы",
        "",
    ]
    disciplines = sorted(
        set(catalog["disciplineDocuments"]) | set(catalog["disciplineEntries"])
    )
    for discipline in disciplines:
        lines.append(
            f"- `{discipline}`: "
            f"{catalog['disciplineDocuments'].get(discipline, 0)} файлов, "
            f"{catalog['disciplineEntries'].get(discipline, 0)} пунктов."
        )
    lines.extend(["", "## Уникальные пункты", ""])
    for entry in catalog["entries"]:
        lines.extend(
            [
                f"### {entry['label']}",
                "",
                f"- Статус: `{entry['status']}`",
                f"- Разделы: {', '.join(entry['disciplines'])}",
                f"- Файлов-источников: {entry['sourceFileCount']}",
                f"- Независимых/внешних файлов: "
                f"{entry['independentSourceFileCount']}/"
                f"{entry['externalReferenceFileCount']}",
                f"- Вхождений: {entry['sourceOccurrenceCount']}",
                f"- Блочных/геометрических/векторных сигнатур: "
                f"{len(entry['referenceSignatures'])}/"
                f"{len(entry['geometrySignatures'])}/"
                f"{len(entry['vectorSignatures'])}",
                "",
            ]
        )
        for source in entry["sources"]:
            pages = ", ".join(str(page) for page in source["sourcePages"])
            marker = " (XREF/внешняя ссылка)" if source["externalReference"] else ""
            lines.append(
                f"  - `{source['relativePath']}`, листы {pages}{marker};"
            )
        lines.append("")
    if catalog["conflicts"]:
        lines.extend(["## Заблокированные конфликты", ""])
        for conflict in catalog["conflicts"]:
            lines.append(
                f"- `{conflict['kind']}`: "
                + " / ".join(conflict["labels"])
                + ";"
            )
        lines.append("")
    failed = [
        document
        for document in catalog["documents"]
        if document.get("status") in {"error", "partial"}
    ]
    if failed:
        lines.extend(["## Неполностью обработанные файлы", ""])
        for document in failed:
            errors = "; ".join(document.get("errors", [])) or document["status"]
            lines.append(f"- `{document['relativePath']}`: {errors}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _process_document(
    source: Path,
    package_root: Path,
    output: Path,
    *,
    rebuild: bool,
) -> dict[str, Any]:
    from dwg_sheets import _DOC_CACHE, sheets_for

    relative = source.relative_to(package_root)
    relative_text = relative.as_posix()
    file_id = stable_id("PF", relative_text)
    destination = output / "documents" / file_id
    scan_path = destination / "document_scan.json"
    catalog_path = destination / "file_legend_catalog.json"
    fingerprint = _source_fingerprint(source)
    if not rebuild and scan_path.exists():
        record = _load_json(scan_path)
        cached_fingerprint = record.get("sourceFingerprint")
        if cached_fingerprint is None:
            # Upgrade artifacts created before source fingerprints were added.
            record["sourceFingerprint"] = fingerprint
            _atomic_json(scan_path, record)
            cached_fingerprint = fingerprint
        if cached_fingerprint == fingerprint:
            if catalog_path.exists():
                record["catalog"] = _load_json(catalog_path)
            return record

    record: dict[str, Any] = {
        "fileId": file_id,
        "relativePath": relative_text,
        "discipline": _discipline(relative),
        "externalReference": _is_external_reference(relative),
        "sourceFingerprint": fingerprint,
        "status": "error",
        "pageCount": 0,
        "legendHeadingPages": [],
        "processedLegendPages": [],
        "errors": [],
    }
    dxf_path: Path | None = None
    try:
        dxf_path, sheets = sheets_for(source)
        record["pageCount"] = len(sheets)
        heading_pages = [
            page
            for page, sheet in enumerate(sheets, start=1)
            if has_legend_heading(sheet.texts)
        ]
        record["legendHeadingPages"] = heading_pages
        if not heading_pages:
            record["status"] = "no_legend"
            _atomic_json(scan_path, record)
            return record

        results: list[PageResult] = []
        page_summaries: list[dict[str, Any]] = []
        for page in heading_pages:
            try:
                extraction = extract_legend_page(source, page)
                result = resolve_exact_blocks(extraction.page_result)
                result = resolve_geometry_profiles(result, extraction.primitives)
                result = resolve_layer_context(result, extraction.primitives)
                page_dir = write_page_result(destination, result)
                write_legend_crops(page_dir, extraction.crops)
                results.append(result)
                record["processedLegendPages"].append(page)
                sheet = sheets[page - 1]
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
            except Exception as exc:
                record["errors"].append(f"page {page}: {type(exc).__name__}: {exc}")

        if results:
            file_catalog = build_file_legend_catalog(results)
            file_catalog["projectRelativePath"] = relative_text
            file_catalog["discipline"] = record["discipline"]
            file_catalog["externalReference"] = record["externalReference"]
            file_catalog["pages"] = page_summaries
            _atomic_json(catalog_path, file_catalog)
            record["catalog"] = file_catalog
        record["status"] = (
            "partial"
            if record["errors"] or len(results) != len(heading_pages)
            else "processed"
        )
    except Exception as exc:
        record["errors"].append(f"{type(exc).__name__}: {exc}")
        record["status"] = "error"
    finally:
        _DOC_CACHE.clear()
        if (
            dxf_path is not None
            and dxf_path != source
            and dxf_path.parent.name.startswith("dwg2dxf_")
        ):
            shutil.rmtree(dxf_path.parent, ignore_errors=True)

    public_record = {key: value for key, value in record.items() if key != "catalog"}
    _atomic_json(scan_path, public_record)
    return record


def review_project_legends(
    package_root: str | Path,
    output: str | Path,
    *,
    rebuild: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Scan all project drawings and build a resumable project legend catalog."""

    source_root = Path(package_root)
    destination = Path(output)
    drawings = sorted(
        (
            path
            for path in source_root.rglob("*")
            if path.is_file() and path.suffix.casefold() in _DRAWING_SUFFIXES
        ),
        key=lambda path: path.relative_to(source_root).as_posix().casefold(),
    )
    if not drawings:
        raise ValueError(f"{source_root}: no DWG/DXF files found")

    records: list[dict[str, Any]] = []
    for index, drawing in enumerate(drawings, start=1):
        relative = drawing.relative_to(source_root).as_posix()
        if progress:
            progress(f"[{index}/{len(drawings)}] {relative}")
        record = _process_document(
            drawing,
            source_root,
            destination,
            rebuild=rebuild,
        )
        records.append(record)
        if progress:
            progress(
                f"  -> {record['status']}, "
                f"{len(record.get('legendHeadingPages', []))} legend page(s)"
            )

    catalog = build_project_legend_catalog(records, package_root=str(source_root))
    _atomic_json(destination / "project_legend_catalog.json", catalog)
    _write_project_report(destination / "PROJECT_LEGEND_REPORT.md", catalog)
    return catalog
