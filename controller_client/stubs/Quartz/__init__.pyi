# Local stub for pyobjc-framework-Quartz, which ships no type information.
# It covers only what the controller uses: posting mouse events with an
# explicit click state (executor double-click) and listing windows
# (app_closer_macos). Extend it when the controller uses more of Quartz.
from collections.abc import Mapping, Sequence
from typing import Final, NewType

CGEventRef = NewType("CGEventRef", object)

class CGPoint:
    x: float
    y: float

def CGPointMake(x: float, y: float) -> CGPoint: ...
def CGEventCreateMouseEvent(
    source: None, mouseType: int, mouseCursorPosition: CGPoint, mouseButton: int
) -> CGEventRef | None: ...
def CGEventSetIntegerValueField(event: CGEventRef, field: int, value: int) -> None: ...
def CGEventGetIntegerValueField(event: CGEventRef, field: int) -> int: ...
def CGEventPost(tap: int, event: CGEventRef) -> None: ...
def CGWindowListCopyWindowInfo(
    option: int, relativeToWindow: int
) -> Sequence[Mapping[str, object]] | None: ...

kCGEventLeftMouseDown: Final[int]
kCGEventLeftMouseUp: Final[int]
kCGMouseButtonLeft: Final[int]
kCGMouseEventClickState: Final[int]
kCGHIDEventTap: Final[int]

kCGNullWindowID: Final[int]
kCGWindowListOptionAll: Final[int]
kCGWindowListOptionOnScreenOnly: Final[int]
kCGWindowListExcludeDesktopElements: Final[int]
kCGWindowLayer: Final[str]
kCGWindowOwnerPID: Final[str]
