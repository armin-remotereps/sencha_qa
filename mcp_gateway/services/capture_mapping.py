"""Response forms shared by ``list_windows`` and ``capture_screenshot``."""

from __future__ import annotations

from mcp_gateway.schemas import WindowDesktopItem
from mcp_gateway.untrusted_text import case_line
from projects.controller_capture_actions import WindowDesktop


def desktop_item(desktop: WindowDesktop) -> WindowDesktopItem:
    """The response form of a window's desktop (name neutralised)."""
    name = case_line(desktop.name) if desktop.name else ""
    return WindowDesktopItem(
        current=desktop.current, index=desktop.index, name=name or None
    )
