"""``diagnose --web-browsers``: check the web browser layer on this desktop.

For each browser this desktop serves (``web_browsers_for``) it opens the
browser with its test profile, lists the browsers, reads the page URL and
closes it again, through the same hub the runner uses. A phone target is
checked by opening the browser app on the phone while the runner runs.
"""

from __future__ import annotations

import asyncio
import platform
from collections.abc import Callable
from typing import Final

from controller_client.config import web_driver_download_enabled
from controller_client.diagnostics import DiagnosticStep
from controller_client.exceptions import WebBrowserSetupNeededError
from controller_client.protocol import (
    WEB_BROWSER_DISPLAY_NAMES,
    MessageType,
    RunnerTarget,
    WebBrowser,
    WebBrowserSetupStep,
    ordered_web_browsers,
    web_browsers_for,
)
from controller_client.update_layout import RUNNING_PACKAGE_DIR
from controller_client.web_browser_backend import WebBrowserContext
from controller_client.web_browser_hub import WebBrowserHub

SETUP_HINTS: Final[dict[WebBrowserSetupStep, str]] = {
    WebBrowserSetupStep.SAFARI_REMOTE_AUTOMATION_OFF: (
        "Run `safaridriver --enable` once (asks for an administrator password) "
        "and tick Develop > Allow Remote Automation in Safari."
    ),
    WebBrowserSetupStep.IOS_WEB_INSPECTOR_OFF: (
        "On the iPhone or iPad: Settings > Apps > Safari > Advanced > Web Inspector on."
    ),
    WebBrowserSetupStep.CHROME_IOS_WEB_INSPECTOR_OFF: (
        "In Chrome on the iPhone or iPad: Settings > Content Settings > Web "
        "Inspector on, then restart Chrome."
    ),
    WebBrowserSetupStep.DRIVER_UNAVAILABLE: (
        "The browser driver could not be found or downloaded. Allow downloads "
        "(CONTROLLER_WEB_DRIVER_DOWNLOAD=true) with internet access, or put "
        "chromedriver / msedgedriver / geckodriver on PATH."
    ),
    WebBrowserSetupStep.ANDROID_DEVTOOLS_UNAVAILABLE: (
        "The browser app exposes no debugging socket. Update the app, keep it "
        "open in the foreground and check USB debugging."
    ),
    WebBrowserSetupStep.FIREFOX_ANDROID_REMOTE_DEBUGGING_OFF: (
        "In Firefox on the phone: Settings > Remote debugging via USB on."
    ),
}


def setup_hint(step: str) -> str:
    try:
        return SETUP_HINTS[WebBrowserSetupStep(step)]
    except ValueError:
        return "The browser needs a one-time setup on this machine."


def _round_trip(browser: WebBrowser) -> str:
    """Open (or attach), list, read the URL and close one browser."""
    hub = WebBrowserHub(
        WebBrowserContext(
            target=RunnerTarget.DESKTOP,
            host_os=platform.system(),
            runner_root=RUNNING_PACKAGE_DIR,
            driver_download=web_driver_download_enabled(),
            mobile=None,
            device_serial=None,
        )
    )

    async def run() -> str:
        try:
            opened = await hub.handle(
                MessageType.WEB_BROWSER_OPEN,
                {"browser": browser.value, "url": "about:blank"},
            )
            url = await hub.handle(
                MessageType.WEB_BROWSER_READ, {"browser": browser.value, "what": "url"}
            )
            await hub.handle(MessageType.WEB_BROWSER_CLOSE, {"browser": browser.value})
            return f"{opened.content.strip()} (url {url.content.strip()})"
        except WebBrowserSetupNeededError as e:
            raise RuntimeError(setup_hint(e.step)) from e
        finally:
            await hub.shutdown()

    return asyncio.run(run())


def _step(browser: WebBrowser) -> Callable[[], str]:
    return lambda: _round_trip(browser)


def build_web_browser_steps() -> tuple[DiagnosticStep, ...]:
    """One step per browser this desktop serves (empty on an unknown OS)."""
    browsers = ordered_web_browsers(
        web_browsers_for(RunnerTarget.DESKTOP, platform.system())
    )
    return tuple(
        DiagnosticStep(
            f"{WEB_BROWSER_DISPLAY_NAMES[browser]} (test browser)", _step(browser)
        )
        for browser in browsers
    )
