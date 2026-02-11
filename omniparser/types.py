from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BBox:
    x_min: float
    y_min: float
    x_max: float
    y_max: float


@dataclass(frozen=True)
class UIElement:
    index: int
    type: str
    content: str
    bbox: BBox
    center_x: float
    center_y: float
    interactivity: bool


@dataclass(frozen=True)
class PixelBBox:
    x_min: int
    y_min: int
    x_max: int
    y_max: int


@dataclass(frozen=True)
class PixelUIElement:
    index: int
    type: str
    content: str
    bbox: PixelBBox
    center_x: int
    center_y: int
    interactivity: bool


@dataclass(frozen=True)
class ParseResult:
    annotated_image: str
    elements: tuple[UIElement, ...]
    image_width: int
    image_height: int


@dataclass(frozen=True)
class PixelParseResult:
    annotated_image: str
    elements: tuple[PixelUIElement, ...]
    image_width: int
    image_height: int
