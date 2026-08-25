"""Deterministic image crops used by the legend vertical slice."""

from __future__ import annotations

from io import BytesIO
import math

from PIL import Image, ImageChops, ImageOps

from .geometry import BBox


NORMALIZED_CROP_SIZE = (96, 96)


def crop_image(image: Image.Image, bbox: BBox) -> Image.Image:
    """Crop a pixel bbox, rounding outwards and clipping to the image."""

    left = max(0, math.floor(bbox.x0))
    top = max(0, math.floor(bbox.y0))
    right = min(image.width, math.ceil(bbox.x1))
    bottom = min(image.height, math.ceil(bbox.y1))
    if left >= right or top >= bottom:
        raise ValueError("crop bbox does not intersect the image")
    return image.crop((left, top, right, bottom))


def normalize_symbol_crop(
    image: Image.Image,
    *,
    size: tuple[int, int] = NORMALIZED_CROP_SIZE,
    padding: int = 8,
) -> Image.Image:
    """Place visible ink on a stable white grayscale canvas."""

    if (
        len(size) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in size)
    ):
        raise ValueError("size must contain two positive integers")
    if isinstance(padding, bool) or not isinstance(padding, int) or padding < 0:
        raise ValueError("padding must be a non-negative integer")
    if size[0] <= 2 * padding or size[1] <= 2 * padding:
        raise ValueError("padding leaves no normalized crop area")

    grayscale = ImageOps.autocontrast(image.convert("L"))
    background = Image.new("L", grayscale.size, 255)
    ink_bbox = ImageChops.difference(grayscale, background).getbbox()
    canvas = Image.new("L", size, 255)
    if ink_bbox is None:
        return canvas

    ink = grayscale.crop(ink_bbox)
    available = (size[0] - 2 * padding, size[1] - 2 * padding)
    scale = min(available[0] / ink.width, available[1] / ink.height)
    resized_size = (
        max(1, round(ink.width * scale)),
        max(1, round(ink.height * scale)),
    )
    ink = ink.resize(resized_size, Image.Resampling.LANCZOS)
    offset = ((size[0] - ink.width) // 2, (size[1] - ink.height) // 2)
    canvas.paste(ink, offset)
    return canvas


def png_bytes(image: Image.Image) -> bytes:
    """Encode a crop reproducibly without filesystem metadata."""

    output = BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()
