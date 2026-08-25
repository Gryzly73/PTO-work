"""Document-agnostic legend-region detection.

The detector combines independent, explainable evidence.  Embedded PDF text
and vector frames are preferred; raster geometry and an injectable text reader
can add evidence for scanned pages.  A weak best candidate is never coerced
into a legend region.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from statistics import median
from typing import Iterable, Literal, Sequence

import pymupdf
from PIL import Image

from .artifacts import PageArtifacts, write_page_artifacts
from .geometry import BBox, PageTransform
from .legend_rows import LegendTextReader, PdfTextReader, TextSpan, extract_legend
from .schema import LegendEntry


DEFAULT_MIN_CONFIDENCE = 0.62
MAX_LEGEND_AREA_RATIO = 0.35
MAX_LEGEND_WIDTH_RATIO = 0.85
MAX_LEGEND_HEIGHT_RATIO = 0.65
MAX_ENTRY_LINE_TEXT = 160
MAX_ENTRY_LINES = 80
_HEADING_RE = re.compile(
    r"\b(?:условн\w*\s+обознач\w*|обозначения|legend|symbols?)\b",
    re.IGNORECASE,
)
_RUSSIAN_HEADING_RE = re.compile(
    r"\bусловн\w*\s+обознач\w*\b",
    re.IGNORECASE,
)
_LATIN_HEADING_RE = re.compile(r"\b(?:legend|symbols?)\b", re.IGNORECASE)
_SECTION_HEADING_RE = re.compile(
    r"(?:проектируем\w*\s+инженерн\w*\s+сет\w*|"
    r"engineering\s+networks?|utility\s+networks?)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class LegendRegionResult:
    """Result of automatic layout detection in canonical PDF coordinates."""

    status: Literal["found", "not_found"]
    region_id: str | None
    bbox_pdf: BBox | None
    confidence: float
    detection_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"found", "not_found"}:
            raise ValueError("status must be 'found' or 'not_found'")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be between 0 and 1")
        if self.status == "found":
            if self.bbox_pdf is None or not self.region_id:
                raise ValueError("found result requires region_id and bbox_pdf")
        elif self.bbox_pdf is not None or self.region_id is not None:
            raise ValueError("not_found result cannot contain a region or bbox")
        if len(set(self.detection_sources)) != len(self.detection_sources):
            raise ValueError("detection_sources must not contain duplicates")

    def to_dict(self) -> dict[str, object]:
        return {
            "legendStatus": self.status,
            "regionId": self.region_id,
            "bboxPdf": self.bbox_pdf.to_list() if self.bbox_pdf else None,
            "confidence": round(float(self.confidence), 6),
            "detectionSources": list(self.detection_sources),
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    bbox: BBox
    source: str


@dataclass(frozen=True, slots=True)
class _ScoredCandidate:
    bbox: BBox
    confidence: float
    sources: tuple[str, ...]


def _intersects(first: BBox, second: BBox) -> bool:
    return (
        first.x0 < second.x1
        and first.x1 > second.x0
        and first.y0 < second.y1
        and first.y1 > second.y0
    )


def _contains(outer: BBox, inner: BBox, *, tolerance: float = 1.5) -> bool:
    return (
        outer.x0 - tolerance <= inner.x0
        and outer.y0 - tolerance <= inner.y0
        and outer.x1 + tolerance >= inner.x1
        and outer.y1 + tolerance >= inner.y1
    )


def _rect_bbox(rect: pymupdf.Rect) -> BBox | None:
    if rect.width <= 0 or rect.height <= 0:
        return None
    return BBox(float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))


def _pdf_spans(page: pymupdf.Page) -> tuple[TextSpan, ...]:
    unique: dict[tuple[object, ...], TextSpan] = {}
    for word in page.get_text("words", sort=True):
        if (
            not str(word[4]).strip()
            or float(word[2]) <= float(word[0])
            or float(word[3]) <= float(word[1])
        ):
            continue
        span = TextSpan(
            text=str(word[4]),
            bbox_pdf=BBox(float(word[0]), float(word[1]), float(word[2]), float(word[3])),
        )
        key = (
            span.text,
            *(round(value, 2) for value in span.bbox_pdf.to_list()),
        )
        unique.setdefault(key, span)
    return tuple(unique.values())


def _line_groups(spans: Sequence[TextSpan]) -> list[list[TextSpan]]:
    if not spans:
        return []
    ordered = sorted(
        spans,
        key=lambda span: (
            (span.bbox_pdf.y0 + span.bbox_pdf.y1) / 2,
            span.bbox_pdf.x0,
        ),
    )
    tolerance = max(2.5, median(span.bbox_pdf.height for span in ordered) * 0.6)
    lines: list[list[TextSpan]] = []
    centers: list[float] = []
    for span in ordered:
        center = (span.bbox_pdf.y0 + span.bbox_pdf.y1) / 2
        if not lines or abs(center - centers[-1]) > tolerance:
            lines.append([span])
            centers.append(center)
        else:
            lines[-1].append(span)
            centers[-1] = median(
                (item.bbox_pdf.y0 + item.bbox_pdf.y1) / 2 for item in lines[-1]
            )
    return lines


def _line_text(line: Sequence[TextSpan]) -> str:
    return " ".join(span.text for span in sorted(line, key=lambda item: item.bbox_pdf.x0))


def _line_bbox(line: Sequence[TextSpan]) -> BBox:
    return BBox(
        min(span.bbox_pdf.x0 for span in line),
        min(span.bbox_pdf.y0 for span in line),
        max(span.bbox_pdf.x1 for span in line),
        max(span.bbox_pdf.y1 for span in line),
    )


def _is_heading_text(value: str) -> bool:
    return bool(_HEADING_RE.search(value) or _SECTION_HEADING_RE.search(value))


def _primary_heading_bbox(line: Sequence[TextSpan]) -> BBox | None:
    """Return only the matching heading words, not an entire drawing-wide line."""

    ordered = sorted(line, key=lambda item: item.bbox_pdf.x0)
    for start in range(len(ordered)):
        for end in range(start + 2, min(len(ordered), start + 6) + 1):
            group = ordered[start:end]
            if _RUSSIAN_HEADING_RE.fullmatch(_line_text(group).strip()):
                return _line_bbox(group)
    for span in ordered:
        text = span.text.strip()
        if _LATIN_HEADING_RE.search(text) or re.fullmatch(
            r"обозначения", text, re.IGNORECASE
        ):
            return span.bbox_pdf
    return None


def _heading_region(
    page_box: BBox, heading: BBox, spans: Sequence[TextSpan]
) -> BBox | None:
    """Grow a compact column from a heading until the next large whitespace gap."""

    left_padding = max(
        12.0,
        min(
            heading.width * 0.5,
            max(heading.height * 2.5, page_box.width * 0.04),
        ),
    )
    x0 = max(page_box.x0, heading.x0 - left_padding)
    # A wide heading must not make the scan consume a distant drawing or title
    # block on the same baselines. Wider legends should be proposed by their
    # frame instead of turning a borderless heading candidate into a page-wide
    # region.
    scan_width = min(
        page_box.width * 0.45,
        max(page_box.width * 0.12, heading.width * 5.0),
    )
    scan_x1 = min(page_box.x1, x0 + scan_width)
    scan_y1 = min(page_box.y1, heading.y1 + page_box.height * 0.6)
    nearby = tuple(
        span
        for span in spans
        if span.bbox_pdf.x1 > x0
        and span.bbox_pdf.x0 < scan_x1
        and span.bbox_pdf.y0 >= heading.y1 - 1.5
        and span.bbox_pdf.y1 <= scan_y1
    )
    lines = [
        line
        for line in _line_groups(nearby)
        if not _HEADING_RE.search(_line_text(line))
    ]
    if not lines:
        return None

    content: list[BBox] = []
    max_first_gap = max(60.0, heading.height * 8.0)
    for line in lines:
        box = _line_bbox(line)
        if not content:
            if box.y0 - heading.y1 > max_first_gap:
                break
            content.append(box)
            continue
        typical_height = median(item.height for item in content)
        max_gap = max(60.0, heading.height * 5.0, typical_height * 5.0)
        if box.y0 - content[-1].y1 > max_gap:
            break
        content.append(box)
    if len(content) < 2:
        return None

    right_padding = max(12.0, heading.height * 2.0)
    x1 = min(
        page_box.x1,
        max(heading.x1, *(box.x1 for box in content)) + right_padding,
    )
    y0 = max(page_box.y0, heading.y0 - max(2.0, heading.height * 0.25))
    y1 = min(
        page_box.y1,
        content[-1].y1 + max(heading.height, median(box.height for box in content)),
    )
    if x1 - x0 < 40 or y1 - y0 < 24:
        return None
    return BBox(x0, y0, x1, y1)


def _vector_geometry(
    page: pymupdf.Page,
) -> tuple[list[BBox], list[BBox], list[BBox]]:
    frames: list[BBox] = []
    horizontal: list[BBox] = []
    vertical: list[BBox] = []
    page_area = float(page.rect.width * page.rect.height)
    for drawing in page.get_drawings():
        rect = _rect_bbox(drawing["rect"])
        for item in drawing["items"]:
            if item[0] == "re":
                item_rect = _rect_bbox(item[1])
                if (
                    item_rect is not None
                    and item_rect.width * item_rect.height < page_area * 0.8
                    and item_rect.width >= 40
                    and item_rect.height >= 24
                ):
                    frames.append(item_rect)
            elif item[0] == "l":
                point_a, point_b = item[1], item[2]
                dx, dy = abs(point_b.x - point_a.x), abs(point_b.y - point_a.y)
                if dx >= 6 and dy <= 2:
                    horizontal.append(
                        BBox(
                            float(min(point_a.x, point_b.x)),
                            float(min(point_a.y, point_b.y) - 0.5),
                            float(max(point_a.x, point_b.x)),
                            float(max(point_a.y, point_b.y) + 0.5),
                        )
                    )
                elif dy >= 6 and dx <= 2:
                    vertical.append(
                        BBox(
                            float(min(point_a.x, point_b.x) - 0.5),
                            float(min(point_a.y, point_b.y)),
                            float(max(point_a.x, point_b.x) + 0.5),
                            float(max(point_a.y, point_b.y)),
                        )
                    )
        # Curves and short line groups are useful as symbol evidence.
        if rect is not None and rect.width <= 80 and rect.height <= 80:
            horizontal.append(rect)
    return frames, horizontal, vertical


def _heading_candidates(
    page_box: BBox, spans: Sequence[TextSpan]
) -> tuple[list[_Candidate], list[BBox]]:
    candidates: list[_Candidate] = []
    headings: list[BBox] = []
    for line in _line_groups(spans):
        bbox = _primary_heading_bbox(line)
        if bbox is None:
            continue
        headings.append(bbox)
        region = _heading_region(page_box, bbox, spans)
        if region is not None:
            candidates.append(_Candidate(region, "heading"))
    return candidates, headings


def _raster_frame_candidates(
    image: Image.Image, transform: PageTransform
) -> list[_Candidate]:
    """Find simple rectangular frames without OpenCV.

    Raster geometry is candidate evidence only.  Text/row evidence is still
    required to cross the normal confidence threshold.
    """

    gray = image.convert("L")
    width, height = gray.size
    pixels = gray.load()
    horizontal: list[tuple[int, int, int]] = []
    min_run = max(30, width // 12)
    for y in range(height):
        start: int | None = None
        for x in range(width):
            dark = pixels[x, y] < 100
            if dark and start is None:
                start = x
            if start is not None and (not dark or x == width - 1):
                end = x if dark and x == width - 1 else x - 1
                if end - start + 1 >= min_run:
                    horizontal.append((y, start, end))
                start = None
    candidates: list[_Candidate] = []
    for top_index, (top, left, right) in enumerate(horizontal):
        for bottom, other_left, other_right in horizontal[top_index + 1 :]:
            if bottom - top < 20 or bottom - top > height * 0.4:
                continue
            if abs(left - other_left) > 3 or abs(right - other_right) > 3:
                continue
            samples = range(top, bottom + 1, max(1, (bottom - top) // 20))
            left_ratio = sum(pixels[left, y] < 100 for y in samples) / len(samples)
            right_ratio = sum(pixels[right, y] < 100 for y in samples) / len(samples)
            if min(left_ratio, right_ratio) < 0.7:
                continue
            raster_box = BBox(float(left), float(top), float(right + 1), float(bottom + 1))
            candidates.append(_Candidate(transform.raster_to_pdf(raster_box), "raster_frame"))
    return candidates


def _dedupe_candidates(candidates: Iterable[_Candidate]) -> list[_Candidate]:
    result: list[_Candidate] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (item.bbox.y0, item.bbox.x0, item.bbox.width * item.bbox.height),
    ):
        if any(
            abs(candidate.bbox.x0 - other.bbox.x0) <= 3
            and abs(candidate.bbox.y0 - other.bbox.y0) <= 3
            and abs(candidate.bbox.x1 - other.bbox.x1) <= 3
            and abs(candidate.bbox.y1 - other.bbox.y1) <= 3
            for other in result
        ):
            continue
        result.append(candidate)
    return result


def _score_candidate(
    candidate: _Candidate,
    page_box: BBox,
    spans: Sequence[TextSpan],
    headings: Sequence[BBox],
    marks: Sequence[BBox],
    verticals: Sequence[BBox],
) -> _ScoredCandidate:
    area_ratio = (
        candidate.bbox.width
        * candidate.bbox.height
        / (page_box.width * page_box.height)
    )
    if (
        area_ratio > MAX_LEGEND_AREA_RATIO
        or candidate.bbox.width > page_box.width * MAX_LEGEND_WIDTH_RATIO
        or candidate.bbox.height > page_box.height * MAX_LEGEND_HEIGHT_RATIO
    ):
        return _ScoredCandidate(candidate.bbox, 0.0, (candidate.source, "oversized"))

    inside_spans = tuple(span for span in spans if _contains(candidate.bbox, span.bbox_pdf))
    lines = _line_groups(inside_spans)
    raw_entry_lines = [
        line for line in lines if not _is_heading_text(_line_text(line))
    ]
    entry_lines = [
        line
        for line in raw_entry_lines
        if len(_line_text(line)) <= MAX_ENTRY_LINE_TEXT
        and max(span.bbox_pdf.y1 for span in line)
        - min(span.bbox_pdf.y0 for span in line)
        <= min(36.0, candidate.bbox.height * 0.3)
    ]
    sources: list[str] = [candidate.source]
    score = 0.2 if candidate.source in {"frame", "raster_frame"} else 0.08

    if any(
        heading.y1 <= candidate.bbox.y1
        and heading.y0 >= candidate.bbox.y0 - max(40, candidate.bbox.height * 0.5)
        and heading.x0 < candidate.bbox.x1
        and heading.x1 > candidate.bbox.x0
        for heading in headings
    ):
        score += 0.2
        sources.append("heading")

    centers = [
        median((span.bbox_pdf.y0 + span.bbox_pdf.y1) / 2 for span in line)
        for line in entry_lines
    ]
    gaps = [
        second - first
        for first, second in zip(centers, centers[1:])
        if second > first
    ]
    regular_rows = (
        2 <= len(entry_lines) <= MAX_ENTRY_LINES
        and len(entry_lines) == len(raw_entry_lines)
        and bool(gaps)
        and min(gaps) >= 3.0
        and max(gaps) <= max(90.0, median(gaps) * 3.5)
    )
    if regular_rows:
        score += min(0.35, 0.24 + 0.055 * len(entry_lines))
        sources.append("row_pattern")

    text_lefts = [min(span.bbox_pdf.x0 for span in line) for line in entry_lines]
    graphic_rows = 0
    for line, text_left in zip(entry_lines, text_lefts):
        y0 = min(span.bbox_pdf.y0 for span in line)
        y1 = max(span.bbox_pdf.y1 for span in line)
        if any(
            _contains(candidate.bbox, mark)
            and mark.x1 < text_left - 2
            and mark.y0 < y1 + 8
            and mark.y1 > y0 - 8
            for mark in marks
        ):
            graphic_rows += 1
    if regular_rows and graphic_rows >= min(2, len(entry_lines)):
        score += 0.25
        sources.append("graphic_text_rows")

    internal_horizontal = [
        mark
        for mark in marks
        if mark.width >= candidate.bbox.width * 0.55
        and candidate.bbox.y0 + 3 < (mark.y0 + mark.y1) / 2 < candidate.bbox.y1 - 3
        and _contains(candidate.bbox, mark)
    ]
    if internal_horizontal:
        score += 0.1
        sources.append("raster_or_vector_rules")

    internal_vertical = [
        line
        for line in verticals
        if line.height >= candidate.bbox.height * 0.55 and _contains(candidate.bbox, line)
    ]
    if internal_vertical and len(entry_lines) < 2:
        score -= 0.15

    page_density = sum(
        span.bbox_pdf.width * span.bbox_pdf.height for span in inside_spans
    ) / (candidate.bbox.width * candidate.bbox.height)
    if regular_rows and 0.01 <= page_density <= 0.35:
        score += 0.1
        sources.append("compact_whitespace")

    # Heading-only candidates require actual symbol/text row geometry. A large
    # body-text block below a heading must not become a legend from text alone.
    if candidate.source == "heading" and "graphic_text_rows" not in sources:
        score = min(score, DEFAULT_MIN_CONFIDENCE - 0.01)
    if not regular_rows:
        score = min(score, DEFAULT_MIN_CONFIDENCE - 0.01)

    return _ScoredCandidate(
        bbox=candidate.bbox,
        confidence=max(0.0, min(1.0, score)),
        sources=tuple(dict.fromkeys(sources)),
    )


def detect_legend_region(
    document: str | Path,
    *,
    page_number: int,
    reader: LegendTextReader | None = None,
    dpi: int = 96,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> LegendRegionResult:
    """Locate the best legend region or return an explicit ``not_found``."""

    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("dpi must be a positive integer")
    if (
        isinstance(min_confidence, bool)
        or not isinstance(min_confidence, (int, float))
        or not math.isfinite(min_confidence)
        or not 0 < min_confidence <= 1
    ):
        raise ValueError("min_confidence must be in (0, 1]")

    with pymupdf.open(Path(document)) as opened:
        if page_number > opened.page_count:
            raise ValueError(
                f"page_number {page_number} exceeds document page count {opened.page_count}"
            )
        page = opened[page_number - 1]
        page_box = BBox(0, 0, float(page.rect.width), float(page.rect.height))
        spans = _pdf_spans(page)
        frames, marks, verticals = _vector_geometry(page)
        heading_candidates, headings = _heading_candidates(page_box, spans)
        candidates = [_Candidate(frame, "frame") for frame in frames]
        candidates.extend(heading_candidates)

        # Avoid a costly full-page raster scan when the PDF layer already
        # supplies candidates. Scanned/image-only pages take this fallback.
        if not candidates:
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            image = Image.frombytes(
                "RGB", (pixmap.width, pixmap.height), pixmap.samples
            )
            transform = PageTransform(
                pdf_width=page_box.width,
                pdf_height=page_box.height,
                raster_width=pixmap.width,
                raster_height=pixmap.height,
            )
            candidates.extend(_raster_frame_candidates(image, transform))
        candidates = _dedupe_candidates(candidates)

        scored: list[_ScoredCandidate] = []
        text_reader = reader
        for candidate in candidates:
            candidate_spans = spans
            if text_reader is not None:
                extra = tuple(text_reader.read(page, candidate.bbox))
                if extra:
                    candidate_spans = tuple(
                        {
                            (
                                span.text,
                                *span.bbox_pdf.to_list(),
                                span.source_kind,
                            ): span
                            for span in (*spans, *extra)
                        }.values()
                    )
            scored.append(
                _score_candidate(
                    candidate,
                    page_box,
                    candidate_spans,
                    headings,
                    marks,
                    verticals,
                )
            )

    if not scored:
        return LegendRegionResult("not_found", None, None, 0.0)
    best = max(
        scored,
        key=lambda item: (
            item.confidence,
            -(item.bbox.width * item.bbox.height),
            -item.bbox.y0,
            -item.bbox.x0,
        ),
    )
    if best.confidence < min_confidence:
        return LegendRegionResult(
            "not_found", None, None, best.confidence, best.sources
        )
    return LegendRegionResult(
        "found",
        "legend-main",
        best.bbox,
        best.confidence,
        best.sources,
    )


def extract_page_legend(
    document: str | Path,
    *,
    page_number: int,
    output_root: str | Path,
    reader: LegendTextReader | None = None,
    dpi: int = 144,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> tuple[LegendRegionResult, list[LegendEntry]]:
    """Auto-detect a legend and delegate row extraction to the S2 pipeline."""

    result = detect_legend_region(
        document,
        page_number=page_number,
        reader=reader,
        dpi=min(dpi, 144),
        min_confidence=min_confidence,
    )
    if result.status == "not_found":
        write_page_artifacts(
            output_root, PageArtifacts(page=page_number, legend_entries=[])
        )
        return result, []
    assert result.bbox_pdf is not None and result.region_id is not None
    entries = extract_legend(
        document,
        page_number=page_number,
        region_pdf=result.bbox_pdf,
        region_id=result.region_id,
        output_root=output_root,
        reader=reader or PdfTextReader(),
        dpi=dpi,
    )
    return result, entries


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Automatically locate and extract a page legend."
    )
    parser.add_argument("document", type=Path)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    args = parser.parse_args(argv)
    result, entries = extract_page_legend(
        args.document,
        page_number=args.page,
        output_root=args.output,
        dpi=args.dpi,
        min_confidence=args.min_confidence,
    )
    payload = result.to_dict()
    payload.update(
        {
            "page": args.page,
            "legendEntryCount": len(entries),
            "statuses": [entry.status for entry in entries],
        }
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
