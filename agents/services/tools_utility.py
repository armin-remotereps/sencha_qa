from __future__ import annotations

import math
import time

from agents.types import AgentCancelledError, CancellationCheck, ToolResult

MAX_WAIT_SECONDS = 300.0
BUDGET_MARGIN_SECONDS = 15.0
_SLEEP_CHUNK_SECONDS = 0.25


def wait(
    seconds: object,
    *,
    cancellation_check: CancellationCheck | None = None,
    deadline: float | None = None,
) -> ToolResult:
    duration = _validate_duration(seconds)
    if duration is None:
        return _invalid_duration_result(seconds)

    remaining = _remaining_seconds_after_margin(deadline)
    if remaining is not None and remaining < 1:
        return _budget_exhausted_result()

    if remaining is not None and duration > remaining:
        _sleep_until_deadline(time.monotonic() + remaining, cancellation_check)
        return _shortened_wait_result(requested=duration, actual=float(remaining))

    _sleep_until_deadline(time.monotonic() + duration, cancellation_check)
    return ToolResult(
        tool_call_id="",
        content=f"Waited {_format_duration(duration)} seconds.",
        is_error=False,
    )


def _validate_duration(seconds: object) -> float | None:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    duration = float(seconds)
    if math.isnan(duration) or math.isinf(duration):
        return None
    if duration <= 0 or duration > MAX_WAIT_SECONDS:
        return None
    return duration


def _remaining_seconds_after_margin(deadline: float | None) -> int | None:
    if deadline is None:
        return None
    return math.floor(deadline - time.monotonic() - BUDGET_MARGIN_SECONDS)


def _invalid_duration_result(seconds: object) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        content=(
            f"Invalid seconds value {seconds!r}: must be a number greater than 0 "
            f"and at most {_format_duration(MAX_WAIT_SECONDS)}."
        ),
        is_error=True,
    )


def _budget_exhausted_result() -> ToolResult:
    return ToolResult(
        tool_call_id="",
        content=(
            "Did not wait: this step's time budget is exhausted. "
            "Inspect the current state and report the result now."
        ),
        is_error=True,
    )


def _shortened_wait_result(*, requested: float, actual: float) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        content=(
            f"Waited {_format_duration(actual)} of the {_format_duration(requested)} "
            "seconds requested: this step's time budget is nearly exhausted. "
            "Inspect the current state and report the result now."
        ),
        is_error=False,
    )


def _sleep_until_deadline(
    deadline: float, cancellation_check: CancellationCheck | None
) -> None:
    while True:
        _raise_if_cancelled(cancellation_check)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(_SLEEP_CHUNK_SECONDS, remaining))


def _raise_if_cancelled(cancellation_check: CancellationCheck | None) -> None:
    if cancellation_check is not None and cancellation_check():
        raise AgentCancelledError("Agent cancelled during wait")


def _format_duration(duration: float) -> str:
    if duration.is_integer():
        return str(int(duration))
    return f"{duration:g}"
