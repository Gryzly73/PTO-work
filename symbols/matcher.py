"""Document-local visual and contextual evidence for symbol bindings."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Iterable, Mapping, Sequence

import pymupdf

from .geometry import BBox
from .schema import (
    ClassificationEvidence,
    LegendEntry,
    SymbolInstance,
    VisualSignature,
    stable_id,
)
from .visual_signature import signature_similarity


_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_MARKER_RE = re.compile(r"\b(?:DN\s*\d+(?:[.,]\d+)?|NO|NC)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class MatcherConfig:
    visual_weight: float = 0.80
    position_weight: float = 0.08
    nearby_label_weight: float = 0.08
    marker_weight: float = 0.04
    line_context_weight: float = 0.0
    nearby_radius_pdf: float = 45.0
    line_tolerance_pdf: float = 3.0
    minimum_visual_similarity: float = 0.55

    def __post_init__(self) -> None:
        weights = (
            self.visual_weight,
            self.position_weight,
            self.nearby_label_weight,
            self.marker_weight,
            self.line_context_weight,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in weights
        ):
            raise ValueError("matcher weights must be finite and non-negative")
        if not math.isclose(sum(weights), 1.0, abs_tol=1e-9):
            raise ValueError("matcher weights must sum to 1")
        for name in (
            "nearby_radius_pdf",
            "line_tolerance_pdf",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be finite and non-negative")
        if not 0 <= self.minimum_visual_similarity <= 1:
            raise ValueError("minimum_visual_similarity must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class InstanceContext:
    instance_id: str
    nearby_labels: tuple[str, ...] = ()
    connected_line_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LegendMatch:
    legend_entry_id: str
    score: float
    visual_similarity: float
    evidence: tuple[ClassificationEvidence, ...]


def _bbox_distance(first: BBox, second: BBox) -> float:
    dx = max(first.x0 - second.x1, second.x0 - first.x1, 0.0)
    dy = max(first.y0 - second.y1, second.y0 - first.y1, 0.0)
    return math.hypot(dx, dy)


class _ContextIndex:
    """Exact spatial prefilter for nearby text and line evidence."""

    def __init__(
        self,
        items: Sequence[tuple[BBox, str]],
        *,
        cell_size: float,
    ) -> None:
        self.items = items
        self.cell_size = max(1.0, cell_size)
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for index, (box, _) in enumerate(items):
            for cell in self._cells_for(box):
                self.cells[cell].append(index)

    def _cells_for(
        self,
        box: BBox,
        *,
        padding: float = 0.0,
    ) -> Iterable[tuple[int, int]]:
        x0 = math.floor((box.x0 - padding) / self.cell_size)
        y0 = math.floor((box.y0 - padding) / self.cell_size)
        x1 = math.floor((box.x1 + padding) / self.cell_size)
        y1 = math.floor((box.y1 + padding) / self.cell_size)
        for cell_x in range(x0, x1 + 1):
            for cell_y in range(y0, y1 + 1):
                yield cell_x, cell_y

    def query(
        self,
        box: BBox,
        *,
        padding: float,
    ) -> Iterable[tuple[BBox, str]]:
        indices: set[int] = set()
        for cell in self._cells_for(box, padding=padding):
            indices.update(self.cells.get(cell, ()))
        return (self.items[index] for index in sorted(indices))


def _normalized_tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    return {token.casefold() for token in _TOKEN_RE.findall(value)}


def _meaningful_position_tokens(value: str | None) -> set[str]:
    """Ignore bare numeric row numbers; they occur everywhere on CAD sheets."""

    return {
        token
        for token in _normalized_tokens(value)
        if not (token.isdigit() and len(token) == 1)
    }


def _meaningful_label_tokens(value: str | None) -> set[str]:
    return {
        token
        for token in _normalized_tokens(value)
        if not (token.isdigit() and len(token) == 1)
    }


def _markers(values: Sequence[str]) -> set[str]:
    return {
        re.sub(r"\s+", "", match.group(0)).upper().replace(",", ".")
        for value in values
        for match in _MARKER_RE.finditer(value)
    }


def extract_page_context(
    document: str | Path,
    *,
    page_number: int,
    instances: Sequence[SymbolInstance],
    config: MatcherConfig | None = None,
) -> dict[str, InstanceContext]:
    """Collect nearby text and touching linework without inferring engineering links."""

    settings = config or MatcherConfig()
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if any(instance.page != page_number for instance in instances):
        raise ValueError("instances must belong to the requested page")

    with pymupdf.open(Path(document)) as opened:
        if page_number > opened.page_count:
            raise ValueError(
                f"page_number {page_number} exceeds document page count {opened.page_count}"
            )
        page = opened[page_number - 1]
        words = [
            (
                BBox(float(item[0]), float(item[1]), float(item[2]), float(item[3])),
                str(item[4]).strip(),
            )
            for item in page.get_text("words", sort=True)
            if str(item[4]).strip()
        ]
        line_boxes: list[tuple[BBox, str]] = []
        for drawing in page.get_drawings():
            for item in drawing.get("items", ()):
                if not item or item[0] != "l":
                    continue
                start, end = item[1], item[2]
                line_box = BBox(
                    min(float(start.x), float(end.x)) - 0.01,
                    min(float(start.y), float(end.y)) - 0.01,
                    max(float(start.x), float(end.x)) + 0.01,
                    max(float(start.y), float(end.y)) + 0.01,
                )
                coordinates = [round(value, 3) for value in line_box.to_list()]
                line_boxes.append(
                    (line_box, stable_id("LN", page_number, coordinates))
                )

    word_index = _ContextIndex(
        words,
        cell_size=max(settings.nearby_radius_pdf, 1.0),
    )
    line_index = _ContextIndex(
        line_boxes,
        cell_size=max(
            settings.nearby_radius_pdf,
            settings.line_tolerance_pdf,
            1.0,
        ),
    )

    result: dict[str, InstanceContext] = {}
    for instance in sorted(instances, key=lambda item: item.id):
        labels = tuple(
            text
            for box, text in word_index.query(
                instance.bbox_pdf,
                padding=settings.nearby_radius_pdf,
            )
            if _bbox_distance(instance.bbox_pdf, box) <= settings.nearby_radius_pdf
        )
        line_ids = tuple(
            sorted(
                {
                    line_id
                    for box, line_id in line_index.query(
                        instance.bbox_pdf,
                        padding=settings.line_tolerance_pdf,
                    )
                    if _bbox_distance(instance.bbox_pdf, box)
                    <= settings.line_tolerance_pdf
                }
            )
        )
        result[instance.id] = InstanceContext(
            instance_id=instance.id,
            nearby_labels=tuple(dict.fromkeys(labels)),
            connected_line_ids=line_ids,
        )
    return result


def score_legend_matches(
    *,
    symbol_type_id: str,
    type_signature: VisualSignature | None,
    legend_entries: Sequence[LegendEntry],
    legend_signatures: Mapping[str, VisualSignature],
    contexts: Sequence[InstanceContext],
    config: MatcherConfig | None = None,
) -> list[LegendMatch]:
    """Rank legend rows using only crops, text, and geometry from the document."""

    settings = config or MatcherConfig()
    context_labels = tuple(
        dict.fromkeys(label for context in contexts for label in context.nearby_labels)
    )
    context_tokens = set().union(
        *(_meaningful_label_tokens(label) for label in context_labels)
    ) if context_labels else set()
    context_markers = _markers(context_labels)
    line_ids = tuple(
        sorted(
            {
                line_id
                for context in contexts
                for line_id in context.connected_line_ids
            }
        )
    )
    matches: list[LegendMatch] = []
    for entry in sorted(legend_entries, key=lambda item: item.id):
        legend_signature = legend_signatures.get(entry.id)
        visual = (
            signature_similarity(type_signature, legend_signature)
            if type_signature is not None and legend_signature is not None
            else 0.0
        )
        if visual < settings.minimum_visual_similarity:
            continue

        evidence: list[ClassificationEvidence] = [
            ClassificationEvidence(
                kind="document_visual",
                score=visual,
                source_entity_ids=(symbol_type_id, entry.id),
                detail="visual signatures compared from current-document crops",
            )
        ]
        position_tokens = _meaningful_position_tokens(entry.position)
        position_score = (
            1.0 if position_tokens and position_tokens <= context_tokens else 0.0
        )
        if position_score:
            evidence.append(
                ClassificationEvidence(
                    kind="document_position",
                    score=position_score,
                    source_entity_ids=(symbol_type_id, entry.id),
                    detail=f"legend position found nearby: {entry.position}",
                )
            )
        entry_tokens = _meaningful_label_tokens(
            entry.name_normalized or entry.name_raw
        )
        label_score = (
            len(entry_tokens & context_tokens) / len(entry_tokens)
            if entry_tokens
            else 0.0
        )
        if label_score:
            evidence.append(
                ClassificationEvidence(
                    kind="document_nearby_label",
                    score=label_score,
                    source_entity_ids=(symbol_type_id, entry.id),
                    detail="legend-name tokens occur in nearby page text",
                )
            )
        entry_markers = _markers(
            tuple(
                value
                for value in (entry.position, entry.name_normalized, entry.name_raw)
                if value
            )
        )
        marker_score = (
            len(entry_markers & context_markers) / len(entry_markers)
            if entry_markers
            else 0.0
        )
        if marker_score:
            evidence.append(
                ClassificationEvidence(
                    kind="document_marker",
                    score=marker_score,
                    source_entity_ids=(symbol_type_id, entry.id),
                    detail="DN/NO/NC marker agrees with nearby document text",
                )
            )
        line_score = 1.0 if line_ids else 0.0
        if line_score:
            evidence.append(
                ClassificationEvidence(
                    kind="document_line_context",
                    score=line_score,
                    source_entity_ids=(symbol_type_id, *line_ids),
                    detail=(
                        "instance geometry touches document linework; recorded "
                        "for audit but not used as classification evidence"
                    ),
                )
            )
        total = (
            settings.visual_weight * visual
            + settings.position_weight * position_score
            + settings.nearby_label_weight * label_score
            + settings.marker_weight * marker_score
            + settings.line_context_weight * line_score
        )
        matches.append(
            LegendMatch(
                legend_entry_id=entry.id,
                score=min(1.0, total),
                visual_similarity=visual,
                evidence=tuple(evidence),
            )
        )
    return sorted(
        matches,
        key=lambda item: (-item.score, -item.visual_similarity, item.legend_entry_id),
    )
