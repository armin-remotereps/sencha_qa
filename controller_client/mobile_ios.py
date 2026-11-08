"""iOS and iPadOS specifics for ``MobileBackend`` (Appium XCUITest).

Screenshots are pixels and taps are points (2x or 3x), so ``input_size`` is
the window size in points and the executor's ``pointer_mapping`` converts.
Real devices need WebDriverAgent signed with the person's Apple team, or a
preinstalled WDA; simulators need neither.
"""

from __future__ import annotations

import logging
import plistlib
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Final

from appium.webdriver.webdriver import WebDriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.keys import Keys

from controller_client.exceptions import ExecutionError
from controller_client.mobile_apps import (
    is_unsupported_command,
    looks_like_app_id,
    match_app_name,
    pick_single,
)
from controller_client.mobile_discovery import DiscoveredDevice
from controller_client.mobile_gestures import send_keys
from controller_client.mobile_platform import SessionPorts
from controller_client.mobile_settings import MobileSettings
from controller_client.protocol import DeviceKind, MobileButton, RunnerTarget

logger = logging.getLogger(__name__)

_PRESS_BUTTON_NAMES: Final[dict[MobileButton, str]] = {
    MobileButton.HOME: "home",
    MobileButton.VOLUME_UP: "volumeup",
    MobileButton.VOLUME_DOWN: "volumedown",
}
_MISSING_BUTTONS: Final[dict[MobileButton, str]] = {
    MobileButton.BACK: (
        "iOS has no back button: tap the app's Back button (usually top left) "
        "or swipe right starting at the left edge of the screen."
    ),
    MobileButton.APP_SWITCH: (
        "iOS has no app switcher button: press home and use launch_app to "
        "bring the app to the front."
    ),
    MobileButton.POWER: (
        "The side button cannot be pressed through the Test Runner (it would "
        "lock the device); use the home button or close_app instead."
    ),
}
_KEY_VALUES: Final[dict[str, str]] = {
    "enter": Keys.ENTER,
    "backspace": Keys.BACKSPACE,
    "delete": Keys.DELETE,
    "tab": Keys.TAB,
    "space": " ",
}
_APP_TYPES: Final[tuple[str, ...]] = ("User", "System")
_INSTALL_SUFFIXES: Final[frozenset[str]] = frozenset({".app", ".ipa"})
_INFO_PLIST: Final[str] = "Info.plist"
_MAX_PLIST_BYTES: Final[int] = 4 * 1024 * 1024
_WDA_LAUNCH_TIMEOUT_MS: Final[int] = 600_000
_WDA_CONNECTION_TIMEOUT_MS: Final[int] = 600_000
# The browsers the web browser layer drives (WEB_BROWSER_V1). Safari's web
# views come with ``includeSafariInWebviews``; the others are WKWebView apps
# whose pages are listed only through ``additionalWebviewBundleIds`` (and
# only when the app makes them inspectable).
IOS_SAFARI_BUNDLE_ID: Final[str] = "com.apple.mobilesafari"
IOS_CHROME_BUNDLE_ID: Final[str] = "com.google.chrome.ios"
IOS_EDGE_BUNDLE_ID: Final[str] = "com.microsoft.msedge"
IOS_FIREFOX_BUNDLE_ID: Final[str] = "org.mozilla.ios.Firefox"
_WEBVIEW_BUNDLE_IDS: Final[tuple[str, ...]] = (
    IOS_CHROME_BUNDLE_ID,
    IOS_EDGE_BUNDLE_ID,
    IOS_FIREFOX_BUNDLE_ID,
)
# Short, so a context switch to a page that does not answer fails within an
# action's budget instead of the driver's default wait.
_WEBVIEW_CONNECT_TIMEOUT_MS: Final[int] = 5_000
# Used when the driver cannot list installed apps.
_WELL_KNOWN_APPS: Final[dict[str, str]] = {
    IOS_SAFARI_BUNDLE_ID: "Safari",
    "com.apple.Preferences": "Settings",
    "com.apple.mobileslideshow": "Photos",
    "com.apple.camera": "Camera",
    "com.apple.MobileSMS": "Messages",
    "com.apple.mobilemail": "Mail",
    "com.apple.Maps": "Maps",
    "com.apple.AppStore": "App Store",
    "com.apple.DocumentsApp": "Files",
    "com.apple.mobilecal": "Calendar",
    "com.apple.mobilenotes": "Notes",
    "com.apple.MobileAddressBook": "Contacts",
    "com.apple.mobiletimer": "Clock",
    "com.apple.mobilephone": "Phone",
}


