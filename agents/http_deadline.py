"""HTTP requests bounded by a call deadline (``projects.call_deadline``).

httpx timeouts apply per phase and, for reads, per chunk, so a slow
trickling body can outlive any single timeout. Under a call deadline,
``deadline_timeout`` splits the time left over the phases (their sum is the
budget) and ``request_by`` returns or raises by the deadline (the transfer
runs in a daemon thread the caller stops waiting for). Callers use them only when a deadline is set; without one
their own request code stays as it was.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Final

import httpx

# Connect, write and pool phases: each at most this, and at most a quarter
# of the budget, so the four phases never add up to more than the budget.
_FIXED_PHASE_SECONDS: Final = 2.0
# Response headers that describe the transfer of the streamed body, not the
# decoded bytes the rebuilt response carries.
_DEADLINE_PASSED: Final = "The call's deadline passed before the response arrived"
_TRANSFER_HEADERS: Final = frozenset(
    {"content-encoding", "content-length", "transfer-encoding"}
)


def deadline_timeout(budget: float) -> httpx.Timeout:
    """Phases whose sum is ``budget`` seconds."""
    phase = min(_FIXED_PHASE_SECONDS, budget / 4)
    return httpx.Timeout(
        connect=phase, write=phase, pool=phase, read=budget - 3 * phase
    )


def request_by(
    client: httpx.Client, method: str, url: str, *, deadline: float, **kwargs: Any
) -> httpx.Response:
    """Send the request and read its whole body; raise ``httpx.ReadTimeout``
    once ``deadline`` (``time.monotonic()``) passes. Returns a response with
    the decoded body loaded.

    httpcore fixes the read timeout once per body, so a stall after a chunk
    could outlast the deadline. The transfer therefore runs in a daemon
    thread and the caller stops waiting at the deadline; an abandoned
    transfer ends by itself within its own phase timeouts (the caller's
    ``with httpx.Client()`` closing the client usually ends it sooner).
    """
    outcome: list[httpx.Response] = []
    failure: list[BaseException] = []

    def transfer() -> None:
        try:
            outcome.append(_read_all(client, method, url, deadline, kwargs))
        except BaseException as exc:  # handed to the caller below
            failure.append(exc)

    worker = threading.Thread(target=transfer, name="http-deadline", daemon=True)
    worker.start()
    worker.join(max(deadline - time.monotonic(), 0.0))
    if worker.is_alive() or not (outcome or failure):
        raise httpx.ReadTimeout(_DEADLINE_PASSED)
    if failure:
        raise failure[0]
    return outcome[0]


def _read_all(
    client: httpx.Client,
    method: str,
    url: str,
    deadline: float,
    kwargs: dict[str, Any],
) -> httpx.Response:
    with client.stream(method, url, **kwargs) as streamed:
        chunks: list[bytes] = []
        for chunk in streamed.iter_bytes():
            chunks.append(chunk)
            if time.monotonic() > deadline:
                raise httpx.ReadTimeout(_DEADLINE_PASSED, request=streamed.request)
        headers = [
            (name, value)
            for name, value in streamed.headers.multi_items()
            if name.lower() not in _TRANSFER_HEADERS
        ]
        return httpx.Response(
            streamed.status_code,
            headers=headers,
            content=b"".join(chunks),
            request=streamed.request,
        )
