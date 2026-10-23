from collections.abc import Callable, Sequence

from playwright.sync_api import sync_playwright

from controller_client.browser_executor import launch_browser
from controller_client.diagnostics import DiagnosticStep
from controller_client.protocol import BROWSER_DISPLAY_NAMES, BrowserName


def _check_browser(browser: BrowserName) -> str:
    # Headless keeps the check quiet. Bundled Chromium would launch its
    # separate headless shell, so ask for the "chromium" channel instead: it
    # runs the same binary a headed test run uses. Not-installed failures
    # raise BrowserNotInstalledError, whose message carries the install hint.
    channel = "chromium" if browser is BrowserName.CHROMIUM else None
    with sync_playwright() as playwright:
        launched = launch_browser(playwright, browser, headless=True, channel=channel)
        try:
            page = launched.new_page()
            page.goto("about:blank")
            return f"version {launched.version}"
        finally:
            launched.close()


def _browser_check(browser: BrowserName) -> Callable[[], str]:
    return lambda: _check_browser(browser)


def build_browser_steps(
    browsers: Sequence[BrowserName],
) -> tuple[DiagnosticStep, ...]:
    return tuple(
        DiagnosticStep(BROWSER_DISPLAY_NAMES[browser], _browser_check(browser))
        for browser in browsers
    )
