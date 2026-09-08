from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, Literal

from agents.exceptions import ActionVerificationError, RejectedElementChosenError
from agents.services.action_verifier import verify_action_outcome, verify_candidate
from agents.services.controller_element_finder import match_element, parse_screen
from agents.types import (
    AgentCancelledError,
    CancellationCheck,
    LLMConfig,
    PixelUIElement,
    RejectedCandidate,
    ScreenshotCallback,
    Verdict,
)
from projects.services import controller_screenshot

MAX_VERIFY_ATTEMPTS: Final = 10
POST_ACTION_SETTLE_SECONDS: Final = 1.0
_POSITION_TOLERANCE_PX: Final = 10
_MIN_REMAINING_SECONDS: Final = 5.0
_MATCHER_REPEAT_REASON: Final = "matcher picked an element that was already ruled out"


@dataclass
class VerificationBudget:
    max_attempts: int = MAX_VERIFY_ATTEMPTS
    used: int = 0
    rejections: list[RejectedCandidate] = field(default_factory=list)
    deadline: float | None = None
    cancellation_check: CancellationCheck | None = None

    def raise_if_cancelled(self) -> None:
        _raise_if_cancelled(self.cancellation_check)

    def consume(self, description: str) -> None:
        """Reserve one verification attempt, raising when none remain."""
        self.raise_if_cancelled()
        if self.used >= self.max_attempts or self._deadline_exhausted():
            raise ActionVerificationError(self._exhausted_message(description))
        self.used += 1

    def reject(
        self,
        element: PixelUIElement,
        stage: Literal["candidate", "outcome"],
        reason: str,
    ) -> None:
        """Record a rejected candidate or outcome for the verification history."""
        self.rejections.append(
            RejectedCandidate(
                attempt=self.used,
                index=element.index,
                center_x=element.center_x,
                center_y=element.center_y,
                content=element.content,
                stage=stage,
                reason=reason,
            )
        )

    def is_rejected(self, element: PixelUIElement) -> bool:
        """Check whether an element sits at the same position as a past rejection."""
        return any(_same_position(element, r) for r in self.rejections)

    def history_lines(self) -> list[str]:
        return [_format_rejection(rejection) for rejection in self.rejections]

    def _deadline_exhausted(self) -> bool:
        if self.deadline is None:
            return False
        return (self.deadline - time.monotonic()) < _MIN_REMAINING_SECONDS

    def _exhausted_message(self, description: str) -> str:
        header = f"could not verify '{description}' after {self.used} attempts:"
        lines = self.history_lines()
        if not lines:
            return header
        return "\n".join([header, *lines])


def _same_position(element: PixelUIElement, rejection: RejectedCandidate) -> bool:
    return (
        abs(element.center_x - rejection.center_x) <= _POSITION_TOLERANCE_PX
        and abs(element.center_y - rejection.center_y) <= _POSITION_TOLERANCE_PX
    )


def _format_rejection(rejection: RejectedCandidate) -> str:
    where = (
        f"[{rejection.index}] '{rejection.content}' "
        f"at ({rejection.center_x}, {rejection.center_y})"
    )
    if rejection.stage == "outcome":
        return (
            f"attempt {rejection.attempt}: outcome rejected after acting on {where} "
            f"— {rejection.reason}"
        )
    return (
        f"attempt {rejection.attempt}: candidate {where} rejected — {rejection.reason}"
    )


def _raise_if_cancelled(cancellation_check: CancellationCheck | None) -> None:
    if cancellation_check is not None and cancellation_check():
        raise AgentCancelledError("Agent cancelled during action verification")


def resolve_verified_element(
    project_id: int,
    description: str,
    vision_config: LLMConfig,
    budget: VerificationBudget,
    *,
    on_screenshot: ScreenshotCallback | None = None,
) -> PixelUIElement:
    """Parse the screen once and retry candidate matching until one is verified."""
    budget.raise_if_cancelled()
    parse_result = parse_screen(project_id, on_screenshot=on_screenshot)
    rejected_indices: set[int] = set()

    while True:
        budget.consume(description)
        try:
            element = match_element(
                parse_result,
                description,
                vision_config,
                is_excluded=_build_exclusion_check(rejected_indices, budget),
            )
        except RejectedElementChosenError as exc:
            budget.reject(exc.element, "candidate", _MATCHER_REPEAT_REASON)
            rejected_indices.add(exc.element.index)
            continue
        verdict = verify_candidate(vision_config, parse_result, element, description)
        if verdict.accepted:
            return element
        budget.reject(element, "candidate", verdict.reason)
        rejected_indices.add(element.index)


def _build_exclusion_check(
    rejected_indices: set[int], budget: VerificationBudget
) -> Callable[[PixelUIElement], bool]:
    return lambda el: el.index in rejected_indices or budget.is_rejected(el)


def take_before_screenshot(project_id: int) -> str:
    """Capture the pre-action screenshot without pulling projects.services into tools."""
    return controller_screenshot(project_id)["image_base64"]


def confirm_action(
    project_id: int,
    vision_config: LLMConfig,
    budget: VerificationBudget,
    *,
    acted_on: tuple[PixelUIElement, ...],
    before_image_base64: str,
    action_summary: str,
    expected_result: str,
    tool_name: str,
    on_screenshot: ScreenshotCallback | None = None,
) -> Verdict:
    """Settle, screenshot the result, and verify the action against the expected outcome."""
    time.sleep(POST_ACTION_SETTLE_SECONDS)
    after_image_base64 = controller_screenshot(project_id)["image_base64"]
    if on_screenshot is not None:
        on_screenshot(after_image_base64, tool_name)

    verdict = verify_action_outcome(
        vision_config,
        before_image_base64,
        after_image_base64,
        action_summary,
        expected_result,
    )
    if not verdict.accepted:
        for element in acted_on:
            budget.reject(element, "outcome", verdict.reason)
    return verdict


def format_verified_message(base: str, budget: VerificationBudget) -> str:
    message = f"{base} (verified on attempt {budget.used})"
    if not budget.rejections:
        return message
    return message + "\nSkipped candidates:\n" + "\n".join(budget.history_lines())


def default_click_expectation(description: str) -> str:
    return (
        f"the click landed on '{description}' and the UI reacted to it "
        "(state change, navigation, focus, selection, menu, dialog, or similar)"
    )
