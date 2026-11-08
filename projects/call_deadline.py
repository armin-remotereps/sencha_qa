"""A scoped deadline for one bounded call (an MCP machine tool call).

``call_deadline(seconds)`` sets a monotonic deadline for the current context
(thread or task); ``capped_timeout(timeout)`` is what every blocking wait
inside it uses: ``min(timeout, remaining)``. The waits that read it are the
controller reply wait (``projects.services._send_controller_action_once``),
the AI request (``agents.services.llm_client.send_chat_completion``) and the
visual engine parse (``visual_engine.services.parse_screenshot``), so a call
never outlives the machine lock and round-trip slot it holds.

Unset (the agent's Celery paths) nothing changes: ``capped_timeout`` returns
the timeout it is given. A leaf module (stdlib and ``agents.exceptions``
only), so ``agents`` and ``visual_engine`` may import it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

from agents.exceptions import InfrastructureError

# Least time a capped wait may start with: below it, an action would be
# sent and then abandoned at once, so it is not sent at all.
MIN_SEND_SECONDS: Final = 2.0

_deadline: ContextVar[float | None] = ContextVar("call_deadline", default=None)


class CallDeadlineExceededError(InfrastructureError):
    """Too little of the call's deadline was left for a wait to start;
    nothing was sent."""


@contextmanager
def call_deadline(seconds: float) -> Iterator[None]:
    """Bound every capped wait inside the block to ``seconds`` from now (or
    to an enclosing deadline, whichever is earlier)."""
    deadline = time.monotonic() + seconds
    enclosing = _deadline.get()
    if enclosing is not None:
        deadline = min(deadline, enclosing)
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_seconds() -> float | None:
    """Seconds left before the deadline (may be ≤ 0), or None when unset."""
    deadline = _deadline.get()
    if deadline is None:
        return None
    return deadline - time.monotonic()


def capped_timeout(timeout: float) -> float:
    """``timeout``, cut to the time left before the deadline when one is set.

    Raises ``CallDeadlineExceededError`` when less than ``MIN_SEND_SECONDS``
    is left, so the caller starts nothing it could not wait for.
    """
    remaining = remaining_seconds()
    if remaining is None:
        return timeout
    if remaining < MIN_SEND_SECONDS:
        raise CallDeadlineExceededError("The call's time budget ran out")
    return min(timeout, remaining)
