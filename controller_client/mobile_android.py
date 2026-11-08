"""Android specifics for ``MobileBackend`` (Appium UiAutomator2).

Taps and screenshots both use device pixels. App names resolve through the
driver's ``mobile: listApps`` (no ``adb shell``: Appium runs without relaxed
security); when the driver cannot list apps, a package id is required.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from appium.webdriver.webdriver import WebDriver
from selenium.common.exceptions import WebDriverException

from controller_client.exceptions import ExecutionError
from controller_client.mobile_apps import (
    is_unsupported_command,
    looks_like_app_id,
    match_app_name,
    pick_single,
)
from controller_client.mobile_discovery import DiscoveredDevice
from controller_client.mobile_platform import SessionPorts
from controller_client.protocol import MobileButton, RunnerTarget

logger = logging.getLogger(__name__)

# android.view.KeyEvent codes.
_BUTTON_KEYCODES: Final[dict[MobileButton, int]] = {
    MobileButton.BACK: 4,
    MobileButton.HOME: 3,
    MobileButton.APP_SWITCH: 187,
    MobileButton.VOLUME_UP: 24,
    MobileButton.VOLUME_DOWN: 25,
    MobileButton.POWER: 26,
}
_KEY_KEYCODES: Final[dict[str, int]] = {
    "enter": 66,
    "backspace": 67,
    "delete": 112,
    "tab": 61,
    "space": 62,
    "escape": 111,
}
_APK_SUFFIXES: Final[frozenset[str]] = frozenset({".apk"})
_VIEW_ACTION: Final[str] = "android.intent.action.VIEW"
_SERVER_LAUNCH_TIMEOUT_MS: Final[int] = 120_000
_ADB_EXEC_TIMEOUT_MS: Final[int] = 60_000
# Under the runner's install HTTP timeout (mobile_session, 240 s).
_INSTALL_TIMEOUT_MS: Final[int] = 230_000
# Used when the driver cannot list installed apps.
_WELL_KNOWN_APPS: Final[dict[str, str]] = {
    "com.android.chrome": "Chrome",
    "com.android.settings": "Settings",
    "com.google.android.youtube": "YouTube",
    "com.google.android.gm": "Gmail",
    "com.google.android.apps.maps": "Maps",
    "com.android.vending": "Play Store",
}


class AndroidPlatform:
    @property
    def target(self) -> RunnerTarget:
        return RunnerTarget.ANDROID

    def platform_name(self, device: DiscoveredDevice) -> str:
        return "Android"

    def capabilities(
        self, device: DiscoveredDevice, ports: SessionPorts
    ) -> Mapping[str, object]:
        return {
            "systemPort": ports.driver_port,
            "mjpegServerPort": ports.mjpeg_server_port,
            "uiautomator2ServerLaunchTimeout": _SERVER_LAUNCH_TIMEOUT_MS,
            "adbExecTimeout": _ADB_EXEC_TIMEOUT_MS,
        }

    def input_size(
        self, driver: WebDriver, capture: tuple[int, int]
    ) -> tuple[int, int]:
        # UiAutomator2 taps take the same device pixels as the screenshot.
        return capture

    def press_button(self, driver: WebDriver, button: MobileButton) -> None:
        driver.press_keycode(_BUTTON_KEYCODES[button])

    def supported_keys(self) -> frozenset[str]:
        return frozenset(_KEY_KEYCODES)

    def press_key(self, driver: WebDriver, key: str) -> None:
        keycode = _KEY_KEYCODES.get(key)
        if keycode is None:
            raise ExecutionError(f"The key '{key}' cannot be pressed on Android")
        driver.press_keycode(keycode)

    def type_text(self, driver: WebDriver, text: str, interval: float) -> None:
        # Types Unicode into the focused field at the cursor; ``interval`` is
        # not supported by the driver.
        driver.execute_script("mobile: type", {"text": text})

    def resolve_app(self, driver: WebDriver, app: str) -> str:
        name = app.strip()
        if looks_like_app_id(name):
            if driver.is_app_installed(name):
                return name
            raise ExecutionError(f"The app '{name}' is not installed on the device")
        apps = self.installed_apps(driver)
        if apps is None:
            matches = match_app_name(name, _WELL_KNOWN_APPS)
            if len(matches) == 1 and driver.is_app_installed(matches[0]):
                return matches[0]
            raise ExecutionError(
                f"This device's Appium driver cannot list installed apps, so "
                f"'{name}' cannot be resolved: name the app by its package id "
                "(e.g. com.example.app)."
            )
        return pick_single(name, match_app_name(name, apps))

    def find_installed_app(self, driver: WebDriver, app: str) -> str | None:
        try:
            return self.resolve_app(driver, app)
        except ExecutionError:
            return None

    def open_url(self, driver: WebDriver, url: str) -> None:
        # ``am start -a VIEW -d <url>``: the default handler (browser or the
        # app owning the deep link) opens it.
        driver.execute_script(
            "mobile: startActivity", {"action": _VIEW_ACTION, "uri": url}
        )

    def install_app(self, driver: WebDriver, path: str) -> str:
        source = Path(path)
        if source.suffix.lower() not in _APK_SUFFIXES or not source.is_file():
            raise ExecutionError(f"'{path}' is not an .apk file on the Test Runner")
        before = self.installed_apps(driver)
        driver.install_app(str(source), replace=True, timeout=_INSTALL_TIMEOUT_MS)
        after = self.installed_apps(driver)
        if before is None or after is None:
            return ""
        added = sorted(set(after) - set(before))
        # An update of an installed app adds nothing: its id stays unknown.
        return added[0] if len(added) == 1 else ""

    def current_app(self, driver: WebDriver) -> str | None:
        package = driver.current_package
        return package if isinstance(package, str) and package else None

    def installed_apps(self, driver: WebDriver) -> dict[str, str] | None:
        """Installed package ids (no visible names), or None when the driver
        has no ``mobile: listApps``."""
        try:
            raw = driver.execute_script("mobile: listApps", {})
        except WebDriverException as exc:
            if is_unsupported_command(exc):
                logger.info("The UiAutomator2 driver cannot list apps: %s", exc.msg)
                return None
            raise
        if isinstance(raw, Mapping):
            return {str(package): "" for package in raw}
        if isinstance(raw, list):
            return {str(package): "" for package in raw if isinstance(package, str)}
        return None
