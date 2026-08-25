"""Deterministic non-maximum suppression for symbol template detections."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from .geometry import BBox


@dataclass(frozen=True, slots=True)
class TemplateDetection:
    """One raw document-template detection before cross-detector deduplication."""

    legend_entry_id: str
    bbox_pdf: BBox
    score: float
    scale: float
    source_kind: str = "document_template"

    def __post_init__(self) -> None:
        if not isinstance(self.legend_entry_id, str) or not self.legend_entry_id.strip():
            raise ValueError("legend_entry_id must be non-empty")
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not math.isfinite(self.score)
            or not 0 <= self.score <= 1
        ):
            raise ValueError("score must be between 0 and 1")
        if (
            isinstance(self.scale, bool)
            or not isinstance(self.scale, (int, float))
            or not math.isfinite(self.scale)
            or self.scale <= 0
        ):
            raise ValueError("scale must be positive")
        if not isinstance(self.source_kind, str) or not self.source_kind.strip():
            raise ValueError("source_kind must be non-empty")


@dataclass(frozen=True, slots=True)
class CandidateDetection:
    """One geometry-only proposal before cross-evidence deduplication."""

    bbox_pdf: BBox
    score: float
    source_kinds: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not math.isfinite(self.score)
            or not 0 <= self.score <= 1
        ):
            raise ValueError("score must be between 0 and 1")
        if (
            not isinstance(self.source_kinds, tuple)
            or not self.source_kinds
            or any(
                not isinstance(source, str) or not source.strip()
                for source in self.source_kinds
            )
            or len(set(self.source_kinds)) != len(self.source_kinds)
        ):
            raise ValueError("source_kinds must be a non-empty unique string tuple")


def bbox_iou(first: BBox, second: BBox) -> float:
    """Return intersection-over-union for canonical PDF-point boxes."""

    left = max(first.x0, second.x0)
    top = max(first.y0, second.y0)
    right = min(first.x1, second.x1)
    bottom = min(first.y1, second.y1)
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    if intersection == 0:
        return 0.0
    union = first.width * first.height + second.width * second.height - intersection
    return intersection / union


def deduplicate_detections(
    detections: Iterable[TemplateDetection],
    *,
    iou_threshold: float = 0.5,
) -> list[TemplateDetection]:
    """Apply stable global NMS, including overlaps from different templates."""

    if (
        isinstance(iou_threshold, bool)
        or not isinstance(iou_threshold, (int, float))
        or not 0 <= iou_threshold <= 1
    ):
        raise ValueError("iou_threshold must be between 0 and 1")
    ordered = sorted(
        detections,
        key=lambda item: (
            -item.score,
            item.legend_entry_id,
            item.bbox_pdf.y0,
            item.bbox_pdf.x0,
            item.bbox_pdf.y1,
            item.bbox_pdf.x1,
            item.scale,
        ),
    )
    kept: list[TemplateDetection] = []
    for detection in ordered:
        if any(
            bbox_iou(detection.bbox_pdf, accepted.bbox_pdf) >= iou_threshold
            for accepted in kept
        ):
            continue
        kept.append(detection)
    return sorted(
        kept,
        key=lambda item: (
            item.bbox_pdf.y0,
            item.bbox_pdf.x0,
            item.legend_entry_id,
            -item.score,
        ),
    )


def deduplicate_candidates(
    detections: Iterable[CandidateDetection],
    *,
    iou_threshold: float = 0.5,
) -> list[CandidateDetection]:
    """Apply deterministic NMS while preserving all evidence kinds."""

    if (
        isinstance(iou_threshold, bool)
        or not isinstance(iou_threshold, (int, float))
        or not 0 <= iou_threshold <= 1
    ):
        raise ValueError("iou_threshold must be between 0 and 1")
    ordered = sorted(
        detections,
        key=lambda item: (
            -item.score,
            item.bbox_pdf.y0,
            item.bbox_pdf.x0,
            item.bbox_pdf.y1,
            item.bbox_pdf.x1,
            item.source_kinds,
        ),
    )
    kept: list[CandidateDetection] = []
    cell_size = max(
        1.0,
        max(
            (
                max(item.bbox_pdf.width, item.bbox_pdf.height)
                for item in ordered
            ),
            default=1.0,
        ),
    )
    cells: dict[tuple[int, int], list[int]] = {}

    def occupied_cells(box: BBox) -> tuple[tuple[int, int], ...]:
        return tuple(
            (cell_x, cell_y)
            for cell_x in range(
                math.floor(box.x0 / cell_size),
                math.floor(box.x1 / cell_size) + 1,
            )
            for cell_y in range(
                math.floor(box.y0 / cell_size),
                math.floor(box.y1 / cell_size) + 1,
            )
        )

    for detection in ordered:
        neighbor_indices: set[int] = (
            set(range(len(kept))) if iou_threshold == 0 else set()
        )
        detection_cells = occupied_cells(detection.bbox_pdf)
        for cell in detection_cells:
            neighbor_indices.update(cells.get(cell, ()))
        overlapping = [
            index
            for index in sorted(neighbor_indices)
            if bbox_iou(detection.bbox_pdf, kept[index].bbox_pdf) >= iou_threshold
        ]
        if not overlapping:
            accepted_index = len(kept)
            kept.append(detection)
            for cell in detection_cells:
                cells.setdefault(cell, []).append(accepted_index)
            continue
        best_index = overlapping[0]
        accepted = kept[best_index]
        kept[best_index] = CandidateDetection(
            bbox_pdf=accepted.bbox_pdf,
            score=max(accepted.score, detection.score),
            source_kinds=tuple(
                sorted(set(accepted.source_kinds) | set(detection.source_kinds))
            ),
        )
    return sorted(
        kept,
        key=lambda item: (
            item.bbox_pdf.y0,
            item.bbox_pdf.x0,
            item.bbox_pdf.y1,
            item.bbox_pdf.x1,
            item.source_kinds,
        ),
    )