class IosPlatform:
    def __init__(self, settings: MobileSettings) -> None:
        self._settings = settings

    @property
    def target(self) -> RunnerTarget:
        return RunnerTarget.IOS

    def platform_name(self, device: DiscoveredDevice) -> str:
        label = f"{device.model} {device.name}".lower()
        return "iPadOS" if "ipad" in label else "iOS"

    def capabilities(
        self, device: DiscoveredDevice, ports: SessionPorts
    ) -> Mapping[str, object]:
        caps: dict[str, object] = {
            "wdaLocalPort": ports.driver_port,
            "mjpegServerPort": ports.mjpeg_server_port,
            "wdaLaunchTimeout": _WDA_LAUNCH_TIMEOUT_MS,
            "wdaConnectionTimeout": _WDA_CONNECTION_TIMEOUT_MS,
            # Web view contexts for the web browser layer: listing them costs
            # nothing until a context is asked for, and the native context
            # stays the default.
            "includeSafariInWebviews": True,
            "additionalWebviewBundleIds": list(_WEBVIEW_BUNDLE_IDS),
            "enableWebviewDetailsCollection": True,
            "webviewConnectTimeout": _WEBVIEW_CONNECT_TIMEOUT_MS,
        }
        if device.kind is DeviceKind.PHYSICAL:
            caps.update(self._signing_capabilities())
        return caps

    def _signing_capabilities(self) -> dict[str, object]:
        settings = self._settings
        if settings.ios_preinstalled_wda:
            caps: dict[str, object] = {"usePreinstalledWDA": True}
            if settings.ios_wda_bundle_id:
                caps["updatedWDABundleId"] = settings.ios_wda_bundle_id
            return caps
        caps = {}
        if settings.ios_team_id:
            caps["xcodeOrgId"] = settings.ios_team_id
            caps["xcodeSigningId"] = settings.ios_signing_id or "Apple Development"
        if settings.ios_wda_bundle_id:
            caps["updatedWDABundleId"] = settings.ios_wda_bundle_id
        return caps

    def input_size(
        self, driver: WebDriver, capture: tuple[int, int]
    ) -> tuple[int, int]:
        # The backend reuses the answer while the screenshot size is unchanged.
        size = driver.get_window_size()
        width, height = size.get("width"), size.get("height")
        if isinstance(width, (int, float)) and isinstance(height, (int, float)):
            if width > 0 and height > 0:
                return round(width), round(height)
        return capture

    def press_button(self, driver: WebDriver, button: MobileButton) -> None:
        refusal = _MISSING_BUTTONS.get(button)
        if refusal is not None:
            raise ExecutionError(refusal)
        try:
            driver.execute_script(
                "mobile: pressButton", {"name": _PRESS_BUTTON_NAMES[button]}
            )
        except WebDriverException as exc:
            if button is not MobileButton.HOME and is_unsupported_command(exc):
                raise ExecutionError(
                    "The volume buttons can only be pressed on a real iPhone or "
                    "iPad, not on a simulator."
                ) from exc
            raise

    def supported_keys(self) -> frozenset[str]:
        return frozenset(_KEY_VALUES)

    def press_key(self, driver: WebDriver, key: str) -> None:
        value = _KEY_VALUES.get(key)
        if value is None:
            raise ExecutionError(f"The key '{key}' cannot be pressed on iOS")
        send_keys(driver, [value], 0.0)

    def type_text(self, driver: WebDriver, text: str, interval: float) -> None:
        # W3C key actions type into the focused field at the cursor.
        send_keys(driver, list(text), interval)

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
                f"This device cannot list installed apps, so '{name}' cannot be "
                "resolved: name the app by its bundle id (e.g. com.example.app)."
            )
        return pick_single(name, match_app_name(name, {**_WELL_KNOWN_APPS, **apps}))

    def find_installed_app(self, driver: WebDriver, app: str) -> str | None:
        try:
            return self.resolve_app(driver, app)
        except ExecutionError:
            return None

    def open_url(self, driver: WebDriver, url: str) -> None:
        try:
            driver.execute_script("mobile: deepLink", {"url": url})
        except WebDriverException as exc:
            if not is_unsupported_command(exc):
                raise
            # Older drivers: in the native context ``get`` opens the URL
            # through Safari.
            driver.get(url)

    def install_app(self, driver: WebDriver, path: str) -> str:
        source = Path(path)
        if source.suffix.lower() not in _INSTALL_SUFFIXES or not source.exists():
            raise ExecutionError(
                f"'{path}' is not an .app (simulator) or .ipa (device) on the "
                "Test Runner"
            )
        bundle_id = _bundle_id(source)
        driver.install_app(str(source))
        return bundle_id

    def current_app(self, driver: WebDriver) -> str | None:
        info = driver.execute_script("mobile: activeAppInfo")
        bundle_id = info.get("bundleId") if isinstance(info, Mapping) else None
        return bundle_id if isinstance(bundle_id, str) and bundle_id else None

    def installed_apps(self, driver: WebDriver) -> dict[str, str] | None:
        """Installed bundle ids with their visible names, or None."""
        apps: dict[str, str] = {}
        for app_type in _APP_TYPES:
            try:
                raw = driver.execute_script(
                    "mobile: listApps", {"applicationType": app_type}
                )
            except WebDriverException as exc:
                if is_unsupported_command(exc):
                    logger.info("The XCUITest driver cannot list apps: %s", exc.msg)
                    return None
                raise
            if isinstance(raw, Mapping):
                for bundle_id, info in raw.items():
                    apps[str(bundle_id)] = _display_name(info)
        return apps


def _display_name(info: object) -> str:
    if not isinstance(info, Mapping):
        return ""
    for key in ("CFBundleDisplayName", "CFBundleName"):
        value = info.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _bundle_id(source: Path) -> str:
    """CFBundleIdentifier of an .app folder or an .ipa; "" when unreadable."""
    try:
        if source.is_dir():
            plist = source / _INFO_PLIST
            if plist.stat().st_size > _MAX_PLIST_BYTES:
                return ""
            data = plistlib.loads(plist.read_bytes())
        else:
            data = _ipa_info(source)
    except (OSError, ValueError, zipfile.BadZipFile, plistlib.InvalidFileException):
        return ""
    value = data.get("CFBundleIdentifier") if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def _ipa_info(source: Path) -> object:
    # Payload/<Name>.app/Info.plist; read in memory, nothing extracted.
    with zipfile.ZipFile(source) as archive:
        for entry in archive.infolist():
            parts = PurePosixPath(entry.filename).parts
            if (
                len(parts) == 3
                and parts[0] == "Payload"
                and parts[1].endswith(".app")
                and parts[2] == _INFO_PLIST
                and entry.file_size <= _MAX_PLIST_BYTES
            ):
                return plistlib.loads(archive.read(entry))
    return None
