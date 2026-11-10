"""Run one input action, then optionally check its outcome once.

Shared by the coordinate tools (``click_at`` and the other ``*_at`` tools) and
the phone gestures ``swipe`` / ``rotate_device``: there is no element to
resolve, so the action runs once; only with an ``expected_result`` is the
screen compared before and after, and a miss is reported, never retried.

A leaf module: ``tools_controller`` and ``tools_mobile`` both import it, so it
must never import either of them.
"""

from __future__ import annotations

from collections.abc import Callable

from agents.services.tool_utils import safe_tool_call
from agents.services.verified_element_actions import (
    VerificationBudget,
    confirm_action,
    format_unconfirmed_message,
    take_before_screenshot,
)
from agents.types import CancellationCheck, LLMConfig, ScreenshotCallback, ToolResult
from runners.types import RunnerId

# The miss hint of the coordinate tools (click_at and the other *_at tools).
COORDINATE_MISS_HINT = "Take a screenshot and ask for the target's coordinates again."


def act_once_and_check(
    runner_id: RunnerId,
    *,
    tool_name: str,
    act: Callable[[], None],
    success_message: str,
    action_summary: str,
    expected_result: str,
    vision_config: LLMConfig | None,
    done_suffix: str,
    miss_hint: str,
    on_screenshot: ScreenshotCallback | None,
    cancellation_check: CancellationCheck | None,
    deadline: float | None,
    precheck: Callable[[], None] | None = None,
) -> ToolResult:
    """Run ``act`` once; with ``expected_result``, check the outcome once.

    ``act`` raises on a failed action. Without ``expected_result`` the result is
    ``success_message + done_suffix``; a confirmed outcome adds "expected result
    confirmed", a changed screen without it is reported (not an error) and a
    miss is an error ending with ``miss_hint``.
    """

    def _do() -> ToolResult:
        if precheck is not None:
            precheck()
        budget = VerificationBudget(
            deadline=deadline, cancellation_check=cancellation_check
        )
        budget.raise_if_cancelled()
        if not expected_result:
            act()
            return ToolResult(
                tool_call_id="",
                content=f"{success_message}{done_suffix}",
                is_error=False,
            )
        if vision_config is None:
            return ToolResult(
                tool_call_id="",
                content="Vision model not configured, so expected_result cannot "
                f"be checked. Call {tool_name} without it.",
                is_error=True,
            )
        before_image = take_before_screenshot(runner_id)
        act()
        verdict = confirm_action(
            runner_id,
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
                f"{reason}. {miss_hint}"
            ),
            is_error=True,
        )

    return safe_tool_call(tool_name, _do)
