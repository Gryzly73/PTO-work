"""Coordinate primitives for symbol sidecars.

All persisted boxes use page-relative PDF points with a top-left origin.
Raster boxes use pixels with the same origin after the render rotation.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


class GeometryError(ValueError):
    """Invalid geometry or coordinate transform."""


@dataclass(frozen=True, slots=True)
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        values = (self.x0, self.y0, self.x1, self.y1)
        if not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in values
        ):
            raise GeometryError("bbox coordinates must be finite numbers")
        if self.x0 >= self.x1 or self.y0 >= self.y1:
            raise GeometryError("bbox must have x0 < x1 and y0 < y1")

    @classmethod
    def from_list(cls, value: Iterable[float]) -> "BBox":
        coordinates = tuple(value)
        if len(coordinates) != 4:
            raise GeometryError("bbox must contain [x0, y0, x1, y1]")
        return cls(*coordinates)

    def to_list(self) -> list[float]:
        return [float(self.x0), float(self.y0), float(self.x1), float(self.y1)]

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass(frozen=True, slots=True)
class PageTransform:
    """Bidirectional PDF/raster transform matching ``render_page``.

    ``rotation`` is the clockwise OSD correction passed to Pillow as
    ``image.rotate(-rotation, expand=True)``. ``raster_width`` and
    ``raster_height`` describe the image before that correction.
    """

    pdf_width: float
    pdf_height: float
    raster_width: int
    raster_height: int
    rotation: int = 0

    def __post_init__(self) -> None:
        dimensions = (
            self.pdf_width,
            self.pdf_height,
            self.raster_width,
            self.raster_height,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in dimensions
        ):
            raise GeometryError("page and raster dimensions must be positive")
        if self.rotation not in {0, 90, 180, 270}:
            raise GeometryError("rotation must be 0, 90, 180, or 270")

    @property
    def output_size(self) -> tuple[int, int]:
        if self.rotation in {90, 270}:
            return self.raster_height, self.raster_width
        return self.raster_width, self.raster_height

    def pdf_to_raster(self, bbox: BBox) -> BBox:
        scaled = BBox(
            bbox.x0 * self.raster_width / self.pdf_width,
            bbox.y0 * self.raster_height / self.pdf_height,
            bbox.x1 * self.raster_width / self.pdf_width,
            bbox.y1 * self.raster_height / self.pdf_height,
        )
        return self._rotate_clockwise(scaled)

    def raster_to_pdf(self, bbox: BBox) -> BBox:
        unrotated = self._unrotate_clockwise(bbox)
        return BBox(
            unrotated.x0 * self.pdf_width / self.raster_width,
            unrotated.y0 * self.pdf_height / self.raster_height,
            unrotated.x1 * self.pdf_width / self.raster_width,
            unrotated.y1 * self.pdf_height / self.raster_height,
        )

    def raster_to_crop(self, bbox: BBox, crop_bbox: BBox) -> BBox:
        """Convert an absolute rendered-page box into crop-local pixels."""

        return BBox(
            bbox.x0 - crop_bbox.x0,
            bbox.y0 - crop_bbox.y0,
            bbox.x1 - crop_bbox.x0,
            bbox.y1 - crop_bbox.y0,
        )

    def crop_to_raster(self, bbox: BBox, crop_bbox: BBox) -> BBox:
        return BBox(
            bbox.x0 + crop_bbox.x0,
            bbox.y0 + crop_bbox.y0,
            bbox.x1 + crop_bbox.x0,
            bbox.y1 + crop_bbox.y0,
        )

    def pdf_to_crop(self, bbox: BBox, crop_bbox: BBox) -> BBox:
        return self.raster_to_crop(self.pdf_to_raster(bbox), crop_bbox)

    def crop_to_pdf(self, bbox: BBox, crop_bbox: BBox) -> BBox:
        return self.raster_to_pdf(self.crop_to_raster(bbox, crop_bbox))

    def _rotate_clockwise(self, bbox: BBox) -> BBox:
        width = float(self.raster_width)
        height = float(self.raster_height)
        if self.rotation == 0:
            return bbox
        if self.rotation == 90:
            return BBox(height - bbox.y1, bbox.x0, height - bbox.y0, bbox.x1)
        if self.rotation == 180:
            return BBox(
                width - bbox.x1,
                height - bbox.y1,
                width - bbox.x0,
                height - bbox.y0,
            )
        return BBox(bbox.y0, width - bbox.x1, bbox.y1, width - bbox.x0)

    def _unrotate_clockwise(self, bbox: BBox) -> BBox:
        width = float(self.raster_width)
        height = float(self.raster_height)
        if self.rotation == 0:
            return bbox
        if self.rotation == 90:
            return BBox(bbox.y0, height - bbox.x1, bbox.y1, height - bbox.x0)
        if self.rotation == 180:
            return BBox(
                width - bbox.x1,
                height - bbox.y1,
                width - bbox.x0,
                height - bbox.y0,
            )
        return BBox(width - bbox.y1, bbox.x0, width - bbox.y0, bbox.x1)
