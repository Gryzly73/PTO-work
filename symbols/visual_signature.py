"""Deterministic visual signatures for normalized open-set symbol crops."""

from __future__ import annotations

from PIL import Image, ImageOps

from .schema import VisualSignature


SIGNATURE_METHOD = "shape-topology-grid"
SIGNATURE_VERSION = 2
_GRID_SIZE = 24
_BLOCK_GRID_SIZE = 8
_BIT_COUNT = _GRID_SIZE * _GRID_SIZE * 2 + _BLOCK_GRID_SIZE**2 * 2


def _append_bit(bits: int, value: bool) -> int:
    return (bits << 1) | int(value)


def compute_visual_signature(image: Image.Image) -> VisualSignature:
    """Encode ink, contour topology, and coarse fill into one stable signature."""

    grayscale = ImageOps.autocontrast(image.convert("L"))
    reduced = grayscale.resize(
        (_GRID_SIZE, _GRID_SIZE),
        Image.Resampling.LANCZOS,
    )
    pixels = tuple(reduced.getdata())
    threshold = min(240, int(sum(pixels) / len(pixels)))
    mask = tuple(value < threshold for value in pixels)
    bits = 0
    for ink in mask:
        bits = _append_bit(bits, ink)

    for index, ink in enumerate(mask):
        if not ink:
            bits = _append_bit(bits, False)
            continue
        y, x = divmod(index, _GRID_SIZE)
        contour = (
            x == 0
            or y == 0
            or x == _GRID_SIZE - 1
            or y == _GRID_SIZE - 1
            or not mask[index - 1]
            or not mask[index + 1]
            or not mask[index - _GRID_SIZE]
            or not mask[index + _GRID_SIZE]
        )
        bits = _append_bit(bits, contour)

    block_size = _GRID_SIZE // _BLOCK_GRID_SIZE
    for block_y in range(_BLOCK_GRID_SIZE):
        for block_x in range(_BLOCK_GRID_SIZE):
            ink_count = sum(
                mask[y * _GRID_SIZE + x]
                for y in range(
                    block_y * block_size, (block_y + 1) * block_size
                )
                for x in range(
                    block_x * block_size, (block_x + 1) * block_size
                )
            )
            level = round(ink_count * 3 / (block_size * block_size))
            bits = (bits << 2) | level
    return VisualSignature(
        method=SIGNATURE_METHOD,
        version=SIGNATURE_VERSION,
        value=f"{bits:0{_BIT_COUNT // 4}x}",
    )


def signature_similarity(first: VisualSignature, second: VisualSignature) -> float:
    """Return normalized Hamming similarity for compatible signatures."""

    if first.method != second.method or first.version != second.version:
        return 0.0
    try:
        first_bits = int(first.value, 16)
        second_bits = int(second.value, 16)
    except ValueError:
        return 0.0
    width = max(len(first.value), len(second.value)) * 4
    if width == 0:
        return 0.0
    return 1.0 - (first_bits ^ second_bits).bit_count() / width
