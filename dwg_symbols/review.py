"""Human-review artifacts and aggregate statistics for real DWG sheets."""

from __future__ import annotations

from collections import Counter, namedtuple
import csv
import html
import io
import json
from pathlib import Path
from typing import Any

from dwg_render import render_svg

from .artifacts import _atomic_json, write_page_result
from .context_resolver import resolve_layer_context
from .furniture import annotate_geology_marks, classify_sheet_furniture
from .geometry_resolver import resolve_geometry_profiles
from .named_block import resolve_named_block_legend
from .legends import (
    _crop_primitives,
    _crop_svg,
    extract_legend_page,
    write_legend_crops,
)
from .resolver import resolve_exact_blocks
from .schema import FieldGeometry, PageResult, SymbolInstance, TextLabel
from .title_block import attach_title_block
from .dimension_read import attach_dimensions
from .axis_read import attach_axes
from .text_labels import attach_text_labels
from .schedule_join import attach_schedule_notes
from .field_geometry import attach_field_geometry
from .sheet_scenes import attach_sheet_scenes


_UNKNOWN_CROP_BUDGET = 100
_OVERLAY_UNKNOWN_BUDGET = 100
# ГОСТ 21.101 form 3 title block. Used only when the INSERT has no bbox, so
# stamp crops are a readable 185×55 mm window instead of a 6 mm point.
_STAMP_WIDTH_MM = 185.0
_STAMP_HEIGHT_MM = 55.0
_FURNITURE_COLOR = "#667085"
_OBJECT_COLOR = "#6941c6"
_ANNOTATION_COLOR = "#026aa2"
_MARK_COLOR = "#c11574"
_TEXT_LABEL_COLOR = "#135e96"
_GEOMETRY_COLOR = "#0d9488"
_STATUS_COLOR = {
    "confirmed": "#0b7a38",
    "probable": "#b26a00",
    "unresolved": "#b42318",
    "unclassified": "#b42318",
    "reference": "#2458a6",
    "ignored": _FURNITURE_COLOR,
}
_ROLE_MARK = {
    "sheet_furniture": (_FURNITURE_COLOR, "F"),
    "drawing_object": (_OBJECT_COLOR, "O"),
    "drawing_annotation": (_ANNOTATION_COLOR, "A"),
    "specification_mark": (_MARK_COLOR, "M"),
}
ReviewBuckets = namedtuple(
    "ReviewBuckets",
    (
        "recognized",
        "unknown",
        "furniture",
        "drawing_objects",
        "drawing_annotations",
        "specification_marks",
    ),
)


