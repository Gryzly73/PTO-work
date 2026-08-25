"""Extract legend rows from an explicitly supplied page region.

This S2 module deliberately does not locate legends.  A caller or fixture must
provide the region in canonical PDF-point coordinates.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
from statistics import median
from typing import Any, Protocol, Sequence

import pymupdf
from PIL import Image

from .artifacts import PageArtifacts, page_sidecar_dir, write_crop, write_page_artifacts
from .crop_normalizer import crop_image, normalize_symbol_crop, png_bytes
from .geometry import BBox, PageTransform
from .gt_schema import load_document
from .schema import LegendEntry, stable_id


MAX_ROW_TEXT_CHARS = 160
MAX_ROW_TEXT_SPANS = 32
MIN_SYMBOL_WIDTH_PT = 4.0
_NON_ENTRY_HEADING_RE = re.compile(
    r"(?:условн\w*\s+обознач\w*|обозначения|legend|symbols?|"
    r"проектируем\w*\s+инженерн\w*\s+сет\w*|"
    r"engineering\s+networks?|utility\s+networks?)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TextSpan:
    """Text and location returned by a reader adapter."""

    text: str
    bbox_pdf: BBox
    confidence: float = 1.0
    source_kind: str = "pdf_text"

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text span must contain non-empty text")
        if not isinstance(self.bbox_pdf, BBox):
            raise TypeError("bbox_pdf must be a BBox")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be between 0 and 1")
        if not isinstance(self.source_kind, str) or not self.source_kind.strip():
            raise ValueError("source_kind must be non-empty")


class LegendTextReader(Protocol):
    """Replaceable adapter; OCR/VLM readers can implement this later."""

    def read(self, page: pymupdf.Page, region_pdf: BBox) -> Sequence[TextSpan]:
        """Read text spans intersecting ``region_pdf``."""


class PdfTextReader:
    """Read the embedded PDF/SVG text layer without OCR or network access."""

    def read(self, page: pymupdf.Page, region_pdf: BBox) -> Sequence[TextSpan]:
        clip = pymupdf.Rect(*region_pdf.to_list())
        words = page.get_text("words", clip=clip, sort=True)
        unique: dict[tuple[object, ...], TextSpan] = {}
        for word in words:
            if not str(word[4]).strip():
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


class FirstAvailableTextReader:
    """Try readers in order and keep PDF text as the normal first adapter."""

    def __init__(self, *readers: LegendTextReader) -> None:
        if not readers:
            raise ValueError("at least one text reader is required")
        self._readers = readers

    def read(self, page: pymupdf.Page, region_pdf: BBox) -> Sequence[TextSpan]:
        for reader in self._readers:
            spans = tuple(reader.read(page, region_pdf))
            if spans:
                return spans
        return ()


def _intersects(first: BBox, second: BBox) -> bool:
    return (
        first.x0 < second.x1
        and first.x1 > second.x0
        and first.y0 < second.y1
        and first.y1 > second.y0
    )


def _union(boxes: Sequence[BBox]) -> BBox:
    return BBox(
        min(box.x0 for box in boxes),
        min(box.y0 for box in boxes),
        max(box.x1 for box in boxes),
        max(box.y1 for box in boxes),
    )


def _group_consecutive(values: Sequence[int]) -> list[list[int]]:
    groups: list[list[int]] = []
    for value in values:
        if not groups or value > groups[-1][-1] + 1:
            groups.append([value])
        else:
            groups[-1].append(value)
    return groups


def _separator_bands(
    page_image: Image.Image,
    region_pdf: BBox,
    transform: PageTransform,
) -> list[BBox]:
    """Infer rows from long horizontal rules inside the supplied region."""

    region_raster = transform.pdf_to_raster(region_pdf)
    region_image = crop_image(page_image, region_raster).convert("L")
    threshold = max(1, round(region_image.width * 0.55))
    dark_rows: list[int] = []
    pixels = region_image.load()
    for y in range(region_image.height):
        dark = sum(1 for x in range(region_image.width) if pixels[x, y] < 128)
        if dark >= threshold:
            dark_rows.append(y)
    groups = _group_consecutive(dark_rows)
    separators_pdf = [
        transform.crop_to_pdf(
            BBox(0, float(group[0]), 1, float(group[-1] + 1)),
            region_raster,
        ).y0
        + transform.crop_to_pdf(
            BBox(0, float(group[0]), 1, float(group[-1] + 1)),
            region_raster,
        ).height
        / 2
        for group in groups
    ]
    internal = [
        y
        for y in separators_pdf
        if region_pdf.y0 + 3 < y < region_pdf.y1 - 3
    ]
    boundaries = [region_pdf.y0, *internal, region_pdf.y1]
    return [
        BBox(region_pdf.x0, y0, region_pdf.x1, y1)
        for y0, y1 in zip(boundaries, boundaries[1:])
        if y1 - y0 >= 8
    ]


def _text_bands(spans: Sequence[TextSpan], region_pdf: BBox) -> list[BBox]:
    """Fallback row bands for borderless legends with readable text."""

    if not spans:
        return []
    ordered = sorted(spans, key=lambda span: ((span.bbox_pdf.y0 + span.bbox_pdf.y1) / 2, span.bbox_pdf.x0))
    tolerance = max(3.0, median(span.bbox_pdf.height for span in ordered) * 0.65)
    lines: list[list[TextSpan]] = []
    for span in ordered:
        center = (span.bbox_pdf.y0 + span.bbox_pdf.y1) / 2
        if not lines:
            lines.append([span])
            continue
        previous_center = median(
            (item.bbox_pdf.y0 + item.bbox_pdf.y1) / 2 for item in lines[-1]
        )
        if abs(center - previous_center) <= tolerance:
            lines[-1].append(span)
        else:
            lines.append([span])
    extents = [_union([span.bbox_pdf for span in line]) for line in lines]
    logical_extents: list[BBox] = []
    for extent in extents:
        if (
            logical_extents
            and extent.y0 <= logical_extents[-1].y1 + 2.5
            and abs(extent.x0 - logical_extents[-1].x0)
            <= max(8.0, region_pdf.width * 0.04)
        ):
            logical_extents[-1] = _union((logical_extents[-1], extent))
        else:
            logical_extents.append(extent)
    edge_padding = max(
        4.0,
        median(extent.height for extent in logical_extents) * 0.75,
    )
    boundaries = [max(region_pdf.y0, logical_extents[0].y0 - edge_padding)]
    boundaries.extend(
        (first.y1 + second.y0) / 2
        for first, second in zip(logical_extents, logical_extents[1:])
    )
    boundaries.append(
        min(region_pdf.y1, logical_extents[-1].y1 + edge_padding)
    )
    return [
        BBox(region_pdf.x0, y0, region_pdf.x1, y1)
        for y0, y1 in zip(boundaries, boundaries[1:])
        if y1 - y0 >= 4
    ]


def _row_content_bbox(row: BBox) -> BBox:
    x_padding = min(5.0, row.width * 0.04)
    y_padding = min(4.0, row.height * 0.1)
    return BBox(
        row.x0 + x_padding,
        row.y0 + y_padding,
        row.x1 - x_padding,
        row.y1 - y_padding,
    )


def _row_text(spans: Sequence[TextSpan]) -> str:
    if not spans:
        return ""
    ordered = sorted(
        spans,
        key=lambda span: (_span_center_y(span), span.bbox_pdf.x0),
    )
    tolerance = max(2.5, median(span.bbox_pdf.height for span in ordered) * 0.6)
    lines: list[list[TextSpan]] = []
    centers: list[float] = []
    for span in ordered:
        center = _span_center_y(span)
        if not lines or abs(center - centers[-1]) > tolerance:
            lines.append([span])
            centers.append(center)
        else:
            lines[-1].append(span)
            centers[-1] = median(_span_center_y(item) for item in lines[-1])
    return " ".join(
        span.text.strip()
        for line in lines
        for span in sorted(line, key=lambda item: item.bbox_pdf.x0)
    )


def _normalized_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _span_center_y(span: TextSpan) -> float:
    return (span.bbox_pdf.y0 + span.bbox_pdf.y1) / 2


def _valid_row_height(row: BBox, rows: Sequence[BBox], region: BBox) -> bool:
    heights = [item.height for item in rows]
    typical = median(heights)
    region_limit = region.height if len(rows) == 1 else region.height * 0.55
    return (
        row.height >= 8.0
        and row.height <= region_limit
        and row.height <= max(24.0, typical * 2.2)
    )


def _row_spans(spans: Sequence[TextSpan], row: BBox) -> tuple[TextSpan, ...]:
    """Assign each text span to one row by center, avoiding boundary leakage."""

    return tuple(
        span
        for span in spans
        if row.y0 <= _span_center_y(span) < row.y1
        and span.bbox_pdf.y0 >= row.y0 - 2.0
        and span.bbox_pdf.y1 <= row.y1 + 2.0
    )


def _label_spans(spans: Sequence[TextSpan], row: BBox) -> tuple[TextSpan, ...]:
    """Drop short symbol-column annotations such as elevation or network IDs."""

    ordered = sorted(spans, key=lambda span: span.bbox_pdf.x0)
    if len(ordered) < 2:
        return tuple(ordered)
    gaps = [
        (
            ordered[index + 1].bbox_pdf.x0 - ordered[index].bbox_pdf.x1,
            index,
        )
        for index in range(len(ordered) - 1)
    ]
    gap, split_index = max(gaps)
    right = ordered[split_index + 1 :]
    left = ordered[: split_index + 1]
    left_bbox = _union([span.bbox_pdf for span in left])
    if (
        gap >= max(8.0, median(span.bbox_pdf.height for span in ordered) * 0.65)
        and left_bbox.x1 <= row.x0 + row.width * 0.45
        and len(left) <= 3
        and sum(len(span.text.strip()) for span in left) <= 16
        and right
    ):
        return tuple(right)
    return tuple(ordered)


def extract_legend(
    document: str | Path,
    *,
    page_number: int,
    region_pdf: BBox,
    region_id: str,
    output_root: str | Path,
    reader: LegendTextReader | None = None,
    dpi: int = 144,
) -> list[LegendEntry]:
    """Extract rows/crops and write the S1 sidecar set for one page."""

    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if not isinstance(region_id, str) or not region_id.strip():
        raise ValueError("region_id must be non-empty")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("dpi must be a positive integer")

    source = Path(document)
    text_reader = reader or FirstAvailableTextReader(PdfTextReader())
    with pymupdf.open(source) as opened:
        if page_number > opened.page_count:
            raise ValueError(
                f"page_number {page_number} exceeds document page count {opened.page_count}"
            )
        page = opened[page_number - 1]
        page_box = BBox(0, 0, float(page.rect.width), float(page.rect.height))
        if not (
            page_box.x0 <= region_pdf.x0 < region_pdf.x1 <= page_box.x1
            and page_box.y0 <= region_pdf.y0 < region_pdf.y1 <= page_box.y1
        ):
            raise ValueError("legend region must be contained by the page")

        spans = tuple(
            span
            for span in text_reader.read(page, region_pdf)
            if _intersects(span.bbox_pdf, region_pdf)
        )
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        page_image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        transform = PageTransform(
            pdf_width=float(page.rect.width),
            pdf_height=float(page.rect.height),
            raster_width=pixmap.width,
            raster_height=pixmap.height,
        )
        row_bands = _separator_bands(page_image, region_pdf, transform)
        valid_separator_bands = sum(
            _valid_row_height(row, row_bands, region_pdf) for row in row_bands
        )
        if len(row_bands) <= 1 or valid_separator_bands < 2:
            row_bands = _text_bands(spans, region_pdf)

        page_dir = page_sidecar_dir(output_root, page_number)
        entries: list[LegendEntry] = []
        for index, row_band in enumerate(row_bands, start=1):
            if not _valid_row_height(row_band, row_bands, region_pdf):
                continue
            row_bbox = _row_content_bbox(row_band)
            row_spans = _row_spans(spans, row_band)
            row_text = _row_text(row_spans) if row_spans else None
            if row_text is not None and _NON_ENTRY_HEADING_RE.search(row_text):
                continue
            row_spans = _label_spans(row_spans, row_bbox)
            row_text = _row_text(row_spans) if row_spans else None
            if (
                len(row_spans) > MAX_ROW_TEXT_SPANS
                or (row_text is not None and len(row_text) > MAX_ROW_TEXT_CHARS)
            ):
                continue
            text_bbox = _union([span.bbox_pdf for span in row_spans]) if row_spans else None
            symbol_right = (
                max(row_bbox.x0 + 1, min(row_bbox.x1, text_bbox.x0 - 5))
                if text_bbox
                else row_bbox.x0 + row_bbox.width * 0.35
            )
            if symbol_right - row_bbox.x0 < MIN_SYMBOL_WIDTH_PT:
                continue
            symbol_bbox = BBox(row_bbox.x0, row_bbox.y0, symbol_right, row_bbox.y1)
            if text_bbox is not None and _intersects(symbol_bbox, text_bbox):
                continue
            name_raw = row_text
            entry_id = stable_id(
                "LE",
                page_number,
                region_id,
                index,
                [round(value, 3) for value in row_bbox.to_list()],
            )
            raw_name = f"legend/{entry_id}.raw.png"
            normalized_name = f"legend/{entry_id}.normalized.png"
            raw_image = crop_image(page_image, transform.pdf_to_raster(row_bbox))
            symbol_image = crop_image(page_image, transform.pdf_to_raster(symbol_bbox))
            write_crop(page_dir, raw_name, png_bytes(raw_image))
            write_crop(
                page_dir,
                normalized_name,
                png_bytes(normalize_symbol_crop(symbol_image)),
            )
            source_kind = (
                row_spans[0].source_kind
                if row_spans and len({span.source_kind for span in row_spans}) == 1
                else ("mixed_text" if row_spans else "layout_only")
            )
            confidence = (
                sum(span.confidence for span in row_spans) / len(row_spans)
                if row_spans
                else 0.0
            )
            entries.append(
                LegendEntry(
                    id=entry_id,
                    page=page_number,
                    region_id=region_id,
                    position=str(index),
                    name_raw=name_raw,
                    name_normalized=_normalized_name(name_raw) if name_raw else None,
                    bbox_pdf=row_bbox,
                    symbol_bbox_pdf=symbol_bbox,
                    text_bbox_pdf=text_bbox,
                    raw_crop=f"symbol_crops/{raw_name}",
                    normalized_crop=f"symbol_crops/{normalized_name}",
                    source_kind=source_kind,
                    source_document=source.name,
                    status="extracted" if name_raw else "text_unreadable",
                    confidence=confidence,
                )
            )

    write_page_artifacts(
        output_root,
        PageArtifacts(page=page_number, legend_entries=entries),
    )
    return entries


def _bbox_argument(value: str) -> BBox:
    try:
        coordinates = [float(item.strip()) for item in value.split(",")]
        return BBox.from_list(coordinates)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "region must be four comma-separated numbers: x0,y0,x1,y1"
        ) from exc


def _gt_region(path: Path, page_number: int) -> tuple[BBox, str]:
    document = load_document(path)
    try:
        page = document["pages"][page_number - 1]
    except IndexError as exc:
        raise ValueError(f"GT has no page {page_number}") from exc
    region = page["legend"]["region"]
    if region["status"] != "found":
        raise ValueError(f"GT page {page_number} has no legend region")
    return BBox.from_list(region["bbox"]), str(region["id"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract legend rows from an explicit PDF-point region."
    )
    parser.add_argument("document", type=Path)
    parser.add_argument("--page", type=int, default=1)
    region_group = parser.add_mutually_exclusive_group(required=True)
    region_group.add_argument("--region", type=_bbox_argument)
    region_group.add_argument("--gt", type=Path, help="fixture GT supplying the region")
    parser.add_argument("--region-id", default="legend-main")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=144)
    args = parser.parse_args(argv)

    if args.gt:
        region, region_id = _gt_region(args.gt, args.page)
    else:
        region, region_id = args.region, args.region_id
    entries = extract_legend(
        args.document,
        page_number=args.page,
        region_pdf=region,
        region_id=region_id,
        output_root=args.output,
        dpi=args.dpi,
    )
    print(
        json.dumps(
            {
                "page": args.page,
                "regionId": region_id,
                "legendEntryCount": len(entries),
                "statuses": [entry.status for entry in entries],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
