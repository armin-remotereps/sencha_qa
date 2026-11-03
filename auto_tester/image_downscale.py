"""Downscale an image for a vision model (pure, no Django).

Re-encodes any image Pillow can decode as a JPEG whose longest side is at
most ``max_side`` pixels. It bounds the tokens and the memory a model call
costs; it never judges the image.
"""

from __future__ import annotations

import io
import warnings

from PIL import Image, UnidentifiedImageError

# Refuse decompression bombs outright instead of only warning about them.
_MAX_PIXELS = 80_000_000


def downscale_for_model(
    data: bytes, *, max_side: int = 1280, quality: int = 80
) -> bytes:
    """Re-encode as JPEG with the longest side <= max_side; ValueError on undecodable data."""
    if max_side < 1:
        raise ValueError("max_side must be positive.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                width, height = source.size
                if width < 1 or height < 1 or width * height > _MAX_PIXELS:
                    raise ValueError("The image has an unusable size.")
                source.draft("RGB", (max_side, max_side))
                image = source.convert("RGB")
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
    ) as exc:
        raise ValueError("The image could not be decoded.") from exc
    with image:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()
