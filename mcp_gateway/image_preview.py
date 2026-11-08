"""A small JPEG preview of a captured screenshot (pure Pillow, no database).

``capture_screenshot`` returns it as MCP image content so the client can
check the shot (right window, expected result) before it marks a step. The
preview is never stored; the staged shot keeps the full image. It is built
from the image ``load_manual_image`` already decoded (``stage_shot``, under
``DECODE_SLOTS``), so the bytes are decoded once. ``take_screenshot``
(``machine_screenshot``) passes its own, larger caps.
"""

from __future__ import annotations

import io
from typing import Final

from PIL import Image

from mcp_gateway.limits import (
    MCP_CAPTURE_PREVIEW_JPEG_QUALITY,
    MCP_CAPTURE_PREVIEW_MAX_BYTES,
    MCP_CAPTURE_PREVIEW_MAX_EDGE,
)

# Tried in order at each size before the size steps down.
_QUALITIES: Final[tuple[int, ...]] = (MCP_CAPTURE_PREVIEW_JPEG_QUALITY, 55, 40)
_EDGE_STEP: Final = 0.75
_MIN_EDGE: Final = 160
_WHITE: Final = (255, 255, 255)
# Modes Pillow resizes with nearest neighbour only: shrunk to twice the
# preview edge that way, converted, then resampled properly.
_NEAREST_ONLY: Final = frozenset({"1", "P"})


def make_preview(
    image: Image.Image,
    *,
    max_edge: int = MCP_CAPTURE_PREVIEW_MAX_EDGE,
    max_bytes: int = MCP_CAPTURE_PREVIEW_MAX_BYTES,
) -> bytes:
    """A JPEG of the loaded ``image`` with its long edge at most ``max_edge``
    and at most ``max_bytes`` (defaults: the capture preview caps).

    Shrinks ``image`` in place first (the caller owns and closes it), so the
    colour conversion and the quality loop only ever handle an image of at
    most ``max_edge`` pixels. Steps the quality, then the size, down until
    it fits; the smallest attempt is returned if nothing fits (cannot happen
    at the minimum edge).
    """
    edge = max_edge
    if image.mode in _NEAREST_ONLY:
        image.thumbnail((2 * edge, 2 * edge), Image.Resampling.NEAREST)
        small = _to_rgb(image)
        small.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    else:
        image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        small = _to_rgb(image)
    smallest = b""
    while True:
        scaled = small.copy()
        scaled.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        for quality in _QUALITIES:
            encoded = _jpeg(scaled, quality)
            if len(encoded) <= max_bytes:
                return encoded
            if not smallest or len(encoded) < len(smallest):
                smallest = encoded
        if edge <= _MIN_EDGE:
            return smallest
        edge = max(_MIN_EDGE, int(edge * _EDGE_STEP))


def preview_size(jpeg: bytes) -> tuple[int, int]:
    """Width and height of a ``make_preview`` JPEG (reads the header only)."""
    with Image.open(io.BytesIO(jpeg), formats=["JPEG"]) as image:
        width, height = image.size
    return int(width), int(height)


def _to_rgb(image: Image.Image) -> Image.Image:
    """RGB, with any transparency flattened onto white."""
    if image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, _WHITE)
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat
    return image.convert("RGB")


def _jpeg(image: Image.Image, quality: int) -> bytes:
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()
