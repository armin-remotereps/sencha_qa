"""Map screenshot pixels to the coordinate space pointer input takes.

The server sends coordinates read from a screenshot. On a Retina Mac, a Windows
display whose DPI scaling the process does not follow, or any screen where the
capture and the input differ in size, the same point has other coordinates for
the input backend. Each axis is scaled on its own; equal sizes change nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from controller_client.exceptions import ExecutionError


@dataclass(frozen=True)
class ScreenSpaces:
    capture: tuple[int, int]
    input: tuple[int, int]


def map_to_input(x: int, y: int, spaces: ScreenSpaces) -> tuple[int, int]:
    capture_w, capture_h = spaces.capture
    input_w, input_h = spaces.input
    if capture_w <= 0 or capture_h <= 0 or input_w <= 0 or input_h <= 0:
        raise ExecutionError(
            "The screen size is unknown, so the coordinates cannot be mapped. "
            "Take a screenshot and try again."
        )
    if not (0 <= x < capture_w and 0 <= y < capture_h):
        raise ExecutionError(
            f"Coordinates ({x}, {y}) are outside the screen, which is "
            f"{capture_w}x{capture_h} pixels as in the latest screenshot. Use "
            "coordinates from a current screenshot."
        )
    if spaces.capture == spaces.input:
        return x, y
    return _scale(x, capture_w, input_w), _scale(y, capture_h, input_h)


def _scale(value: int, capture: int, target: int) -> int:
    # Map the pixel's centre, so a point never drifts towards the origin.
    return min(target - 1, math.floor((value + 0.5) * target / capture))
