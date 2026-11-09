"""Tools that exist only on a phone or tablet runner.

The shared tools (tap = ``click``, ``scroll`` as a swipe, ``type_text``, ...)
stay in ``tools_controller``; the runner maps them onto the device.
``long_press`` reuses the verified element flow of ``click``; ``swipe``,
``rotate_device`` and the coordinate tool ``long_press_at`` act once through
``act_and_check.act_once_and_check`` and run one before/after outcome check
only when an ``expected_result`` is given, like ``click_at``; the rest are
plain.
"""

from __future__ import annotations

from collections.abc import Callable

from agents.services.act_and_check import COORDINATE_MISS_HINT, act_once_and_check
from agents.services.tool_utils import safe_tool_call
from agents.services.tools_controller import ensure_action_succeeded, verified_click
from agents.types import CancellationCheck, LLMConfig, ScreenshotCallback, ToolResult
from controller_client.protocol import DeviceOrientation, MobileButton, ScrollDirection
from projects.controller_mobile_actions import (
    controller_hide_keyboard,
    controller_install_app,
    controller_long_press,
    controller_open_url,
    controller_press_button,
    controller_rotate_device,
    controller_swipe,
)
from projects.services import ActionResult


def _default_long_press_expectation(description: str) -> str:
    return (
        f"the long press landed on '{description}' and the app reacted to it "
        "(context menu, selection, drag mode, preview, or similar)"
    )


def long_press(
    project_id: int,
    *,
    description: str,
    duration: float,
    vision_config: LLMConfig,
    expected_result: str = "",
    on_screenshot: ScreenshotCallback | None = None,
    cancellation_check: CancellationCheck | None = None,
    deadline: float | None = None,
) -> ToolResult:
    def _press(x: int, y: int) -> ActionResult:
        return controller_long_press(project_id, x, y, duration)

    return verified_click(
        project_id,
        tool_name="long_press",
        description=description,
        vision_config=vision_config,
        expected_result=expected_result,
        press=_press,
        verb="long-pressed",
        success_verb="Long-pressed",
        default_expectation=_default_long_press_expectation,
        on_screenshot=on_screenshot,
        cancellation_check=cancellation_check,
        deadline=deadline,
    )


_GESTURE_DONE_SUFFIX = ". Take a screenshot to see the result."
_GESTURE_MISS_HINT = "Take a screenshot before trying again."


def long_press_at(
    project_id: int,
    *,
    x: int,
    y: int,
    duration: float,
    vision_config: LLMConfig | None,
    expected_result: str = "",
    on_screenshot: ScreenshotCallback | None = None,
    cancellation_check: CancellationCheck | None = None,
    deadline: float | None = None,
) -> ToolResult:
    """Long-press screenshot pixel (x, y) for ``duration`` seconds."""
    summary = f"long-pressed at ({x}, {y}) for {duration:g}s"
    return act_once_and_check(
        project_id,
        tool_name="long_press_at",
        act=lambda: ensure_action_succeeded(
            controller_long_press(project_id, x, y, duration)
        ),
        success_message=summary[0].upper() + summary[1:],
        action_summary=summary,
        expected_result=expected_result,
        vision_config=vision_config,
        done_suffix=".",
        miss_hint=COORDINATE_MISS_HINT,
        on_screenshot=on_screenshot,
        cancellation_check=cancellation_check,
        deadline=deadline,
    )


def swipe(
    project_id: int,
    *,
    direction: ScrollDirection,
    x: int | None,
    y: int | None,
    distance: float,
    duration: float,
    vision_config: LLMConfig | None,
    expected_result: str = "",
    on_screenshot: ScreenshotCallback | None = None,
    cancellation_check: CancellationCheck | None = None,
    deadline: float | None = None,
) -> ToolResult:
    start = "from the centre" if x is None or y is None else f"from ({x}, {y})"
    summary = f"swiped {direction.value} {start} over {distance:g} of the screen"
    return act_once_and_check(
        project_id,
        tool_name="swipe",
        act=lambda: ensure_action_succeeded(
            controller_swipe(project_id, direction, x, y, distance, duration)
        ),
        success_message=summary[0].upper() + summary[1:],
        action_summary=summary,
        expected_result=expected_result,
        vision_config=vision_config,
        done_suffix=_GESTURE_DONE_SUFFIX,
        miss_hint=_GESTURE_MISS_HINT,
        on_screenshot=on_screenshot,
        cancellation_check=cancellation_check,
        deadline=deadline,
    )


def rotate_device(
    project_id: int,
    *,
    orientation: DeviceOrientation,
    vision_config: LLMConfig | None,
    expected_result: str = "",
    on_screenshot: ScreenshotCallback | None = None,
    cancellation_check: CancellationCheck | None = None,
    deadline: float | None = None,
) -> ToolResult:
    return act_once_and_check(
        project_id,
        tool_name="rotate_device",
        act=lambda: ensure_action_succeeded(
            controller_rotate_device(project_id, orientation)
        ),
        success_message=(
            f"Rotated the device to {orientation.value}; screenshot coordinates "
            "have changed"
        ),
        action_summary=f"rotated the device to {orientation.value}",
        expected_result=expected_result,
        vision_config=vision_config,
        done_suffix=_GESTURE_DONE_SUFFIX,
        miss_hint=_GESTURE_MISS_HINT,
        on_screenshot=on_screenshot,
        cancellation_check=cancellation_check,
        deadline=deadline,
    )


def _plain(operation: str, act: Callable[[], ActionResult]) -> ToolResult:
    """The runner's own message; a failed action is a tool error."""

    def _do() -> ToolResult:
        result = act()
        return ToolResult(
            tool_call_id="",
            content=result["message"] or f"{operation} done.",
            is_error=not result["success"],
        )

    return safe_tool_call(operation, _do)


def press_button(project_id: int, *, button: MobileButton) -> ToolResult:
    return _plain("press_button", lambda: controller_press_button(project_id, button))


def hide_keyboard(project_id: int) -> ToolResult:
    return _plain("hide_keyboard", lambda: controller_hide_keyboard(project_id))


def open_url(project_id: int, *, url: str) -> ToolResult:
    return _plain("open_url", lambda: controller_open_url(project_id, url))


def install_app(project_id: int, *, path: str) -> ToolResult:
    return _plain("install_app", lambda: controller_install_app(project_id, path))
