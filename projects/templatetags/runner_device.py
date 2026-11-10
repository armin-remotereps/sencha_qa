"""Template helpers for a phone or tablet Test Runner.

docs/plan/feat-mobile-device-runner.md (D4, D6, Pages and forms). Device
strings come from the runner: callers render them with autoescape only.
The label rule lives in ``runners.device_label`` (shared with the runner
list and detail services); the live pages compute the same from the
``runner.status`` socket in ``static/js/punk-hazard.js``
(``punkHazard.runnerDevice`` / ``punkHazard.deviceMachineState``).
"""

from __future__ import annotations

from django import template
from django.conf import settings

from runners.device_label import runner_device_label as _runner_device_label
from runners.models import TestRunner

register = template.Library()


@register.simple_tag
def runner_device_stale_seconds() -> int:
    """``RUNNER_DEVICE_STALE_SECONDS`` for the live pages' staleness check."""
    stale_seconds: int = settings.RUNNER_DEVICE_STALE_SECONDS
    return stale_seconds


@register.filter
def runner_device_label(runner: TestRunner | None) -> str:
    """ "Android phone · Pixel 8 · Android 15 · emulator"; "" on a desktop
    runner or without one."""
    return _runner_device_label(runner)
