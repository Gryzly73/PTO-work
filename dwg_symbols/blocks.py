"""H2 baseline: visible top-level DWG INSERT inventory per sheet."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Iterator

from .furniture import classify_sheet_furniture
from .schema import (
    PageResult,
    Point,
    SymbolInstance,
    UnknownSymbolCluster,
    stable_id,
)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return round(value, 8)
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "x") and hasattr(value, "y"):
        result = [round(float(value.x), 8), round(float(value.y), 8)]
        if hasattr(value, "z"):
            result.append(round(float(value.z), 8))
        return result
    return str(value)


def block_signature(doc, block_name: str) -> str:
    """Document-local vector signature independent of handles and owners."""

    try:
        block = doc.blocks.get(block_name)
    except Exception:
        return stable_id("BS", "missing", block_name)
    if block is None:
        return stable_id("BS", "missing", block_name)
    entities: list[dict[str, Any]] = []
    for entity in block:
        attributes = {
            key: _json_value(value)
            for key, value in entity.dxfattribs().items()
            if key not in {"handle", "owner"}
        }
        entities.append(
            {
                "type": entity.dxftype(),
                "attributes": dict(sorted(attributes.items())),
            }
        )
    entities.sort(
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )
    payload = json.dumps(
        {"version": 1, "entities": entities},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"blockdef-v1:{hashlib.sha256(payload).hexdigest()}"


def _attributes(entity) -> dict[str, str]:
    result: dict[str, str] = {}
    for attribute in getattr(entity, "attribs", ()):
        tag = str(attribute.dxf.get("tag", "") or "").strip()
        if not tag:
            continue
        try:
            value = attribute.plain_text()
        except Exception:
            value = str(attribute.dxf.get("text", "") or "")
        result[tag] = value.strip()
    return result


def _insert_point(entity) -> tuple[float, float]:
    point = entity.dxf.insert
    return float(point.x), float(point.y)


def _model_occurrences(msp, sheet) -> Iterator[tuple[Any, str, int, Point]]:
    for entity in msp:
        if entity.dxftype() != "INSERT":
            continue
        x, y = _insert_point(entity)
        if sheet.views:
            for view_index, view in enumerate(sheet.views):
                if view.holds(x, y):
                    paper_x, paper_y = view.to_paper(x, y)
                    yield (
                        entity,
                        f"modelspace:viewport:{view_index}",
                        view_index,
                        Point(paper_x, paper_y, "paper", "mm"),
                    )
            continue
        window = sheet.main_window()
        if window and window[0] <= x <= window[2] and window[1] <= y <= window[3]:
            yield entity, "modelspace", 0, Point(x, y, "model", "drawing_unit")


def _layout_occurrences(doc, sheet) -> Iterator[tuple[Any, str, int, Point]]:
    if not sheet.layout_name:
        return
    try:
        layout = doc.layouts.get(sheet.layout_name)
    except Exception:
        return
    for entity in layout:
        if entity.dxftype() != "INSERT":
            continue
        x, y = _insert_point(entity)
        yield entity, f"layout:{sheet.layout_name}", 0, Point(x, y, "paper", "mm")


def _all_occurrences(doc, sheet) -> Iterable[tuple[Any, str, int, Point]]:
    yield from _model_occurrences(doc.modelspace(), sheet)
    yield from _layout_occurrences(doc, sheet)


def extract_block_page(path: str | Path, page: int) -> PageResult:
    """Extract an auditable unresolved candidate inventory for one DWG sheet."""

    import ezdxf

    from dwg_sheets import sheets_for, xref_names

    source = Path(path)
    dxf_path, sheets = sheets_for(source)
    if not 1 <= page <= len(sheets):
        raise IndexError(f"document has {len(sheets)} sheets, requested {page}")
    doc = ezdxf.readfile(str(dxf_path))
    sheet = sheets[page - 1]
    fingerprint = str(doc.header.get("$FINGERPRINTGUID", "") or "").strip()
    document_id = stable_id(
        "DOC",
        fingerprint,
        source.name,
        source.stat().st_size,
    )
    xrefs = xref_names(doc)
    instances: list[SymbolInstance] = []
    anomalies: list[str] = [
        "BLOCK_BASELINE_ONLY",
        "LEGEND_STAGE_NOT_RUN",
        "RELATIONSHIP_STAGE_NOT_RUN",
    ]
    skipped_xrefs = 0

    unit_code = int(doc.header.get("$INSUNITS", 0) or 0)
    if unit_code == 0:
        anomalies.append("UNITS_UNSPECIFIED")
    if xrefs:
        anomalies.append("XREF_RESOLUTION_NOT_VERIFIED")

    for occurrence_index, (entity, space, view_index, position) in enumerate(
        _all_occurrences(doc, sheet)
    ):
        block_name = str(entity.dxf.get("name", "") or "")
        if block_name in xrefs:
            skipped_xrefs += 1
            continue
        handle = str(entity.dxf.get("handle", "") or "")
        if not handle:
            handle = stable_id(
                "HANDLE", block_name, space, view_index, occurrence_index, position.x, position.y
            )
        signature = block_signature(doc, block_name)
        instance_id = stable_id(
            "SI", document_id, page, handle, space, view_index, position.x, position.y
        )
        instances.append(
            SymbolInstance(
                id=instance_id,
                page=page,
                source_kind="dwg_insert_candidate",
                status="unresolved",
                position=position,
                source_handle=handle,
                source_space=space,
                layer=str(entity.dxf.get("layer", "0") or "0"),
                signature=signature,
                block_name=block_name or None,
                attributes=_attributes(entity),
                confidence=1.0,
            )
        )

    if skipped_xrefs:
        anomalies.append("XREF_INSERTS_SKIPPED")
    if not instances:
        anomalies.append("NO_BLOCK_CANDIDATES")

    grouped: dict[str, list[SymbolInstance]] = defaultdict(list)
    for instance in instances:
        grouped[instance.signature].append(instance)
    unknowns = [
        UnknownSymbolCluster(
            id=stable_id("US", document_id, page, signature),
            page=page,
            signature=signature,
            instance_ids=tuple(item.id for item in items),
            reason="NO_LEGEND_BINDING_BASELINE",
            representative_instance_id=items[0].id,
        )
        for signature, items in sorted(grouped.items())
    ]
    return classify_sheet_furniture(
        PageResult(
            document_id=document_id,
            document_path=str(source),
            page=page,
            completeness="partial",
            anomaly_codes=anomalies,
            symbol_instances=instances,
            unknown_symbols=unknowns,
        )
    )
