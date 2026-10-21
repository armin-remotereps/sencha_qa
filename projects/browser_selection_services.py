"""Check that the connected Test Runner can drive the browser a case asked for.

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
from projects.models import Project


def ensure_runner_supports_browser(project_id: int, browser: BrowserName) -> None:
    """Raise ``BrowserUnavailableError`` when the runner cannot drive ``browser``."""
    if browser == BrowserName.CHROMIUM:
        return
    capabilities = (
        Project.objects.filter(pk=project_id)
        .values_list("agent_capabilities", flat=True)
        .get()
    )
    if ClientCapability.BROWSER_SELECTION_V1.value in (capabilities or []):
        return
    # No runner-reported version in the text: it reaches the UI and TestRail.
    raise BrowserUnavailableError(
        "This Test Runner is too old to drive anything but Chromium; this case "
        f"needs {BROWSER_DISPLAY_NAMES[browser]}. Download a fresh Test Runner from "
        "the Environment page."
    )
