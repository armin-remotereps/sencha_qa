from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final

from agents.types import AgentCancelledError
from projects.controller_outage_clock import agent_monotonic
from projects.exceptions import VisualEngineError
from visual_engine.services.pool_status import get_visual_engine_status
from visual_engine.types import VisualEngineState

POLL_INTERVAL_SECONDS: Final[float] = 2.0

_FAILED_MESSAGE: Final[str] = (
    "Every connected vision worker failed to load its model; check the worker's "
    "log or run `python -m vision_worker.diagnose`"
)


def wait_for_ready_worker(
    *,
    timeout: float,
    cancellation_check: Callable[[], bool] | None = None,
) -> None:
    """Block until at least one vision worker is ready to parse.

    Raises ``AgentCancelledError`` when the run is cancelled meanwhile, and
    ``VisualEngineError`` when every connected worker failed to load or none
    became ready within ``timeout`` seconds.
    """
    state = poll_for_ready_worker(
        timeout=timeout, cancellation_check=cancellation_check
    )
    if state is not VisualEngineState.READY:
        raise VisualEngineError(
            f"No vision worker became ready within {timeout:.0f}s "
            f"(state: {state.value})"
        )


def poll_for_ready_worker(
    *,
    timeout: float,
    cancellation_check: Callable[[], bool] | None = None,
) -> VisualEngineState:
    """Poll the pool until a worker is ready or ``timeout`` runs out.

    Returns READY, or the last state seen when the time ran out. A pool whose
    connected workers all failed raises ``VisualEngineError`` at once: a failed
    model load does not recover without restarting the worker.
    """
    start = agent_monotonic()
    while True:
        if cancellation_check is not None and cancellation_check():
            raise AgentCancelledError(
                "Test run cancelled while waiting for a vision worker"
            )
        state = get_visual_engine_status().state
        if state is VisualEngineState.READY:
            return state
        if state is VisualEngineState.FAILED:
            raise VisualEngineError(_FAILED_MESSAGE)
        remaining = timeout - (agent_monotonic() - start)
        if remaining <= 0:
            return state
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
