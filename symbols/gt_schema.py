"""Strict stdlib-only validation for the S0 symbol GT interchange format."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from . import GT_SCHEMA_VERSION

REGION_STATUSES = {"found", "not_found"}
ROW_STATUSES = {"extracted", "text_unreadable", "unmatched"}
TYPED_ROW_STATUSES = {"extracted", "unmatched"}
INSTANCE_STATUSES = {
    "confirmed",
    "probable",
    "unresolved",
    "conflicting",
    "unclassified",
}
TYPED_INSTANCE_STATUSES = {"confirmed", "probable", "conflicting"}
UNTYPED_INSTANCE_STATUSES = {"unresolved", "unclassified"}
EXPECTED_STATUSES = {"annotated", "not_found", "skip"}
NEGATIVE_KINDS = {"text", "table", "line_intersection"}


class ValidationError(ValueError):
    """A malformed or unsupported GT document."""


def _fail(path: str, message: str) -> None:
    raise ValidationError(f"{path}: {message}")


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "expected object")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(path, "expected array")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "expected non-empty string")
    return value


def validate_bbox(value: Any, path: str = "bbox") -> list[float]:
    bbox = _list(value, path)
    if len(bbox) != 4:
        _fail(path, "expected [x0, y0, x1, y1]")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in bbox):
        _fail(path, "coordinates must be numbers")
    result = [float(v) for v in bbox]
    if not all(math.isfinite(v) for v in result):
        _fail(path, "coordinates must be finite")
    if result[0] >= result[2] or result[1] >= result[3]:
        _fail(path, "must have x0 < x1 and y0 < y1")
    return result


def _unique_id(item: dict[str, Any], path: str, seen: set[str]) -> str:
    item_id = _string(item.get("id"), f"{path}.id")
    if item_id in seen:
        _fail(f"{path}.id", f"duplicate id {item_id!r}")
    seen.add(item_id)
    return item_id


def validate_document(data: Any) -> dict[str, Any]:
    root = _object(data, "$")
    version = root.get("schemaVersion")
    if version != GT_SCHEMA_VERSION:
        _fail(
            "$.schemaVersion",
            f"unsupported version {version!r}; expected {GT_SCHEMA_VERSION}",
        )
    _string(root.get("documentId"), "$.documentId")
    pages = _list(root.get("pages"), "$.pages")
    page_ids: set[str] = set()
    for page_index, raw_page in enumerate(pages):
        page_path = f"$.pages[{page_index}]"
        page = _object(raw_page, page_path)
        page_id = _string(page.get("pageId"), f"{page_path}.pageId")
        if page_id in page_ids:
            _fail(f"{page_path}.pageId", f"duplicate page id {page_id!r}")
        page_ids.add(page_id)
        expected_status = page.get("expectedStatus")
        if expected_status not in EXPECTED_STATUSES:
            _fail(
                f"{page_path}.expectedStatus",
                f"expected one of {sorted(EXPECTED_STATUSES)}",
            )

        legend = _object(page.get("legend"), f"{page_path}.legend")
        region_path = f"{page_path}.legend.region"
        region = _object(legend.get("region"), region_path)
        _string(region.get("id"), f"{region_path}.id")
        region_status = region.get("status")
        if region_status not in REGION_STATUSES:
            _fail(
                f"{region_path}.status",
                f"expected one of {sorted(REGION_STATUSES)}",
            )
        if region_status == "found":
            validate_bbox(region.get("bbox"), f"{region_path}.bbox")
        elif region.get("bbox") is not None:
            _fail(f"{region_path}.bbox", "not_found region must use null")
        if expected_status == "annotated" and region_status != "found":
            _fail(region_path, "annotated page requires a found region")
        if expected_status in {"not_found", "skip"} and region_status != "not_found":
            _fail(
                region_path,
                f"{expected_status} page requires a not_found region",
            )

        rows = _list(legend.get("rows"), f"{page_path}.legend.rows")
        row_ids: set[str] = set()
        rows_by_id: dict[str, dict[str, Any]] = {}
        rows_by_type: dict[str, list[str]] = {}
        for row_index, raw_row in enumerate(rows):
            row_path = f"{page_path}.legend.rows[{row_index}]"
            row = _object(raw_row, row_path)
            row_id = _unique_id(row, row_path, row_ids)
            rows_by_id[row_id] = row
            validate_bbox(row.get("bbox"), f"{row_path}.bbox")
            status = row.get("status")
            if status not in ROW_STATUSES:
                _fail(f"{row_path}.status", f"expected one of {sorted(ROW_STATUSES)}")
            type_id = row.get("typeId")
            label = row.get("label")
            if status in TYPED_ROW_STATUSES:
                _string(label, f"{row_path}.label")
                type_id = _string(type_id, f"{row_path}.typeId")
                rows_by_type.setdefault(type_id, []).append(row_id)
            else:
                if label is not None:
                    _fail(f"{row_path}.label", "text_unreadable row must use null")
                if type_id is not None:
                    _fail(f"{row_path}.typeId", "text_unreadable row must use null")
        if region_status == "not_found" and rows:
            _fail(f"{page_path}.legend.rows", "not_found region requires empty rows")

        instances = _list(page.get("instances"), f"{page_path}.instances")
        if expected_status in {"not_found", "skip"} and instances:
            _fail(
                f"{page_path}.instances",
                f"{expected_status} page requires empty instances",
            )
        referenced_rows: set[str] = set()
        instance_ids: set[str] = set()
        for instance_index, raw_instance in enumerate(instances):
            instance_path = f"{page_path}.instances[{instance_index}]"
            instance = _object(raw_instance, instance_path)
            _unique_id(instance, instance_path, instance_ids)
            validate_bbox(instance.get("bbox"), f"{instance_path}.bbox")
            status = instance.get("status")
            if status not in INSTANCE_STATUSES:
                _fail(
                    f"{instance_path}.status",
                    f"expected one of {sorted(INSTANCE_STATUSES)}",
                )
            type_id = instance.get("typeId")
            row_id = instance.get("legendRowId")
            if status in TYPED_INSTANCE_STATUSES:
                type_id = _string(type_id, f"{instance_path}.typeId")
                if type_id not in rows_by_type:
                    _fail(
                        f"{instance_path}.typeId",
                        f"type {type_id!r} is not declared by a typed legend row",
                    )
                if status == "confirmed" and row_id is None:
                    _fail(
                        f"{instance_path}.legendRowId",
                        "confirmed instance requires a legend row",
                    )
            elif type_id is not None:
                _fail(f"{instance_path}.typeId", f"{status} instances must use null")
            if status in UNTYPED_INSTANCE_STATUSES and row_id is not None:
                _fail(
                    f"{instance_path}.legendRowId",
                    f"{status} instances must use null",
                )
            if row_id is not None:
                row = rows_by_id.get(row_id)
                if row is None:
                    _fail(f"{instance_path}.legendRowId", f"unknown row {row_id!r}")
                if row["status"] == "text_unreadable":
                    _fail(
                        f"{instance_path}.legendRowId",
                        "cannot bind to a text_unreadable row",
                    )
                if row["typeId"] != type_id:
                    _fail(
                        f"{instance_path}.legendRowId",
                        f"row type {row['typeId']!r} disagrees with instance type {type_id!r}",
                    )
                referenced_rows.add(row_id)
        unmatched_references = {
            row_id
            for row_id in referenced_rows
            if rows_by_id[row_id]["status"] == "unmatched"
        }
        if unmatched_references:
            _fail(
                f"{page_path}.legend.rows",
                f"unmatched rows cannot be referenced: {sorted(unmatched_references)}",
            )
        negatives = _list(page.get("negativeComponents"), f"{page_path}.negativeComponents")
        negative_ids: set[str] = set()
        for negative_index, raw_negative in enumerate(negatives):
            negative_path = f"{page_path}.negativeComponents[{negative_index}]"
            negative = _object(raw_negative, negative_path)
            _unique_id(negative, negative_path, negative_ids)
            validate_bbox(negative.get("bbox"), f"{negative_path}.bbox")
            if negative.get("kind") not in NEGATIVE_KINDS:
                _fail(
                    f"{negative_path}.kind",
                    f"expected one of {sorted(NEGATIVE_KINDS)}",
                )
    return root


def load_document(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{source}: cannot read JSON: {exc}") from exc
    return validate_document(data)

