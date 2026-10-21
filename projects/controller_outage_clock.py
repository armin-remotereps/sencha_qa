"""Process-local clock of time spent waiting for the Test Runner to reconnect.

Agent and sub-agent deadlines compare against ``agent_monotonic()`` instead of
``time.monotonic()``, so a reconnect wait does not consume agent time.

The state is module-global: correct only while one test case runs per process on
one thread (Celery prefork, as configured for the ``execution`` queue). Threaded
execution would need the outage total passed explicitly instead.
"""

import time

_outage_seconds: float = 0.0


def reset_outage_clock() -> None:
    global _outage_seconds
    _outage_seconds = 0.0


def record_outage(seconds: float) -> None:
    global _outage_seconds
    _outage_seconds += max(seconds, 0.0)


def outage_seconds() -> float:
    return _outage_seconds


def agent_monotonic() -> float:
    """Monotonic clock that stands still while the server waits for the runner."""
    return time.monotonic() - _outage_seconds
