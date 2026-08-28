"""Apply a project legend catalog to every readable sheet in a DWG package."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Callable

from .artifacts import _atomic_json, load_page_result, write_page_result
from .blocks import extract_block_page
from .context_resolver import resolve_layer_context
from .geometry_resolver import resolve_geometry_profiles
from .legends import extract_legend_page, has_legend_heading, write_legend_crops
from .project_catalog import (
    _discipline,
    _is_external_reference,
    _source_fingerprint,
)
from .project_resolver import resolve_project_exact_blocks
from .resolver import resolve_exact_blocks
from .schema import PageResult, stable_id


_DRAWING_SUFFIXES = {".dwg", ".dxf"}


def _catalog_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding_kind(result: PageResult, binding_id: str) -> str:
    for binding in result.symbol_bindings:
        if binding.id == binding_id:
            kinds = {evidence.kind for evidence in binding.evidence}
            if "project_exact_block_definition" in kinds:
                return "project"
            return "local"
    return "local"


def _page_summary(result: PageResult) -> dict[str, Any]:
    field_instances = [
        item for item in result.symbol_instances if item.role == "field_candidate"
    ]
    project_bindings = [
        binding
        for binding in result.symbol_bindings
        if _binding_kind(result, binding.id) == "project"
    ]
    project_legend_ids = {binding.legend_entry_id for binding in project_bindings}
    legends_by_id = {entry.id: entry for entry in result.legend_entries}
    return {
        "page": result.page,
        "completeness": result.completeness,
        "localConfirmed": sum(
            1
            for binding in result.symbol_bindings
            if binding.status == "confirmed"
            and _binding_kind(result, binding.id) == "local"
        ),
        "localProbable": sum(
            1
            for binding in result.symbol_bindings
            if binding.status == "probable"
            and _binding_kind(result, binding.id) == "local"
        ),
        "projectProbable": len(project_bindings),
        "projectLabels": sorted(
            {
                legends_by_id[item_id].label
                for item_id in project_legend_ids
                if item_id in legends_by_id and legends_by_id[item_id].label
            }
        ),
        "unresolvedCandidates": sum(
            item.status in {"unresolved", "unclassified"} for item in field_instances
        ),
        "unknownClusters": len(result.unknown_symbols),
        "anomalyCodes": sorted(set(result.anomaly_codes)),
    }


def _process_document(
    source: Path,
    package_root: Path,
    output: Path,
    catalog: dict[str, Any],
    catalog_fingerprint: str,
    *,
    rebuild: bool,
) -> dict[str, Any]:
    from dwg_sheets import _DOC_CACHE, sheets_for

    relative = source.relative_to(package_root)
    relative_text = relative.as_posix()
    file_id = stable_id("PF", relative_text)
    destination = output / "documents" / file_id
    summary_path = destination / "document_application.json"
    source_fingerprint = _source_fingerprint(source)
    if not rebuild and summary_path.exists():
        cached = json.loads(summary_path.read_text(encoding="utf-8"))
        if (
            cached.get("status") == "processed"
            and
            cached.get("sourceFingerprint") == source_fingerprint
            and cached.get("catalogFingerprint") == catalog_fingerprint
        ):
            return cached

    record: dict[str, Any] = {
        "fileId": file_id,
        "relativePath": relative_text,
        "discipline": _discipline(relative),
        "externalReference": _is_external_reference(relative),
        "sourceFingerprint": source_fingerprint,
        "catalogFingerprint": catalog_fingerprint,
        "status": "error",
        "pageCount": 0,
        "processedPages": 0,
        "legendPages": [],
        "pageResults": [],
        "errors": [],
    }
    dxf_path: Path | None = None
    try:
        dxf_path, sheets = sheets_for(source)
        record["pageCount"] = len(sheets)
        legend_pages = {
            page
            for page, sheet in enumerate(sheets, start=1)
            if has_legend_heading(sheet.texts)
        }
        record["legendPages"] = sorted(legend_pages)
        for page in range(1, len(sheets) + 1):
            try:
                if page in legend_pages:
                    extraction = extract_legend_page(source, page)
                    result = resolve_exact_blocks(extraction.page_result)
                    result = resolve_geometry_profiles(result, extraction.primitives)
                    result = resolve_layer_context(result, extraction.primitives)
                    result = resolve_project_exact_blocks(result, catalog)
                    page_dir = write_page_result(destination, result)
                    write_legend_crops(page_dir, extraction.crops)
                else:
                    result = extract_block_page(source, page)
                    result = resolve_project_exact_blocks(result, catalog)
                    write_page_result(destination, result)
                record["pageResults"].append(_page_summary(result))
                record["processedPages"] += 1
            except Exception as exc:
                record["errors"].append(
                    f"page {page}: {type(exc).__name__}: {exc}"
                )
        record["status"] = (
            "processed"
            if not record["errors"] and record["processedPages"] == record["pageCount"]
            else "partial"
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

    _atomic_json(summary_path, record)
    return record


def build_application_summary(
    records: list[dict[str, Any]],
    *,
    package_root: str,
    catalog_path: str,
) -> dict[str, Any]:
    page_results = [
        page
        for record in records
        for page in record.get("pageResults", [])
    ]
    statuses = Counter(record["status"] for record in records)
    disciplines = sorted({record["discipline"] for record in records})
    by_discipline: dict[str, dict[str, int]] = {}
    for discipline in disciplines:
        discipline_records = [
            record for record in records if record["discipline"] == discipline
        ]
        discipline_pages = [
            page
            for record in discipline_records
            for page in record.get("pageResults", [])
        ]
        by_discipline[discipline] = {
            "documents": len(discipline_records),
            "processedPages": len(discipline_pages),
            "projectProbable": sum(
                int(page["projectProbable"]) for page in discipline_pages
            ),
            "unresolvedCandidates": sum(
                int(page["unresolvedCandidates"]) for page in discipline_pages
            ),
        }
    return {
        "schemaVersion": 1,
        "packageRoot": package_root,
        "projectLegendCatalog": catalog_path,
        "completeness": "partial" if statuses.get("error") or statuses.get("partial") else "complete",
        "counts": {
            "documents": len(records),
            "processedDocuments": statuses.get("processed", 0),
            "partialDocuments": statuses.get("partial", 0),
            "errorDocuments": statuses.get("error", 0),
            "discoveredPages": sum(int(record["pageCount"]) for record in records),
            "processedPages": len(page_results),
            "localConfirmed": sum(int(page["localConfirmed"]) for page in page_results),
            "localProbable": sum(int(page["localProbable"]) for page in page_results),
            "projectProbable": sum(
                int(page["projectProbable"]) for page in page_results
            ),
            "unresolvedCandidates": sum(
                int(page["unresolvedCandidates"]) for page in page_results
            ),
            "unknownClusters": sum(int(page["unknownClusters"]) for page in page_results),
            "pagesWithProjectMatches": sum(
                int(page["projectProbable"]) > 0 for page in page_results
            ),
            "projectLabelsApplied": len(
                {
                    label
                    for page in page_results
                    for label in page.get("projectLabels", [])
                }
            ),
        },
        "documentStatuses": dict(sorted(statuses.items())),
        "byDiscipline": by_discipline,
        "documents": records,
        "policy": {
            "localLegendPriority": True,
            "projectMatchesStatus": "probable",
            "projectGeometryMatching": False,
            "relationshipsIncluded": False,
        },
    }


def _write_report(path: Path, summary: dict[str, Any]) -> None:
    counts = summary["counts"]
    lines = [
        "# Применение общей легенды проекта ко всем DWG",
        "",
        f"- Статус: `{summary['completeness']}`",
        f"- Документов: {counts['documents']}",
        f"- Обработано листов/представлений: "
        f"{counts['processedPages']} из {counts['discoveredPages']}",
        f"- Локальных `confirmed`: {counts['localConfirmed']}",
        f"- Локальных `probable`: {counts['localProbable']}",
        f"- Новых проектных `probable`: {counts['projectProbable']}",
        f"- Листов с проектными совпадениями: {counts['pagesWithProjectMatches']}",
        f"- Применено типов из проектной легенды: {counts['projectLabelsApplied']}",
        f"- Осталось неразрешённых кандидатов: {counts['unresolvedCandidates']} "
        f"в {counts['unknownClusters']} кластерах",
        "",
        "> Проектная легенда применяется только по уникальному точному совпадению "
        "block definition. Все межфайловые результаты остаются `probable`; "
        "геометрические проектные совпадения и связи на этом этапе не строятся.",
        "",
        "## По разделам",
        "",
    ]
    for discipline, values in summary["byDiscipline"].items():
        lines.append(
            f"- `{discipline}`: {values['processedPages']} листов, "
            f"{values['projectProbable']} проектных совпадений, "
            f"{values['unresolvedCandidates']} неразрешённых кандидатов."
        )
    failed = [
        record
        for record in summary["documents"]
        if record["status"] in {"partial", "error"}
    ]
    if failed:
        lines.extend(["", "## Неполностью обработанные документы", ""])
        for record in failed:
            detail = "; ".join(record["errors"]) or record["status"]
            lines.append(f"- `{record['relativePath']}`: {detail}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _fixture_root_for_page_dir(page_dir: Path) -> Path:
    if page_dir.parent.name == "dwg_symbols":
        return page_dir.parent.parent
    return page_dir.parent


def _sync_review_json(fixture_root: Path, result: PageResult) -> None:
    path = fixture_root / "review.json"
    if not path.is_file():
        return
    field = Counter(
        item.status
        for item in result.symbol_instances
        if item.role == "field_candidate"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["legendEntries"] = len(result.legend_entries)
    payload["recognized"] = field["confirmed"] + field["probable"]
    payload["confirmed"] = field["confirmed"]
    payload["probable"] = field["probable"]
    payload["unrecognizedCandidates"] = field["unresolved"] + field["unclassified"]
    payload["unknownClusters"] = len(result.unknown_symbols)
    payload["unknownOccurrences"] = sum(
        len(cluster.instance_ids) for cluster in result.unknown_symbols
    )
    payload["bindings"] = len(result.symbol_bindings)
    payload["anomalyCodes"] = sorted(set(result.anomaly_codes))
    _atomic_json(path, payload)


def apply_project_catalog_to_page_dir(
    page_dir: str | Path,
    project_catalog: str | Path,
) -> PageResult:
    """Join unresolved field INSERTs to the project catalog without converting DWG.

    Local confirmed bindings stay. Roles do not change. Status becomes
    ``probable`` only. Sheet scenes, notes and drawing_field are kept.
    """

    from .ideal_md import resolve_page_dir

    sidecar = resolve_page_dir(page_dir)
    catalog = json.loads(Path(project_catalog).read_text(encoding="utf-8"))
    result = resolve_project_exact_blocks(load_page_result(sidecar), catalog)
    fixture_root = _fixture_root_for_page_dir(sidecar)
    write_page_result(fixture_root, result)
    _sync_review_json(fixture_root, result)
    return result


def apply_project_legend(
    package_root: str | Path,
    project_catalog: str | Path,
    output: str | Path,
    *,
    rebuild: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Apply one project catalog to all package sheets with resumable documents."""

    source_root = Path(package_root)
    catalog_path = Path(project_catalog)
    destination = Path(output)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog_fingerprint = _catalog_fingerprint(catalog_path)
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
            catalog,
            catalog_fingerprint,
            rebuild=rebuild,
        )
        records.append(record)
        if progress:
            project_matches = sum(
                int(page["projectProbable"])
                for page in record.get("pageResults", [])
            )
            progress(
                f"  -> {record['status']}, {record['processedPages']}/"
                f"{record['pageCount']} page(s), {project_matches} project match(es)"
            )

    summary = build_application_summary(
        records,
        package_root=str(source_root),
        catalog_path=str(catalog_path),
    )
    _atomic_json(destination / "project_application_summary.json", summary)
    _write_report(destination / "PROJECT_APPLICATION_REPORT.md", summary)
    return summary