def _intersection_area(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    x0 = max(first[0], second[0])
    y0 = max(first[1], second[1])
    x1 = min(first[2], second[2])
    y1 = min(first[3], second[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def _stamp_bbox_from_insert(
    instance: SymbolInstance,
    frame: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Place the GOST title-block rectangle so it stays on the sheet."""

    x = instance.position.x
    y = instance.position.y
    width = _STAMP_WIDTH_MM
    height = _STAMP_HEIGHT_MM
    candidates = (
        (x, y, x + width, y + height),
        (x - width, y, x, y + height),
        (x, y - height, x + width, y),
        (x - width, y - height, x, y),
    )
    return max(candidates, key=lambda box: _intersection_area(box, frame))


def _instance_bbox(
    instance: SymbolInstance,
    meta: dict[str, Any],
) -> tuple[float, float, float, float]:
    frame = meta.get("bbox")
    if instance.bbox is not None:
        pad = 1.5
        bbox = (
            instance.bbox[0] - pad,
            instance.bbox[1] - pad,
            instance.bbox[2] + pad,
            instance.bbox[3] + pad,
        )
    elif instance.role == "sheet_furniture":
        fallback_frame = (
            tuple(float(value) for value in frame)
            if frame is not None
            else (
                instance.position.x,
                instance.position.y,
                instance.position.x + _STAMP_WIDTH_MM,
                instance.position.y + _STAMP_HEIGHT_MM,
            )
        )
        bbox = _stamp_bbox_from_insert(instance, fallback_frame)
    else:
        radius = 6.0
        bbox = (
            instance.position.x - radius,
            instance.position.y - radius,
            instance.position.x + radius,
            instance.position.y + radius,
        )
    clip = frame or bbox
    return (
        max(float(clip[0]), bbox[0]),
        max(float(clip[1]), bbox[1]),
        min(float(clip[2]), bbox[2]),
        min(float(clip[3]), bbox[3]),
    )


def _overlay_mark(instance: SymbolInstance) -> tuple[str, str]:
    if instance.role in _ROLE_MARK:
        return _ROLE_MARK[instance.role]
    return _STATUS_COLOR.get(instance.status, "#555555"), instance.status[0].upper()


def _svg_rect(
    bbox: tuple[float, float, float, float],
    origin: tuple[float, float],
) -> tuple[float, float, float, float]:
    x0, y1 = origin
    return (
        bbox[0] - x0,
        y1 - bbox[3],
        max(bbox[2] - bbox[0], 0.5),
        max(bbox[3] - bbox[1], 0.5),
    )


_DASHED_ZONES = (
    ("title_block", "title-block-zone", "зона основной надписи"),
    ("legend", "legend-zone", "зона легенды"),
    ("notes", "notes-zone", "зона примечаний"),
)


def _zone_bbox(
    sheet_zones: dict[str, Any] | None,
    name: str,
) -> tuple[float, float, float, float] | None:
    """Paper-mm window. Drawing field stays JSON-only."""

    if not sheet_zones:
        return None
    raw = sheet_zones.get(name)
    if raw is None:
        return None
    bbox = tuple(float(value) for value in raw)
    if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    return bbox  # type: ignore[return-value]


def _title_block_zone(
    sheet_zones: dict[str, Any] | None,
) -> tuple[float, float, float, float] | None:
    """Paper-mm title-block window. Drawing field stays JSON-only."""

    return _zone_bbox(sheet_zones, "title_block")


def _dashed_zone_markup(
    bbox: tuple[float, float, float, float],
    origin: tuple[float, float],
    group_id: str,
    title: str,
    color: str = _FURNITURE_COLOR,
) -> str:
    sx, sy, width, height = _svg_rect(bbox, origin)
    return (
        f'<g id="{html.escape(group_id)}"><title>{html.escape(title)}</title>'
        f'<rect x="{sx:.3f}" y="{sy:.3f}" width="{width:.3f}" '
        f'height="{height:.3f}" fill="none" stroke="{html.escape(color)}" '
        f'stroke-width="0.8" stroke-dasharray="4 2" '
        f'vector-effect="non-scaling-stroke"/></g>'
    )


def _overlay_svg(
    primitives: list[dict[str, Any]],
    meta: dict[str, Any],
    instances: list[SymbolInstance],
    sheet_zones: dict[str, Any] | None = None,
    text_labels: list[TextLabel] | None = None,
    field_geometry: list[FieldGeometry] | None = None,
) -> str:
    base = render_svg(primitives, meta)
    if not base:
        return ""
    x0, _, _, y1 = (float(value) for value in meta["bbox"])
    origin = (x0, y1)
    marks: list[str] = []
    for name, group_id, title in _DASHED_ZONES:
        zone = _zone_bbox(sheet_zones, name)
        if zone is not None:
            marks.append(_dashed_zone_markup(zone, origin, group_id, title))
    for instance in instances:
        bbox = _instance_bbox(instance, meta)
        sx, sy, width, height = _svg_rect(bbox, origin)
        color, mark = _overlay_mark(instance)
        title = html.escape(
            f"{instance.id} | {instance.status} | "
            f"{instance.block_name or instance.layer}"
        )
        marks.append(
            f'<g><title>{title}</title><rect x="{sx:.3f}" y="{sy:.3f}" '
            f'width="{width:.3f}" height="{height:.3f}" fill="none" '
            f'stroke="{color}" stroke-width="0.8" vector-effect="non-scaling-stroke"/>'
            f'<text x="{sx:.3f}" y="{max(2.0, sy - 1.0):.3f}" '
            f'font-size="3" fill="{color}">{html.escape(mark)}'
            f"</text></g>"
        )
    for label in text_labels or []:
        sx, sy, width, height = _svg_rect(label.bbox, origin)
        title = html.escape(f"{label.id} | {label.kind} | {label.text}")
        marks.append(
            f'<g><title>{title}</title><rect x="{sx:.3f}" y="{sy:.3f}" '
            f'width="{width:.3f}" height="{height:.3f}" fill="none" '
            f'stroke="{_TEXT_LABEL_COLOR}" stroke-width="0.8" '
            f'vector-effect="non-scaling-stroke"/>'
            f'<text x="{sx:.3f}" y="{max(2.0, sy - 1.0):.3f}" '
            f'font-size="3" fill="{_TEXT_LABEL_COLOR}">T</text></g>'
        )
    for item in field_geometry or []:
        marks.append(
            _dashed_zone_markup(
                item.bbox,
                origin,
                f"field-geometry-{item.id}",
                item.label or "линия / штриховка",
                color=_GEOMETRY_COLOR,
            )
        )
    caption_lines = [
        '<text x="0" y="0" fill="#0b7a38">C — confirmed</text>',
        '<text x="0" y="6" fill="#b26a00">P — probable</text>',
        '<text x="0" y="12" fill="#b42318">U — unknown representative</text>',
        f'<text x="0" y="18" fill="{_FURNITURE_COLOR}">F — оформление листа</text>',
        f'<text x="0" y="24" fill="{_OBJECT_COLOR}">O — объект чертежа</text>',
        f'<text x="0" y="30" fill="{_ANNOTATION_COLOR}">A — аннотация</text>',
        f'<text x="0" y="36" fill="{_MARK_COLOR}">M — марка / подпись</text>',
        f'<text x="0" y="42" fill="{_FURNITURE_COLOR}">зона основной надписи</text>',
    ]
    next_y = 48
    if text_labels:
        caption_lines.append(
            f'<text x="0" y="{next_y}" fill="{_TEXT_LABEL_COLOR}">'
            "T — текст вне блока</text>"
        )
        next_y += 6
    if _zone_bbox(sheet_zones, "legend") is not None:
        caption_lines.append(
            f'<text x="0" y="{next_y}" fill="{_FURNITURE_COLOR}">зона легенды</text>'
        )
        next_y += 6
    if _zone_bbox(sheet_zones, "notes") is not None:
        caption_lines.append(
            f'<text x="0" y="{next_y}" fill="{_FURNITURE_COLOR}">'
            "зона примечаний</text>"
        )
        next_y += 6
    if field_geometry:
        caption_lines.append(
            f'<text x="0" y="{next_y}" fill="{_GEOMETRY_COLOR}">'
            "линия / штриховка</text>"
        )
        next_y += 6
    legend_height = next_y + 4
    legend = (
        '<g transform="translate(8 8)" font-size="4">'
        f'<rect x="-3" y="-6" width="92" height="{legend_height}" fill="white" '
        'fill-opacity="0.9" stroke="#777" stroke-width="0.3"/>'
        f"{''.join(caption_lines)}"
        "</g>"
    )
    return base.replace("</svg>", f'<g id="symbol-review">{"".join(marks)}{legend}</g></svg>')


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _review_instances(result: PageResult) -> ReviewBuckets:
    by_id = {instance.id: instance for instance in result.symbol_instances}
    recognized = [
        instance
        for instance in result.symbol_instances
        if instance.status in {"confirmed", "probable"}
    ]
    unknown = [
        by_id[cluster.representative_instance_id]
        for cluster in result.unknown_symbols[:_UNKNOWN_CROP_BUDGET]
        if cluster.representative_instance_id in by_id
    ]
    furniture = [
        instance
        for instance in result.symbol_instances
        if instance.role == "sheet_furniture"
    ]
    drawing_objects = [
        instance
        for instance in result.symbol_instances
        if instance.role == "drawing_object"
    ]
    drawing_annotations = [
        instance
        for instance in result.symbol_instances
        if instance.role == "drawing_annotation"
    ]
    specification_marks = [
        instance
        for instance in result.symbol_instances
        if instance.role == "specification_mark"
    ]
    return ReviewBuckets(
        recognized,
        unknown,
        furniture,
        drawing_objects,
        drawing_annotations,
        specification_marks,
    )


def _review_counts(result: PageResult) -> dict[str, int]:
    status_counts = Counter(instance.status for instance in result.symbol_instances)
    field_status_counts = Counter(
        instance.status
        for instance in result.symbol_instances
        if instance.role == "field_candidate"
    )
    furniture = [
        instance
        for instance in result.symbol_instances
        if instance.role == "sheet_furniture"
    ]
    drawing_objects = [
        instance
        for instance in result.symbol_instances
        if instance.role == "drawing_object"
    ]
    drawing_annotations = [
        instance
        for instance in result.symbol_instances
        if instance.role == "drawing_annotation"
    ]
    specification_marks = [
        instance
        for instance in result.symbol_instances
        if instance.role == "specification_mark"
    ]
    return {
        "legendEntries": len(result.legend_entries),
        "recognized": field_status_counts["confirmed"] + field_status_counts["probable"],
        "confirmed": field_status_counts["confirmed"],
        "probable": field_status_counts["probable"],
        "unrecognizedCandidates": (
            field_status_counts["unresolved"] + field_status_counts["unclassified"]
        ),
        "unknownClusters": len(result.unknown_symbols),
        "unknownOccurrences": sum(
            len(cluster.instance_ids) for cluster in result.unknown_symbols
        ),
        "legendReferences": status_counts["reference"],
        "sheetFurniture": len(furniture),
        "sheetFurnitureClusters": len({item.signature for item in furniture}),
        "drawingObjects": len(drawing_objects),
        "drawingObjectClusters": len({item.signature for item in drawing_objects}),
        "drawingAnnotations": len(drawing_annotations),
        "drawingAnnotationClusters": len(
            {item.signature for item in drawing_annotations}
        ),
        "specificationMarks": len(specification_marks),
        "specificationMarkClusters": len(
            {item.signature for item in specification_marks}
        ),
        "textLabels": len(result.text_labels),
        "textLabelAxis": sum(
            1
            for item in result.text_labels
            if item.kind in {"axis", "axis_letter", "axis_digit"}
        ),
        "textLabelLinear": sum(
            1 for item in result.text_labels if item.kind == "linear"
        ),
        "fieldGeometry": len(result.field_geometry),
        "fieldGeometryLabeled": sum(
            1 for item in result.field_geometry if item.legend_entry_id
        ),
        "bindings": len(result.symbol_bindings),
    }


def run_sheet_review(
    drawing: str | Path,
    page: int,
    output: str | Path,
    fixture_id: str,
) -> dict[str, Any]:
    """Run H2-H4c and write sidecars, crops, overlay and one review summary."""

    destination = Path(output) / fixture_id
    extraction = extract_legend_page(drawing, page)
    result = classify_sheet_furniture(
        extraction.page_result,
        sheet_bbox=extraction.meta.get("bbox"),
    )
    # Furniture runs in extract and again here; read the stamp once on this
    # classified PageResult before H4 resolvers mutate instances.
    result = attach_title_block(
        result,
        extraction.texts,
        sheet_bbox=extraction.meta.get("bbox"),
    )
    result = resolve_exact_blocks(result)
    result = resolve_geometry_profiles(result, extraction.primitives)
    result = resolve_layer_context(result, extraction.primitives)
    result = resolve_named_block_legend(result)
    result = annotate_geology_marks(result)
    result = attach_dimensions(result, extraction.texts)
    result = attach_axes(result, extraction.texts)
    result = attach_text_labels(result, extraction.texts)
    result = attach_schedule_notes(result)
    result = attach_field_geometry(
        result, extraction.primitives, extraction.hatches
    )
    # Scenes cluster confirmed кругN; H4 confirm runs after the stamp pass.
    result = attach_sheet_scenes(result, extraction.texts)
    page_dir = write_page_result(destination, result)
    write_legend_crops(page_dir, extraction.crops)

    buckets = _review_instances(result)
    crop_dir = page_dir / "symbol_crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    expected_crops: set[str] = set()
    crop_records: list[dict[str, Any]] = []
    for category, instances in (
        ("recognized", buckets.recognized),
        ("unknown", buckets.unknown),
        ("furniture", buckets.furniture),
        ("object", buckets.drawing_objects),
        ("annotation", buckets.drawing_annotations),
        ("mark", buckets.specification_marks),
    ):
        for instance in instances:
            bbox = _instance_bbox(instance, extraction.meta)
            vectors, _ = _crop_primitives(extraction.primitives, bbox)
            filename = f"{category}-{instance.id}.svg"
            expected_crops.add(filename)
            _write_text(crop_dir / filename, _crop_svg(vectors, bbox))
            crop_records.append(
                {
                    "instanceId": instance.id,
                    "category": category,
                    "status": instance.status,
                    "path": f"dwg_symbols/page_{page:04d}/symbol_crops/{filename}",
                }
            )
    for existing in crop_dir.glob("*.svg"):
        if existing.name not in expected_crops:
            existing.unlink()

    overlay_instances = [
        *buckets.recognized,
        *buckets.unknown[:_OVERLAY_UNKNOWN_BUDGET],
        *buckets.furniture,
        *buckets.drawing_objects,
        *buckets.drawing_annotations,
        *buckets.specification_marks,
    ]
    overlay = _overlay_svg(
        extraction.primitives,
        extraction.meta,
        overlay_instances,
        sheet_zones=result.sheet_zones,
        text_labels=result.text_labels,
        field_geometry=result.field_geometry,
    )
    _write_text(destination / "page_review.svg", overlay)
    _write_text(destination / "page.svg", render_svg(extraction.primitives, extraction.meta))

    evidence_counts = Counter(
        evidence.kind
        for binding in result.symbol_bindings
        for evidence in binding.evidence
    )
    review = {
        "id": fixture_id,
        "documentPath": str(drawing),
        "page": page,
        "completeness": result.completeness,
        **_review_counts(result),
        "bindingEvidence": dict(sorted(evidence_counts.items())),
        "anomalyCodes": sorted(set(result.anomaly_codes)),
        "images": {
            "page": "page.svg",
            "overlay": "page_review.svg",
            "legendCrops": len(extraction.crops),
            "recognizedCrops": len(buckets.recognized),
            "unknownRepresentativeCrops": len(buckets.unknown),
            "furnitureCrops": len(buckets.furniture),
            "objectCrops": len(buckets.drawing_objects),
            "annotationCrops": len(buckets.drawing_annotations),
            "markCrops": len(buckets.specification_marks),
        },
        "cropIndex": crop_records,
    }
    _atomic_json(destination / "review.json", review)
    return review


def summarize_reviews(selection: str | Path, root: str | Path) -> dict[str, Any]:
    """Aggregate completed review.json files into JSON, CSV and Markdown."""

    selection_path = Path(selection)
    output = Path(root)
    manifest = json.loads(selection_path.read_text(encoding="utf-8"))
    reviews = [
        json.loads((output / fixture["id"] / "review.json").read_text(encoding="utf-8"))
        for fixture in manifest["fixtures"]
    ]
    numeric = (
        "legendEntries",
        "recognized",
        "confirmed",
        "probable",
        "unrecognizedCandidates",
        "unknownClusters",
        "unknownOccurrences",
        "sheetFurniture",
        "sheetFurnitureClusters",
        "drawingObjects",
        "drawingObjectClusters",
        "drawingAnnotations",
        "drawingAnnotationClusters",
        "specificationMarks",
        "specificationMarkClusters",
        "textLabels",
        "textLabelAxis",
        "textLabelLinear",
        "fieldGeometry",
        "fieldGeometryLabeled",
        "bindings",
    )
    totals = {
        name: sum(int(review.get(name, 0)) for review in reviews) for name in numeric
    }
    evidence = Counter(
        {
            kind: sum(
                int(review.get("bindingEvidence", {}).get(kind, 0))
                for review in reviews
            )
            for kind in {
                kind
                for review in reviews
                for kind in review.get("bindingEvidence", {})
            }
        }
    )
    report = {
        "schemaVersion": 1,
        "selection": str(selection_path),
        "documents": len(reviews),
        "totals": totals,
        "bindingEvidence": dict(sorted(evidence.items())),
        "fixtures": reviews,
        "caveat": (
            "unrecognizedCandidates are unresolved INSERT candidates; without "
            "human annotation they are not guaranteed to be real symbols."
        ),
    }
    _atomic_json(output / "statistics.json", report)

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(("id", *numeric, "completeness", "anomalies"))
    for review in reviews:
        writer.writerow(
            (
                review["id"],
                *(review.get(name, 0) for name in numeric),
                review["completeness"],
                "|".join(review["anomalyCodes"]),
            )
        )
    _write_text(output / "statistics.csv", buffer.getvalue())

    lines = [
        "# Проверка распознавания символов на 10 DWG-листах",
        "",
        f"Обработано листов: {len(reviews)}.",
        f"Распознано: {totals['recognized']} "
        f"(`confirmed`: {totals['confirmed']}, `probable`: {totals['probable']}).",
        f"Нераспознанных кандидатов: {totals['unrecognizedCandidates']} "
        f"в {totals['unknownClusters']} кластерах.",
        f"Оформление листа: {totals['sheetFurniture']} "
        f"({totals['sheetFurnitureClusters']} кластеров).",
        f"Объекты чертежа: {totals['drawingObjects']} "
        f"({totals['drawingObjectClusters']} кластеров).",
        f"Аннотации: {totals['drawingAnnotations']} "
        f"({totals['drawingAnnotationClusters']} кластеров).",
        f"Марки / подписи: {totals['specificationMarks']} "
        f"({totals['specificationMarkClusters']} кластеров).",
        f"Текст вне блока: {totals.get('textLabels', 0)} "
        f"(оси {totals.get('textLabelAxis', 0)}, "
        f"размеры {totals.get('textLabelLinear', 0)}).",
        f"Линии / штриховки: {totals.get('fieldGeometry', 0)} "
        f"(с подписью {totals.get('fieldGeometryLabeled', 0)}).",
        "",
        "> Нераспознанные кандидаты являются результатом H2 и могут включать "
        "технические блоки. Штамп, рамка, объекты чертежа, размеры и марки "
        "(оси, номерация) вынесены из unknown. Human annotation ещё не выполнена.",
        "",
        "## Листы",
        "",
    ]
    for review in reviews:
        lines.extend(
            [
                f"### {review['id']}",
                "",
                f"- документ: `{review['documentPath']}`; лист {review['page']};",
                f"- легенда: {review['legendEntries']};",
                f"- confirmed/probable: {review['confirmed']}/{review['probable']};",
                f"- нераспознанные кандидаты: {review['unrecognizedCandidates']} "
                f"({review['unknownClusters']} кластеров);",
                f"- оформление листа: {review.get('sheetFurniture', 0)} "
                f"({review.get('sheetFurnitureClusters', 0)} кластеров);",
                f"- объекты чертежа: {review.get('drawingObjects', 0)} "
                f"({review.get('drawingObjectClusters', 0)} кластеров);",
                f"- аннотации: {review.get('drawingAnnotations', 0)} "
                f"({review.get('drawingAnnotationClusters', 0)} кластеров);",
                f"- марки / подписи: {review.get('specificationMarks', 0)} "
                f"({review.get('specificationMarkClusters', 0)} кластеров);",
                f"- текст вне блока: {review.get('textLabels', 0)} "
                f"(оси {review.get('textLabelAxis', 0)}, "
                f"размеры {review.get('textLabelLinear', 0)});",
                f"- линии / штриховки: {review.get('fieldGeometry', 0)} "
                f"(с подписью {review.get('fieldGeometryLabeled', 0)});",
                f"- [лист с разметкой]({review['id']}/page_review.svg);",
                f"- [sidecars и crops]({review['id']}/dwg_symbols/page_{review['page']:04d}/).",
                "",
            ]
        )
    _write_text(output / "REPORT.md", "\n".join(lines) + "\n")
    return report
