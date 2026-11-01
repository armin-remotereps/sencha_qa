"""Push a stored Manual run screenshot to its case page.

The same ``test_run_case.screenshot`` event the worker sends for an agent
screenshot (``services._broadcast_screenshot``), so the case page gallery
needs no manual branch. A leaf (models only); call it after commit.
"""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.utils import dateformat

from projects.models import TestRunScreenshot

logger = logging.getLogger(__name__)


def broadcast_manual_screenshot(pivot_id: int, screenshot: TestRunScreenshot) -> None:
    """Send the case page the new screenshot; never raises."""
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            f"test_run_case_{pivot_id}",
            {
                "type": "test_run_case.screenshot",
                "screenshot_id": screenshot.pk,
                "image_url": screenshot.image.url,
                "tool_name": screenshot.tool_name,
                "created_at": dateformat.format(screenshot.created_at, "M d, H:i"),
            },
        )
    except Exception:
        logger.exception("Manual screenshot broadcast failed for pivot %s", pivot_id)
