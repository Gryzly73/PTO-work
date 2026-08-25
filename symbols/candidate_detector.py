"""Independent open-set symbol proposals and unclassified sidecar registry."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import math
from pathlib import Path
from time import perf_counter
from typing import Iterable, Literal, Sequence

import pymupdf
from PIL import Image, ImageDraw

from .artifacts import (
    PageArtifacts,
    load_page_artifacts,
    page_sidecar_dir,
    write_crop,
    write_page_artifacts,
)
from .cluster import cluster_candidates
from .crop_normalizer import crop_image, normalize_symbol_crop, png_bytes
from .dedupe import (
    CandidateDetection,
    bbox_iou,
    deduplicate_candidates,
)
from .geometry import BBox, PageTransform
from .gates import AnomalyCode, SymbolsBudgets, enforce_budget
from .schema import LegendEntry, SymbolCandidate, SymbolInstance, stable_id
from .template_matcher import TemplateMatcherConfig, _components, _expanded_bbox
from .visual_signature import compute_visual_signature


REJECTION_REASONS = (
    "DimensionOrLineNetwork",
    "Hatching",
    "KnownOverlap",
    "LineNetwork",
    "LowFill",
    "SinglePrimitive",
    "SizeOrAspect",
    "SolidFill",
    "TextOverlap",
)


@dataclass(frozen=True, slots=True)
class CandidateDetectorConfig:
    """Conservative defaults: retain small repeated shapes, reject page structure."""

    threshold: int = 205
    min_component_pixels: int = 8
    component_padding: int = 3
    min_size_pdf: float = 6.0
    max_size_pdf: float = 45.0
    min_aspect_ratio: float = 0.18
    text_exclusion_padding_pdf: float = 2.0
    primitive_merge_gap_pdf: float = 2.5
    line_network_gap_pdf: float = 3.0
    min_fill_ratio: float = 0.012
    max_raster_fill_ratio: float = 0.85
    max_hatch_lines: int = 6
    known_overlap_iou: float = 0.25
    dedupe_iou: float = 0.5
    signature_similarity: float = 0.9
    minimum_repeats: int = 2
    clustering_backend: Literal["brute_force", "bk_tree"] = "brute_force"

    def __post_init__(self) -> None:
        if not 0 <= self.threshold <= 255:
            raise ValueError("threshold must be between 0 and 255")
        if self.min_component_pixels < 1:
            raise ValueError("min_component_pixels must be positive")
        if self.component_padding < 0:
            raise ValueError("component_padding must be non-negative")
        if not 0 < self.min_size_pdf <= self.max_size_pdf:
            raise ValueError("PDF size bounds must be positive and ordered")
        for name in (
            "text_exclusion_padding_pdf",
            "primitive_merge_gap_pdf",
            "line_network_gap_pdf",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative number")
        for name in (
            "min_aspect_ratio",
            "min_fill_ratio",
            "max_raster_fill_ratio",
            "known_overlap_iou",
            "dedupe_iou",
            "signature_similarity",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.minimum_repeats < 1:
            raise ValueError("minimum_repeats must be positive")
        if self.min_fill_ratio > self.max_raster_fill_ratio:
            raise ValueError("fill ratio bounds must be ordered")
        if self.max_hatch_lines < 2:
            raise ValueError("max_hatch_lines must be at least 2")
        if self.clustering_backend not in {"brute_force", "bk_tree"}:
            raise ValueError(
                "clustering_backend must be 'brute_force' or 'bk_tree'"
            )


def _expanded_pdf_bbox(bbox: BBox, padding: float, page_box: BBox) -> BBox:
    return BBox(
        max(page_box.x0, bbox.x0 - padding),
        max(page_box.y0, bbox.y0 - padding),
        min(page_box.x1, bbox.x1 + padding),
        min(page_box.y1, bbox.y1 + padding),
    )


@dataclass(frozen=True, slots=True)
class _Primitive:
    """A cheap geometry observation; no crop or visual signature is attached."""

    bbox_pdf: BBox
    source_kind: str
    ink_area_pdf: float
    item_count: int = 1
    line_only: bool = False


class _BBoxIndex:
    """Exact grid neighborhood lookup; callers still apply precise predicates."""

    def __init__(self, boxes: Sequence[BBox], *, cell_size: float) -> None:
        self.boxes = boxes
        self.cell_size = cell_size
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for index, box in enumerate(boxes):
            for cell in self._cells_for(box):
                self.cells[cell].append(index)

    def _cells_for(self, box: BBox, padding: float = 0.0) -> Iterable[tuple[int, int]]:
        x0 = math.floor((box.x0 - padding) / self.cell_size)
        y0 = math.floor((box.y0 - padding) / self.cell_size)
        x1 = math.floor((box.x1 + padding) / self.cell_size)
        y1 = math.floor((box.y1 + padding) / self.cell_size)
        for cell_x in range(x0, x1 + 1):
            for cell_y in range(y0, y1 + 1):
                yield cell_x, cell_y

    def query(self, box: BBox, *, padding: float = 0.0) -> Iterable[BBox]:
        indices: set[int] = set()
        for cell in self._cells_for(box, padding):
            indices.update(self.cells.get(cell, ()))
        return (self.boxes[index] for index in sorted(indices))


def _union_bbox(boxes: Iterable[BBox]) -> BBox:
    concrete = tuple(boxes)
    return BBox(
        min(box.x0 for box in concrete),
        min(box.y0 for box in concrete),
        max(box.x1 for box in concrete),
        max(box.y1 for box in concrete),
    )


def _boxes_within(first: BBox, second: BBox, gap: float) -> bool:
    horizontal = max(0.0, first.x0 - second.x1, second.x0 - first.x1)
    vertical = max(0.0, first.y0 - second.y1, second.y0 - first.y1)
    return horizontal <= gap and vertical <= gap


def _group_primitives(
    primitives: Sequence[_Primitive],
    *,
    gap: float,
    cell_size: float,
) -> list[tuple[_Primitive, ...]]:
    """Join nearby strokes/components using a bounded spatial index."""

    if not primitives:
        return []
    parent = list(range(len(primitives)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def join(first: int, second: int) -> None:
        first_root, second_root = root(first), root(second)
        if first_root != second_root:
            parent[max(first_root, second_root)] = min(first_root, second_root)

    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, primitive in enumerate(primitives):
        box = primitive.bbox_pdf
        x0 = math.floor((box.x0 - gap) / cell_size)
        y0 = math.floor((box.y0 - gap) / cell_size)
        x1 = math.floor((box.x1 + gap) / cell_size)
        y1 = math.floor((box.y1 + gap) / cell_size)
        neighbors: set[int] = set()
        for cell_x in range(x0, x1 + 1):
            for cell_y in range(y0, y1 + 1):
                neighbors.update(cells[(cell_x, cell_y)])
        for other in sorted(neighbors):
            if _boxes_within(box, primitives[other].bbox_pdf, gap):
                join(index, other)
        for cell_x in range(x0, x1 + 1):
            for cell_y in range(y0, y1 + 1):
                cells[(cell_x, cell_y)].append(index)

    grouped: dict[int, list[_Primitive]] = defaultdict(list)
    for index, primitive in enumerate(primitives):
        grouped[root(index)].append(primitive)
    return [
        tuple(grouped[key])
        for key in sorted(
            grouped,
            key=lambda item: (
                min(part.bbox_pdf.y0 for part in grouped[item]),
                min(part.bbox_pdf.x0 for part in grouped[item]),
            ),
        )
    ]


def _intersection_fraction(first: BBox, second: BBox) -> float:
    width = max(0.0, min(first.x1, second.x1) - max(first.x0, second.x0))
    height = max(0.0, min(first.y1, second.y1) - max(first.y0, second.y0))
    return width * height / (first.width * first.height)


def _plausible_size(bbox: BBox, config: CandidateDetectorConfig) -> bool:
    smaller, larger = min(bbox.width, bbox.height), max(bbox.width, bbox.height)
    return (
        smaller >= config.min_size_pdf
        and larger <= config.max_size_pdf
        and smaller / larger >= config.min_aspect_ratio
    )


def _exclusion_boxes(
    page: pymupdf.Page,
    legend_entries: Sequence[LegendEntry],
    known_instances: Sequence[SymbolInstance],
    *,
    padding: float,
) -> list[BBox]:
    page_box = BBox(0.0, 0.0, float(page.rect.width), float(page.rect.height))
    boxes = [
        BBox(float(word[0]), float(word[1]), float(word[2]), float(word[3]))
        for word in page.get_text("words", sort=False)
    ]
    boxes.extend(entry.bbox_pdf for entry in legend_entries)
    boxes.extend(instance.bbox_pdf for instance in known_instances)
    return [_expanded_pdf_bbox(box, padding, page_box) for box in boxes]


def _masked_raster(
    image: Image.Image,
    transform: PageTransform,
    exclusions: Sequence[BBox],
) -> Image.Image:
    masked = image.copy()
    draw = ImageDraw.Draw(masked)
    for exclusion in exclusions:
        box = transform.pdf_to_raster(exclusion)
        draw.rectangle(
            [box.x0, box.y0, box.x1, box.y1],
            fill="white",
        )
    return masked


def _drawing_bbox(rect: object, page_box: BBox, minimum_extent: float) -> BBox | None:
    if rect is None:
        return None
    if all(hasattr(rect, name) for name in ("x0", "y0", "x1", "y1")):
        x0, y0 = float(rect.x0), float(rect.y0)
        x1, y1 = float(rect.x1), float(rect.y1)
    else:
        coordinates = tuple(rect)
        if len(coordinates) != 4:
            return None
        x0, y0, x1, y1 = (float(value) for value in coordinates)
    if x0 == x1:
        x0, x1 = x0 - minimum_extent / 2, x1 + minimum_extent / 2
    if y0 == y1:
        y0, y1 = y0 - minimum_extent / 2, y1 + minimum_extent / 2
    x0, y0 = max(page_box.x0, x0), max(page_box.y0, y0)
    x1, y1 = min(page_box.x1, x1), min(page_box.y1, y1)
    if x0 >= x1 or y0 >= y1:
        return None
    return BBox(x0, y0, x1, y1)


def _filter_groups(
    groups: Sequence[tuple[_Primitive, ...]],
    *,
    exclusions: Sequence[BBox],
    line_networks: Sequence[BBox],
    config: CandidateDetectorConfig,
    rejected: Counter[str],
) -> list[CandidateDetection]:
    detections: list[CandidateDetection] = []
    exclusion_index = _BBoxIndex(
        exclusions,
        cell_size=max(config.max_size_pdf, config.text_exclusion_padding_pdf, 1.0),
    )
    network_index = _BBoxIndex(
        line_networks,
        cell_size=max(config.max_size_pdf, config.line_network_gap_pdf, 1.0),
    )
    for group in groups:
        bbox = _union_bbox(part.bbox_pdf for part in group)
        source_kinds = tuple(sorted({part.source_kind for part in group}))
        all_lines = all(part.line_only for part in group)
        line_items = sum(part.item_count for part in group if part.line_only)
        if any(
            _intersection_fraction(bbox, excluded) >= 0.05
            for excluded in exclusion_index.query(bbox)
        ):
            rejected["TextOverlap"] += 1
            continue
        if any(
            _boxes_within(bbox, network, config.line_network_gap_pdf)
            for network in network_index.query(
                bbox, padding=config.line_network_gap_pdf
            )
        ):
            rejected["DimensionOrLineNetwork"] += 1
            continue
        if all_lines and line_items == 1:
            rejected["SinglePrimitive"] += 1
            continue
        if all_lines and line_items >= config.max_hatch_lines:
            rejected["Hatching"] += 1
            continue
        if not _plausible_size(bbox, config):
            rejected["SizeOrAspect"] += 1
            continue
        fill_ratio = sum(part.ink_area_pdf for part in group) / (
            bbox.width * bbox.height
        )
        if fill_ratio < config.min_fill_ratio:
            rejected["LowFill"] += 1
            continue
        if (
            source_kinds == ("connected_component",)
            and fill_ratio > config.max_raster_fill_ratio
        ):
            rejected["SolidFill"] += 1
            continue
        detections.append(
            CandidateDetection(
                bbox_pdf=bbox,
                score=min(0.94, 0.82 + 0.02 * (len(group) > 1)),
                source_kinds=source_kinds,
            )
        )
    return detections


def _vector_detections(
    page: pymupdf.Page,
    exclusions: Sequence[BBox],
    config: CandidateDetectorConfig,
    rejected: Counter[str],
) -> tuple[list[CandidateDetection], int]:
    page_box = BBox(0.0, 0.0, float(page.rect.width), float(page.rect.height))
    primitives: list[_Primitive] = []
    line_networks: list[BBox] = []
    raw_count = 0
    for drawing in page.get_cdrawings():
        rect = drawing.get("rect")
        items = drawing.get("items", ())
        if rect is None or not items:
            continue
        raw_count += 1
        width = max(0.5, float(drawing.get("width", 0.5) or 0.5))
        bbox = _drawing_bbox(rect, page_box, width)
        if bbox is None:
            continue
        item_kinds = tuple(str(item[0]) for item in items if item)
        line_only = bool(item_kinds) and all(kind == "l" for kind in item_kinds)
        if (
            max(bbox.width, bbox.height) > config.max_size_pdf
            or min(bbox.width, bbox.height) > config.max_size_pdf
        ):
            rejected["LineNetwork" if line_only else "SizeOrAspect"] += 1
            if line_only:
                line_networks.append(bbox)
            continue
        primitives.append(
            _Primitive(
                bbox_pdf=bbox,
                source_kind="vector_path",
                ink_area_pdf=max(
                    bbox.width * bbox.height if not line_only else 0.0,
                    width * max(bbox.width, bbox.height),
                ),
                item_count=len(item_kinds),
                line_only=line_only,
            )
        )
    groups = _group_primitives(
        primitives,
        gap=config.primitive_merge_gap_pdf,
        cell_size=config.max_size_pdf + config.primitive_merge_gap_pdf,
    )
    return (
        _filter_groups(
            groups,
            exclusions=exclusions,
            line_networks=line_networks,
            config=config,
            rejected=rejected,
        ),
        raw_count,
    )


def find_open_set_candidates(
    document: str | Path,
    *,
    page_number: int,
    legend_entries: Sequence[LegendEntry] = (),
    known_instances: Sequence[SymbolInstance] = (),
    dpi: int = 144,
    config: CandidateDetectorConfig | None = None,
    budgets: SymbolsBudgets | None = None,
    metrics: dict[str, int | float] | None = None,
) -> tuple[list[CandidateDetection], Image.Image, PageTransform]:
    """Find geometry-only candidates without assigning legend names."""

    settings = config or CandidateDetectorConfig()
    limits = budgets or SymbolsBudgets()
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("dpi must be a positive integer")
    if any(entry.page != page_number for entry in legend_entries):
        raise ValueError("legend entries must belong to the requested page")
    if any(instance.page != page_number for instance in known_instances):
        raise ValueError("known instances must belong to the requested page")

    geometry_started = perf_counter()
    with pymupdf.open(Path(document)) as opened:
        if page_number > opened.page_count:
            raise ValueError(
                f"page_number {page_number} exceeds document page count {opened.page_count}"
            )
        page = opened[page_number - 1]
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        transform = PageTransform(
            pdf_width=float(page.rect.width),
            pdf_height=float(page.rect.height),
            raster_width=pixmap.width,
            raster_height=pixmap.height,
        )
        exclusions = _exclusion_boxes(
            page,
            legend_entries,
            known_instances,
            padding=settings.text_exclusion_padding_pdf,
        )
        masked = _masked_raster(image, transform, exclusions)
        component_config = TemplateMatcherConfig(
            threshold=settings.threshold,
            component_padding=settings.component_padding,
            min_component_pixels=settings.min_component_pixels,
        )
        component_started = perf_counter()
        components = _components(masked, component_config)
        component_elapsed_ms = (perf_counter() - component_started) * 1000.0
        rejected: Counter[str] = Counter()
        raster_started = perf_counter()
        raster_primitives: list[_Primitive] = []
        raster_line_networks: list[BBox] = []
        pixel_area_pdf = (
            transform.pdf_width
            * transform.pdf_height
            / (transform.raster_width * transform.raster_height)
        )
        for component in components:
            raster_bbox = _expanded_bbox(
                component.bbox,
                settings.component_padding,
                image.size,
            )
            pdf_bbox = transform.raster_to_pdf(raster_bbox)
            smaller, larger = (
                min(pdf_bbox.width, pdf_bbox.height),
                max(pdf_bbox.width, pdf_bbox.height),
            )
            if larger > settings.max_size_pdf:
                if smaller / larger < settings.min_aspect_ratio:
                    raster_line_networks.append(pdf_bbox)
                    rejected["LineNetwork"] += 1
                else:
                    rejected["SizeOrAspect"] += 1
                continue
            if any(
                bbox_iou(pdf_bbox, instance.bbox_pdf) >= settings.known_overlap_iou
                for instance in known_instances
            ):
                rejected["KnownOverlap"] += 1
                continue
            raster_primitives.append(
                _Primitive(
                    bbox_pdf=pdf_bbox,
                    source_kind="connected_component",
                    ink_area_pdf=component.pixel_count * pixel_area_pdf,
                    line_only=smaller / larger < settings.min_aspect_ratio,
                )
            )
        raster_groups = _group_primitives(
            raster_primitives,
            gap=settings.primitive_merge_gap_pdf,
            cell_size=settings.max_size_pdf + settings.primitive_merge_gap_pdf,
        )
        raster_detections = _filter_groups(
            raster_groups,
            exclusions=exclusions,
            line_networks=raster_line_networks,
            config=settings,
            rejected=rejected,
        )
        raster_elapsed_ms = (perf_counter() - raster_started) * 1000.0
        vector_started = perf_counter()
        vector_detections, raw_vector_count = _vector_detections(
            page,
            exclusions,
            settings,
            rejected,
        )
        vector_elapsed_ms = (perf_counter() - vector_started) * 1000.0
        enforce_budget(
            stage="s5",
            code=AnomalyCode.CANDIDATE_COMPONENT_BUDGET_EXCEEDED,
            observed=len(raster_detections) + len(vector_detections),
            limit=limits.max_candidate_components,
        )

    dedupe_started = perf_counter()
    detections = deduplicate_candidates(
        [*raster_detections, *vector_detections],
        iou_threshold=settings.dedupe_iou,
    )
    dedupe_elapsed_ms = (perf_counter() - dedupe_started) * 1000.0
    enforce_budget(
        stage="s5",
        code=AnomalyCode.CANDIDATE_BUDGET_EXCEEDED,
        observed=len(detections),
        limit=limits.max_candidates,
    )
    if metrics is not None:
        metrics.update(
            {
                "connectedComponentCount": len(raster_detections),
                "vectorPathCount": len(vector_detections),
                "rawConnectedComponentCount": len(components),
                "rawVectorPathCount": raw_vector_count,
                "filteredComponentCount": len(raster_detections)
                + len(vector_detections),
                "preDedupeCount": len(raster_detections) + len(vector_detections),
                "candidateCount": len(detections),
                "rejectedCount": sum(rejected.values()),
                "componentExtractionElapsedMs": round(component_elapsed_ms, 3),
                "rasterFilterElapsedMs": round(raster_elapsed_ms, 3),
                "vectorFilterElapsedMs": round(vector_elapsed_ms, 3),
                "dedupeElapsedMs": round(dedupe_elapsed_ms, 3),
                "geometryElapsedMs": round(
                    (perf_counter() - geometry_started) * 1000.0, 3
                ),
                **{
                    f"rejected{reason}Count": rejected[reason]
                    for reason in REJECTION_REASONS
                },
            }
        )
    return detections, image, transform


def _existing_artifacts(output_root: str | Path, page_number: int) -> PageArtifacts:
    page_dir = page_sidecar_dir(output_root, page_number)
    if (page_dir / "summary.json").exists():
        return load_page_artifacts(output_root, page_number)
    return PageArtifacts(page=page_number)


def detect_page_open_set(
    document: str | Path,
    *,
    page_number: int,
    output_root: str | Path,
    legend_entries: Sequence[LegendEntry] | None = None,
    known_instances: Sequence[SymbolInstance] | None = None,
    dpi: int = 144,
    config: CandidateDetectorConfig | None = None,
    budgets: SymbolsBudgets | None = None,
    metrics: dict[str, int | float] | None = None,
) -> PageArtifacts:
    """Persist candidates, repeated unclassified types, instances, and crops."""

    settings = config or CandidateDetectorConfig()
    limits = budgets or SymbolsBudgets()
    artifacts = _existing_artifacts(output_root, page_number)
    entries = tuple(legend_entries if legend_entries is not None else artifacts.legend_entries)
    known = tuple(
        known_instances
        if known_instances is not None
        else (
            instance
            for instance in artifacts.symbol_instances
            if instance.status != "unclassified"
        )
    )
    detections, image, transform = find_open_set_candidates(
        document,
        page_number=page_number,
        legend_entries=entries,
        known_instances=known,
        dpi=dpi,
        config=settings,
        budgets=limits,
        metrics=metrics,
    )
    enforce_budget(
        stage="s5",
        code=AnomalyCode.CROP_BUDGET_EXCEEDED,
        observed=len(detections) * 2,
        limit=limits.max_crop_files,
    )
    page_dir = page_sidecar_dir(output_root, page_number)
    crop_started = perf_counter()
    candidates: list[SymbolCandidate] = []
    for detection in detections:
        bbox_values = [round(value, 3) for value in detection.bbox_pdf.to_list()]
        candidate_id = stable_id(
            "SC",
            page_number,
            bbox_values,
            detection.source_kinds,
        )
        raw_name = f"candidates/{candidate_id}.raw.png"
        normalized_name = f"candidates/{candidate_id}.normalized.png"
        raw_image = crop_image(image, transform.pdf_to_raster(detection.bbox_pdf))
        normalized = normalize_symbol_crop(raw_image)
        write_crop(page_dir, raw_name, png_bytes(raw_image))
        write_crop(page_dir, normalized_name, png_bytes(normalized))
        candidates.append(
            SymbolCandidate(
                id=candidate_id,
                page=page_number,
                bbox_pdf=detection.bbox_pdf,
                raw_crop=f"symbol_crops/{raw_name}",
                normalized_crop=f"symbol_crops/{normalized_name}",
                source_kinds=detection.source_kinds,
                visual_signature=compute_visual_signature(normalized),
                confidence=detection.score,
            )
        )

    cluster_started = perf_counter()
    clusters = cluster_candidates(
        candidates,
        similarity_threshold=settings.signature_similarity,
        minimum_repeats=settings.minimum_repeats,
        backend=settings.clustering_backend,
    )
    cluster_elapsed_ms = round((perf_counter() - cluster_started) * 1000.0, 3)
    if metrics is not None:
        metrics.update(
            {
                "cropFileCount": len(candidates) * 2,
                "signatureCount": len(candidates),
                "cropSignatureElapsedMs": round(
                    (perf_counter() - crop_started) * 1000.0, 3
                ),
                "clusterElapsedMs": cluster_elapsed_ms,
                "clusterCount": len(clusters),
                "clusteredCandidateCount": sum(
                    len(cluster.candidate_ids) for cluster in clusters
                ),
            }
        )
    clustered_ids = {
        candidate_id for cluster in clusters for candidate_id in cluster.candidate_ids
    }
    candidates = [
        replace(candidate, status="unclassified")
        if candidate.id in clustered_ids
        else candidate
        for candidate in candidates
    ]
    unclassified = [
        instance for cluster in clusters for instance in cluster.instances
    ]
    result = PageArtifacts(
        page=page_number,
        legend_entries=list(entries),
        symbol_candidates=candidates,
        symbol_types=[
            *(item for item in artifacts.symbol_types if item.status != "unclassified"),
            *(cluster.symbol_type for cluster in clusters),
        ],
        symbol_instances=[*known, *unclassified],
        unclassified_symbols=unclassified,
        unmatched_legend_entries=artifacts.unmatched_legend_entries,
        conflicts=artifacts.conflicts,
    )
    write_page_artifacts(output_root, result)
    return result
