"""Check that a Test Runner can drive the browser a case asked for.

Chromium works on every runner. Any other browser needs a runner that
advertises ``BROWSER_SELECTION_V1``; an older one ignores the ``browser`` field
and would silently run the case in Chromium instead.
"""

from __future__ import annotations

from controller_client.protocol import (
    BROWSER_DISPLAY_NAMES,
    BrowserName,
    ClientCapability,
)
from projects.exceptions import BrowserUnavailableError
from runners.models import TestRunner
from runners.types import RunnerId


def ensure_runner_supports_browser(runner_id: RunnerId, browser: BrowserName) -> None:
    """Raise ``BrowserUnavailableError`` when the runner cannot drive ``browser``."""
    if browser == BrowserName.CHROMIUM:
        return
    capabilities = (
        TestRunner.objects.filter(pk=runner_id)
        .values_list("capabilities", flat=True)
        .first()
    )
    if ClientCapability.BROWSER_SELECTION_V1.value in (capabilities or []):
        return
    # No runner-reported version in the text: it reaches the UI and TestRail.
    raise BrowserUnavailableError(
        "This Test Runner is too old to drive anything but Chromium; this case "
        f"needs {BROWSER_DISPLAY_NAMES[browser]}. Install a fresh Test Runner from "
        "the Test Runners page."
    )
