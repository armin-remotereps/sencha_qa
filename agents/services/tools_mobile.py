"""Tools that exist only on a phone or tablet runner.

The shared tools (tap = ``click``, ``scroll`` as a swipe, ``type_text``, ...)
stay in ``tools_controller``; the runner maps them onto the device.
``long_press`` reuses the verified element flow of ``click``; ``swipe`` and
``rotate_device`` run one before/after outcome check only when an
``expected_result`` is given, like ``click_at``; the rest are plain.
"""

from __future__ import annotations

from collections.abc import Callable

from agents.services.tool_utils import safe_tool_call
from agents.services.tools_controller import ensure_action_succeeded, verified_click
from agents.services.verified_element_actions import (
    VerificationBudget,
    confirm_action,
    format_unconfirmed_message,
    take_before_screenshot,
)
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


def _act_and_check(
    project_id: int,
    *,
    tool_name: str,
    act: Callable[[], ActionResult],
    success_message: str,
    action_summary: str,
    expected_result: str,
    vision_config: LLMConfig | None,
    on_screenshot: ScreenshotCallback | None,
    cancellation_check: CancellationCheck | None,
    deadline: float | None,
) -> ToolResult:
    """Run ``act`` once; with ``expected_result``, compare the screen before
    and after once and report it (a miss is an error, never retried)."""

    def _do() -> ToolResult:
        budget = VerificationBudget(
            deadline=deadline, cancellation_check=cancellation_check
        )
        budget.raise_if_cancelled()
        if not expected_result:
            ensure_action_succeeded(act())
            return ToolResult(
                tool_call_id="",
                content=f"{success_message}. Take a screenshot to see the result.",
                is_error=False,
            )
        if vision_config is None:
            return ToolResult(
                tool_call_id="",
                content="Vision model not configured, so expected_result cannot "
                f"be checked. Call {tool_name} without it.",
                is_error=True,
            )
        before_image = take_before_screenshot(project_id)
        ensure_action_succeeded(act())
        verdict = confirm_action(
            project_id,
            vision_config,
            budget,
            acted_on=(),
            before_image_base64=before_image,
            action_summary=action_summary,
            expected_result=expected_result,
            tool_name=tool_name,
            on_screenshot=on_screenshot,
        )
        if verdict.status == "confirmed":
            return ToolResult(
                tool_call_id="",
                content=f"{success_message}; expected result confirmed.",
                is_error=False,
            )
        if verdict.status == "unexpected":
            return ToolResult(
                tool_call_id="",
                content=format_unconfirmed_message(
                    success_message, verdict.reason, budget
                ),
                is_error=False,
            )
        reason = " ".join(verdict.reason.split()).rstrip(".")
        return ToolResult(
            tool_call_id="",
            content=(
                f"{success_message}, but the expected result is NOT shown: "
                f"{reason}. Take a screenshot before trying again."
            ),
            is_error=True,
        )

    return safe_tool_call(tool_name, _do)


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
    return _act_and_check(
        project_id,
        tool_name="swipe",
        act=lambda: controller_swipe(project_id, direction, x, y, distance, duration),
        success_message=summary[0].upper() + summary[1:],
        action_summary=summary,
        expected_result=expected_result,
        vision_config=vision_config,
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
    return _act_and_check(
        project_id,
        tool_name="rotate_device",
        act=lambda: controller_rotate_device(project_id, orientation),
        success_message=(
            f"Rotated the device to {orientation.value}; screenshot coordinates "
            "have changed"
        ),
        action_summary=f"rotated the device to {orientation.value}",
        expected_result=expected_result,
        vision_config=vision_config,
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
