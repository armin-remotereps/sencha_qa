from __future__ import annotations

import logging

from playwright.sync_api import sync_playwright

from environments.types import ContainerPorts

logger = logging.getLogger(__name__)


def get_playwright_cdp_url(ports: ContainerPorts) -> str:
    return f"http://localhost:{ports.playwright_cdp}"


def verify_playwright_connection(ports: ContainerPorts) -> bool:
    try:
        cdp_url = get_playwright_cdp_url(ports)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            page = browser.new_page()
            page.goto("about:blank")
            page.close()
            browser.close()
        return True
    except (OSError, TimeoutError) as e:
        logger.debug("Playwright connection failed: %s", e)
        return False
