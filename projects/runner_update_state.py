"""How long a Test Runner self-update may stay in flight.

A leaf module (imports only settings) so ``projects.runner_use`` and the
update services share the timeout without import cycles. It must never import ``projects.services``.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings


def runner_update_timeout() -> timedelta:
    """How long an update may stay in flight before it counts as expired."""
    return timedelta(seconds=settings.RUNNER_UPDATE_TIMEOUT_SECONDS)
