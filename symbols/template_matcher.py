"""Find instances of visual templates extracted from the current document."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
from statistics import mean, pstdev
from typing import Iterable, Sequence

import pymupdf
from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

from .artifacts import (
    PageArtifacts,
    load_page_artifacts,
    page_sidecar_dir,
    write_crop,
    write_page_artifacts,
)
from .crop_normalizer import crop_image, normalize_symbol_crop, png_bytes
from .dedupe import TemplateDetection, deduplicate_detections
from .gates import AnomalyCode, SymbolsBudgets, enforce_budget
from .geometry import BBox, PageTransform
from .schema import LegendEntry, SymbolInstance, SymbolType, stable_id


MIN_PATTERN_STDDEV = 12.0


@dataclass(frozen=True, slots=True)
class TemplateMatcherConfig:
    """Measured S4 defaults; scales are relative to legend-template ink size."""

    scales: tuple[float, ...] = (0.75, 1.0, 1.25)
    scale_tolerance: float = 0.22
    score_threshold: float = 0.78
    dedupe_iou: float = 0.5
    threshold: int = 205
    component_padding: int = 2
    min_component_pixels: int = 8
    min_ink_width: int = 3
    min_ink_height: int = 3
    min_ink_pixels: int = 16
    min_ink_area_ratio: float = 0.003
    max_ink_density: float = 0.8
    min_contrast: int = 20
    threshold_delta: int = 20
    min_threshold_stability: float = 0.7
    min_normalization_consistency: float = 0.9

    def __post_init__(self) -> None:
        if not self.scales or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
            for value in self.scales
        ):
            raise ValueError("scales must contain positive numbers")
        for name in ("scale_tolerance", "score_threshold", "dedupe_iou"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be between 0 and 1")
        if (
            isinstance(self.threshold, bool)
            or not isinstance(self.threshold, int)
            or not 0 <= self.threshold <= 255
        ):
            raise ValueError("threshold must be an integer from 0 to 255")
        if (
            isinstance(self.component_padding, bool)
            or not isinstance(self.component_padding, int)
            or self.component_padding < 0
        ):
            raise ValueError("component_padding must be a non-negative integer")
        for name in (
            "min_component_pixels",
            "min_ink_width",
            "min_ink_height",
            "min_ink_pixels",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.min_contrast, bool)
            or not isinstance(self.min_contrast, int)
            or not 0 <= self.min_contrast <= 255
        ):
            raise ValueError("min_contrast must be an integer from 0 to 255")
        if (
            isinstance(self.threshold_delta, bool)
            or not isinstance(self.threshold_delta, int)
            or not 1 <= self.threshold_delta <= 127
        ):
            raise ValueError("threshold_delta must be an integer from 1 to 127")
        for name in (
            "min_ink_area_ratio",
            "max_ink_density",
            "min_threshold_stability",
            "min_normalization_consistency",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be between 0 and 1")
@dataclass(frozen=True, slots=True)
class _Template:
    entry: LegendEntry
    normalized: Image.Image
    ink_width: int
    ink_height: int
    ink_density: float


@dataclass(frozen=True, slots=True)
class _Component:
    bbox: BBox
    pixel_count: int

    @property
    def width(self) -> float:
        return self.bbox.width

    @property
    def height(self) -> float:
        return self.bbox.height


def _ink_properties(image: Image.Image, threshold: int) -> tuple[int, int, float]:
    grayscale = image.convert("L")
    mask = grayscale.point(lambda value: 255 if value < threshold else 0)
    ink_bbox = mask.getbbox()
    if ink_bbox is None:
        return 0, 0, 0.0
    width = ink_bbox[2] - ink_bbox[0]
    height = ink_bbox[3] - ink_bbox[1]
    pixels = list(mask.crop(ink_bbox).getdata())
    return width, height, sum(value > 0 for value in pixels) / len(pixels)


def _binary_entropy(ratio: float) -> float:
    if ratio <= 0 or ratio >= 1:
        return 0.0
    return -(ratio * math.log2(ratio) + (1 - ratio) * math.log2(1 - ratio))


def _quality_threshold(image: Image.Image, config: TemplateMatcherConfig) -> int:
    """Include visible low-contrast legend ink without weakening contrast gates."""

    grayscale = image.convert("L")
    extrema = grayscale.getextrema()
    assert extrema is not None
    low, high = extrema
    midpoint = round(low + (high - low) * 0.5)
    return min(254, max(config.threshold, midpoint))


def _patterned_ink(
    image: Image.Image,
    threshold: int,
    *,
    min_stddev: float,
) -> bool:
    """Distinguish a textured fill swatch from an uninformative solid block."""

    grayscale = image.convert("L")
    mask = grayscale.point(lambda value: 255 if value < threshold else 0)
    ink_bbox = mask.getbbox()
    if ink_bbox is None:
        return False
    values = [
        value
        for value in grayscale.crop(ink_bbox).getdata()
        if value < threshold
    ]
    return len(set(values)) >= 8 and pstdev(values) >= min_stddev


def _bbox_stability(
    image: Image.Image, threshold: int, delta: int
) -> float:
    grayscale = image.convert("L")
    boxes = [
        grayscale.point(lambda value, cutoff=cutoff: 255 if value < cutoff else 0).getbbox()
        for cutoff in (
            max(1, threshold - delta),
            threshold,
            min(254, threshold + delta),
        )
    ]
    if any(box is None for box in boxes):
        return 0.0
    concrete = [box for box in boxes if box is not None]
    intersection = (
        max(box[0] for box in concrete),
        max(box[1] for box in concrete),
        min(box[2] for box in concrete),
        min(box[3] for box in concrete),
    )
    union = (
        min(box[0] for box in concrete),
        min(box[1] for box in concrete),
        max(box[2] for box in concrete),
        max(box[3] for box in concrete),
    )
    intersection_area = max(0, intersection[2] - intersection[0]) * max(
        0, intersection[3] - intersection[1]
    )
    union_area = (union[2] - union[0]) * (union[3] - union[1])
    return intersection_area / union_area if union_area else 0.0


def _valid_template(
    image: Image.Image, config: TemplateMatcherConfig
) -> tuple[Image.Image, int, int, float] | None:
    """Validate that a legend crop contains stable, informative visual ink."""

    grayscale = image.convert("L")
    values = sorted(grayscale.getdata())
    if not values:
        return None
    # Sparse line symbols can legitimately occupy less than two percent of a
    # roomy legend cell, so percentile contrast would mistake them for blanks.
    # Later ink-count, density, entropy, and stability gates still reject noise.
    low = values[0]
    high = values[-1]
    if high - low < config.min_contrast:
        return None

    quality_threshold = _quality_threshold(grayscale, config)
    ink_width, ink_height, ink_density = _ink_properties(
        grayscale, quality_threshold
    )
    ink_pixels = sum(value < quality_threshold for value in values)
    ink_ratio = ink_pixels / len(values)
    short_side = min(ink_width, ink_height)
    long_side = max(ink_width, ink_height)
    elongated_line = (
        short_side >= 1
        and long_side >= max(config.min_ink_width, config.min_ink_height) * 4
        and long_side / short_side >= 4.0
        and ink_ratio <= 0.25
    )
    patterned_fill = (
        ink_ratio <= 0.8
        and _patterned_ink(
            grayscale,
            quality_threshold,
            min_stddev=MIN_PATTERN_STDDEV,
        )
    )
    contrast_span = high - low
    stability_delta = min(
        config.threshold_delta,
        max(1, contrast_span // 4),
    )
    threshold_stability = _bbox_stability(
        grayscale, quality_threshold, stability_delta
    )
    if (
        (
            (
                ink_width < config.min_ink_width
                or ink_height < config.min_ink_height
            )
            and not elongated_line
        )
        or ink_pixels < config.min_ink_pixels
        or ink_ratio < config.min_ink_area_ratio
        or (
            ink_density > config.max_ink_density
            and not elongated_line
            and not patterned_fill
        )
        or _binary_entropy(ink_ratio)
        < _binary_entropy(config.min_ink_area_ratio)
        or (
            threshold_stability < config.min_threshold_stability
            and not elongated_line
        )
    ):
        return None

    normalized = normalize_symbol_crop(grayscale)
    renormalized = normalize_symbol_crop(normalized)
    if (
        _image_similarity(normalized, renormalized)
        < config.min_normalization_consistency
    ):
        return None
    return normalized, ink_width, ink_height, ink_density


def _components(image: Image.Image, config: TemplateMatcherConfig) -> list[_Component]:
    grayscale = image.convert("L")
    dark = bytearray(
        grayscale.point(
            lambda value: 1 if value < config.threshold else 0
        ).tobytes()
    )
    width = grayscale.width
    height = grayscale.height
    result: list[_Component] = []
    search_from = 0
    while (start := dark.find(1, search_from)) >= 0:
        dark[start] = 0
        pending = [start]
        start_y, start_x = divmod(start, width)
        min_x = max_x = start_x
        min_y = max_y = start_y
        pixel_count = 1
        while pending:
            index = pending.pop()
            y, x = divmod(index, width)
            for neighbor_y in range(max(0, y - 1), min(height, y + 2)):
                row = neighbor_y * width
                for neighbor_x in range(max(0, x - 1), min(width, x + 2)):
                    neighbor = row + neighbor_x
                    if dark[neighbor] == 1:
                        dark[neighbor] = 0
                        pending.append(neighbor)
                        pixel_count += 1
                        min_x = min(min_x, neighbor_x)
                        max_x = max(max_x, neighbor_x)
                        min_y = min(min_y, neighbor_y)
                        max_y = max(max_y, neighbor_y)
        search_from = start + 1
        if pixel_count < config.min_component_pixels:
            continue
        result.append(
            _Component(
                BBox(
                    float(min_x),
                    float(min_y),
                    float(max_x + 1),
                    float(max_y + 1),
                ),
                pixel_count,
            )
        )
    return result


def _expanded_bbox(bbox: BBox, padding: int, size: tuple[int, int]) -> BBox:
    left = max(0.0, bbox.x0 - padding)
    top = max(0.0, bbox.y0 - padding)
    right = min(float(size[0]), bbox.x1 + padding)
    bottom = min(float(size[1]), bbox.y1 + padding)
    return BBox(left, top, right, bottom)


def _image_similarity(first: Image.Image, second: Image.Image) -> float:
    first_gray = ImageOps.autocontrast(first.convert("L"))
    second_gray = ImageOps.autocontrast(second.convert("L"))
    difference = ImageChops.difference(first_gray, second_gray)
    return max(0.0, 1.0 - ImageStat.Stat(difference).mean[0] / 255.0)


def _closest_scale(
    component: _Component,
    template: _Template,
    config: TemplateMatcherConfig,
) -> tuple[float, float] | None:
    width_scale = component.width / template.ink_width
    height_scale = component.height / template.ink_height
    observed = (width_scale * height_scale) ** 0.5
    closest = min(config.scales, key=lambda scale: (abs(scale - observed), scale))
    relative_error = mean(
        (
            abs(width_scale - closest) / closest,
            abs(height_scale - closest) / closest,
        )
    )
    if relative_error > config.scale_tolerance:
        return None
    return closest, relative_error


def _mask_page_evidence(
    image: Image.Image,
    page: pymupdf.Page,
    transform: PageTransform,
    legend_entries: Sequence[LegendEntry],
) -> Image.Image:
    """Remove known text and legend rows before connected-component proposals."""

    masked = image.copy()
    draw = ImageDraw.Draw(masked)
    for word in page.get_text("words", sort=False):
        word_box = transform.pdf_to_raster(
            BBox(float(word[0]), float(word[1]), float(word[2]), float(word[3]))
        )
        draw.rectangle(word_box.to_list(), fill="white")
    for entry in legend_entries:
        row_box = transform.pdf_to_raster(entry.bbox_pdf)
        draw.rectangle(row_box.to_list(), fill="white")
    return masked


def find_template_detections(
    document: str | Path,
    *,
    page_number: int,
    legend_entries: Sequence[LegendEntry],
    dpi: int = 144,
    config: TemplateMatcherConfig | None = None,
    budgets: SymbolsBudgets | None = None,
    metrics: dict[str, int] | None = None,
) -> tuple[list[TemplateDetection], Image.Image, PageTransform]:
    """Detect known symbols without writing sidecars."""

    settings = config or TemplateMatcherConfig()
    limits = budgets or SymbolsBudgets()
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("dpi must be a positive integer")
    if any(entry.page != page_number for entry in legend_entries):
        raise ValueError("S4 templates must belong to the page being matched")

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
        templates: list[_Template] = []
        for entry in legend_entries:
            template_image = crop_image(
                image, transform.pdf_to_raster(entry.symbol_bbox_pdf)
            )
            validated = _valid_template(template_image, settings)
            if validated is None:
                continue
            normalized, ink_width, ink_height, ink_density = validated
            templates.append(
                _Template(
                    entry,
                    normalized,
                    ink_width,
                    ink_height,
                    ink_density,
                )
            )

        masked = _mask_page_evidence(image, page, transform, legend_entries)
        if not templates:
            if metrics is not None:
                metrics.update(
                    {
                        "templateCount": 0,
                        "rejectedTemplateCount": len(legend_entries),
                        "componentCount": 0,
                        "rawDetectionCount": 0,
                        "detectionCount": 0,
                    }
                )
            return [], image, transform
        raw_components = _components(masked, settings)
        components = [
            component
            for component in raw_components
            if any(
                _closest_scale(component, template, settings) is not None
                for template in templates
            )
        ]
        enforce_budget(
            stage="s4",
            code=AnomalyCode.TEMPLATE_COMPONENT_BUDGET_EXCEEDED,
            observed=len(components),
            limit=limits.max_template_components,
        )

    raw: list[TemplateDetection] = []
    for component in components:
        for template in templates:
            scale_result = _closest_scale(component, template, settings)
            if scale_result is None:
                continue
            scale, scale_error = scale_result
            candidate_bbox = _expanded_bbox(
                component.bbox, settings.component_padding, image.size
            )
            candidate_image = crop_image(image, candidate_bbox)
            normalized = normalize_symbol_crop(candidate_image)
            _, _, density = _ink_properties(candidate_image, settings.threshold)
            visual_score = _image_similarity(template.normalized, normalized)
            density_score = max(
                0.0,
                1.0
                - abs(density - template.ink_density)
                / max(template.ink_density, 0.01),
            )
            scale_score = max(0.0, 1.0 - scale_error)
            score = 0.8 * visual_score + 0.1 * density_score + 0.1 * scale_score
            if score < settings.score_threshold:
                continue
            raw.append(
                TemplateDetection(
                    legend_entry_id=template.entry.id,
                    bbox_pdf=transform.raster_to_pdf(candidate_bbox),
                    score=min(1.0, score),
                    scale=float(scale),
                )
            )
            enforce_budget(
                stage="s4",
                code=AnomalyCode.TEMPLATE_DETECTION_BUDGET_EXCEEDED,
                observed=len(raw),
                limit=limits.max_template_detections,
            )
    detections = deduplicate_detections(raw, iou_threshold=settings.dedupe_iou)
    enforce_budget(
        stage="s4",
        code=AnomalyCode.TEMPLATE_DETECTION_BUDGET_EXCEEDED,
        observed=len(detections),
        limit=limits.max_template_detections,
    )
    if metrics is not None:
        metrics.update(
            {
                "templateCount": len(templates),
                "rejectedTemplateCount": len(legend_entries) - len(templates),
                "rawComponentCount": len(raw_components),
                "rejectedComponentGeometryCount": len(raw_components)
                - len(components),
                "componentCount": len(components),
                "rawDetectionCount": len(raw),
                "detectionCount": len(detections),
            }
        )
    return detections, image, transform


def _existing_artifacts(output_root: str | Path, page_number: int) -> PageArtifacts:
    page_dir = page_sidecar_dir(output_root, page_number)
    if (page_dir / "summary.json").exists():
        return load_page_artifacts(output_root, page_number)
    return PageArtifacts(page=page_number)


def match_page_templates(
    document: str | Path,
    *,
    page_number: int,
    output_root: str | Path,
    legend_entries: Sequence[LegendEntry] | None = None,
    dpi: int = 144,
    config: TemplateMatcherConfig | None = None,
    budgets: SymbolsBudgets | None = None,
    metrics: dict[str, int] | None = None,
) -> PageArtifacts:
    """Match document templates and persist instances, types, and unmatched rows."""

    artifacts = _existing_artifacts(output_root, page_number)
    entries = tuple(legend_entries if legend_entries is not None else artifacts.legend_entries)
    matchable_entries = tuple(
        entry for entry in entries if entry.status != "text_unreadable"
    )
    detections, image, transform = find_template_detections(
        document,
        page_number=page_number,
        legend_entries=matchable_entries,
        dpi=dpi,
        config=config,
        budgets=budgets,
        metrics=metrics,
    )
    limits = budgets or SymbolsBudgets()
    enforce_budget(
        stage="s4",
        code=AnomalyCode.CROP_BUDGET_EXCEEDED,
        observed=len(detections) * 2,
        limit=limits.max_crop_files,
    )
    enforce_budget(
        stage="s4",
        code=AnomalyCode.INSTANCE_BUDGET_EXCEEDED,
        observed=len(detections),
        limit=limits.max_instances,
    )
    page_dir = page_sidecar_dir(output_root, page_number)
    entry_by_id = {entry.id: entry for entry in entries}
    instances: list[SymbolInstance] = []
    detections_by_entry: dict[str, list[TemplateDetection]] = {
        entry.id: [] for entry in entries
    }
    for detection in detections:
        detections_by_entry[detection.legend_entry_id].append(detection)
        bbox_values = [round(value, 3) for value in detection.bbox_pdf.to_list()]
        instance_id = stable_id(
            "SI", page_number, detection.legend_entry_id, bbox_values
        )
        raw_name = f"instances/{instance_id}.raw.png"
        normalized_name = f"instances/{instance_id}.normalized.png"
        raw_image = crop_image(image, transform.pdf_to_raster(detection.bbox_pdf))
        write_crop(page_dir, raw_name, png_bytes(raw_image))
        write_crop(
            page_dir,
            normalized_name,
            png_bytes(normalize_symbol_crop(raw_image)),
        )
        type_id = stable_id("ST", page_number, detection.legend_entry_id)
        instances.append(
            SymbolInstance(
                id=instance_id,
                page=page_number,
                symbol_type_id=type_id,
                legend_entry_id=detection.legend_entry_id,
                bbox_pdf=detection.bbox_pdf,
                raw_crop=f"symbol_crops/{raw_name}",
                normalized_crop=f"symbol_crops/{normalized_name}",
                source_kinds=(detection.source_kind,),
                status="probable",
                geometry_confidence=detection.score,
                classification_confidence=detection.score,
            )
        )

    types: list[SymbolType] = []
    for entry_id in sorted(detections_by_entry):
        matched = detections_by_entry[entry_id]
        if not matched:
            continue
        entry = entry_by_id[entry_id]
        types.append(
            SymbolType(
                id=stable_id("ST", page_number, entry_id),
                representative_crop=entry.normalized_crop or entry.raw_crop,
                instance_ids=tuple(
                    instance.id
                    for instance in instances
                    if instance.legend_entry_id == entry_id
                ),
                matched_legend_entry_id=entry_id,
                candidate_legend_entry_ids=(entry_id,),
                status="probable",
            )
        )

    unmatched = [
        replace(entry, status="unmatched")
        for entry in entries
        if entry.status != "text_unreadable"
        and not detections_by_entry[entry.id]
    ]
    result = PageArtifacts(
        page=page_number,
        legend_entries=list(entries),
        symbol_candidates=artifacts.symbol_candidates,
        symbol_types=types,
        symbol_instances=instances,
        unclassified_symbols=artifacts.unclassified_symbols,
        unmatched_legend_entries=unmatched,
        conflicts=artifacts.conflicts,
    )
    write_page_artifacts(output_root, result)
    return result
