"""Atomic sidecar writer for one DWG sheet."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from . import SCHEMA_VERSION
from .schema import (
    Evidence,
    LegendEntry,
    NoteText,
    PageResult,
    Relationship,
    SheetScene,
    SymbolBinding,
    UnknownSymbolCluster,
    _field_geometry_from_mapping,
    _text_label_from_mapping,
    symbol_instance_from_dict,
)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _sidecar(page: int, items: Iterable[Any]) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "page": page,
        "items": [item.to_dict() for item in items],
    }


def write_page_result(root: str | Path, result: PageResult) -> Path:
    """Write the complete public contract; absent stages are explicit empty arrays."""

    result.validate()
    page_dir = Path(root) / "dwg_symbols" / f"page_{result.page:04d}"
    page_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "legend_entries.json": _sidecar(result.page, result.legend_entries),
        "symbol_instances.json": _sidecar(result.page, result.symbol_instances),
        "symbol_bindings.json": _sidecar(result.page, result.symbol_bindings),
        "unrecognized_symbols.json": _sidecar(result.page, result.unknown_symbols),
        "relationships.json": _sidecar(result.page, result.relationships),
        "text_labels.json": _sidecar(result.page, result.text_labels),
        "notes_texts.json": _sidecar(result.page, result.notes_texts),
        "sheet_scenes.json": _sidecar(result.page, result.sheet_scenes),
        "field_geometry.json": _sidecar(result.page, result.field_geometry),
        "summary.json": result.summary_dict(),
    }
    for filename, payload in payloads.items():
        _atomic_json(page_dir / filename, payload)
    return page_dir


def load_items(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError(
            f"{source}: unsupported schemaVersion {data.get('schemaVersion')!r}"
        )
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError(f"{source}: items must be an array")
    return items


def _optional_sidecar_items(page_dir: Path, filename: str) -> list[dict[str, Any]]:
    path = page_dir / filename
    if not path.is_file():
        return []
    return load_items(path)


def _evidence_from_dict(payload: Mapping[str, Any]) -> Evidence:
    return Evidence(
        kind=str(payload["kind"]),
        score=float(payload["score"]),
        source_ids=tuple(str(item) for item in payload.get("sourceIds") or ()),
        detail=str(payload.get("detail") or ""),
    )


def _legend_from_dict(payload: Mapping[str, Any], *, page: int) -> LegendEntry:
    bbox = payload.get("bbox")
    symbol_bbox = payload.get("symbolBbox")
    return LegendEntry(
        id=str(payload["id"]),
        page=int(payload.get("page") or page),
        label=payload.get("label"),
        status=str(payload["status"]),
        source_kind=str(payload.get("sourceKind") or "dwg_vector_legend"),
        signature=payload.get("signature"),
        bbox=tuple(bbox) if bbox else None,
        symbol_bbox=tuple(symbol_bbox) if symbol_bbox else None,
        crop_path=payload.get("cropPath"),
        source_texts=tuple(str(item) for item in payload.get("sourceTexts") or ()),
        reference_signatures=tuple(
            str(item) for item in payload.get("referenceSignatures") or ()
        ),
        geometry_signatures=tuple(
            str(item) for item in payload.get("geometrySignatures") or ()
        ),
        confidence=float(payload.get("confidence") or 0.0),
    )


def _binding_from_dict(payload: Mapping[str, Any], *, page: int) -> SymbolBinding:
    return SymbolBinding(
        id=str(payload["id"]),
        page=int(payload.get("page") or page),
        instance_id=str(payload["instanceId"]),
        legend_entry_id=str(payload["legendEntryId"]),
        status=str(payload["status"]),
        confidence=float(payload["confidence"]),
        evidence=tuple(
            _evidence_from_dict(item) for item in payload.get("evidence") or ()
        ),
    )


def _unknown_from_dict(
    payload: Mapping[str, Any], *, page: int
) -> UnknownSymbolCluster:
    instance_ids = tuple(str(item) for item in payload.get("instanceIds") or ())
    return UnknownSymbolCluster(
        id=str(payload["id"]),
        page=int(payload.get("page") or page),
        signature=str(payload["signature"]),
        instance_ids=instance_ids,
        reason=str(payload["reason"]),
        representative_instance_id=str(
            payload.get("representativeInstanceId") or instance_ids[0]
        ),
    )


def _relationship_from_dict(payload: Mapping[str, Any], *, page: int) -> Relationship:
    return Relationship(
        id=str(payload["id"]),
        page=int(payload.get("page") or page),
        source_id=str(payload["sourceId"]),
        target_id=str(payload["targetId"]),
        relation_type=str(payload["type"]),
        status=str(payload["status"]),
        directed=bool(payload.get("directed")),
        confidence=float(payload["confidence"]),
        evidence=tuple(
            _evidence_from_dict(item) for item in payload.get("evidence") or ()
        ),
    )


def _note_from_dict(payload: Mapping[str, Any], *, page: int) -> NoteText:
    return NoteText(
        id=str(payload["id"]),
        page=int(payload.get("page") or page),
        text=str(payload["text"]),
        source=str(payload.get("source") or "text"),
        layer=str(payload.get("layer") or ""),
        x=float(payload.get("x") or 0.0),
        y=float(payload.get("y") or 0.0),
        bbox=tuple(payload["bbox"]),
    )


def _scene_from_dict(payload: Mapping[str, Any], *, page: int) -> SheetScene:
    return SheetScene(
        id=str(payload["id"]),
        page=int(payload.get("page") or page),
        title=str(payload["title"]),
        bbox=tuple(payload["bbox"]),
        soil_ids=tuple(str(item) for item in payload.get("soilIds") or ()),
        axis_ids=tuple(str(item) for item in payload.get("axisIds") or ()),
    )


def load_page_result(page_dir: str | Path) -> PageResult:
    """Rebuild a page from sidecar JSON without converting DWG."""

    source = Path(page_dir)
    summary = json.loads((source / "summary.json").read_text(encoding="utf-8"))
    page = int(summary.get("page") or 1)
    return PageResult(
        document_id=str(summary["documentId"]),
        document_path=str(summary["documentPath"]),
        page=page,
        completeness=str(summary.get("completeness") or "partial"),
        anomaly_codes=list(summary.get("anomalyCodes") or []),
        legend_entries=[
            _legend_from_dict(item, page=page)
            for item in _optional_sidecar_items(source, "legend_entries.json")
        ],
        symbol_instances=[
            symbol_instance_from_dict(item, page=page)
            for item in _optional_sidecar_items(source, "symbol_instances.json")
        ],
        symbol_bindings=[
            _binding_from_dict(item, page=page)
            for item in _optional_sidecar_items(source, "symbol_bindings.json")
        ],
        unknown_symbols=[
            _unknown_from_dict(item, page=page)
            for item in _optional_sidecar_items(source, "unrecognized_symbols.json")
        ],
        relationships=[
            _relationship_from_dict(item, page=page)
            for item in _optional_sidecar_items(source, "relationships.json")
        ],
        title_block=summary.get("titleBlock"),
        sheet_zones=summary.get("sheetZones"),
        text_labels=[
            _text_label_from_mapping(item, page=page)
            for item in _optional_sidecar_items(source, "text_labels.json")
        ],
        notes_texts=[
            _note_from_dict(item, page=page)
            for item in _optional_sidecar_items(source, "notes_texts.json")
        ],
        sheet_scenes=[
            _scene_from_dict(item, page=page)
            for item in _optional_sidecar_items(source, "sheet_scenes.json")
        ],
        field_geometry=[
            _field_geometry_from_mapping(item, page=page)
            for item in _optional_sidecar_items(source, "field_geometry.json")
        ],
    )
