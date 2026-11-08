"""What ``MobileBackend`` asks of a platform (``mobile_android`` /
``mobile_ios``): capabilities, sizes, buttons, keys and app handling.

Every method that takes a driver runs inside ``MobileSession.call`` (under
the session lock, with WebDriver errors translated), so implementations may
let selenium's exceptions through and raise ExecutionError for refusals.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from appium.webdriver.webdriver import WebDriver

from controller_client.mobile_discovery import DiscoveredDevice
from controller_client.protocol import MobileButton, RunnerTarget


@dataclass(frozen=True)
class SessionPorts:
    """Free local ports per session, so two runners on one host never clash."""

    # UiAutomator2 server port (Android) / WebDriverAgent port (iOS).
    driver_port: int
    mjpeg_server_port: int


class MobilePlatform(Protocol):
    @property
    def target(self) -> RunnerTarget: ...

    def platform_name(self, device: DiscoveredDevice) -> str:
        """``Android``, ``iOS`` or ``iPadOS``."""
        ...

    def capabilities(
        self, device: DiscoveredDevice, ports: SessionPorts
    ) -> Mapping[str, object]:
        """Platform-specific Appium capabilities (no ``udid``/``noReset``)."""
        ...

    def input_size(
        self, driver: WebDriver, capture: tuple[int, int]
    ) -> tuple[int, int]:
        """The coordinate space taps take, given the latest screenshot size."""
        ...

    def press_button(self, driver: WebDriver, button: MobileButton) -> None: ...

    def supported_keys(self) -> frozenset[str]:
        """Canonical key names ``press_key`` takes."""
        ...

    def press_key(self, driver: WebDriver, key: str) -> None: ...

    def type_text(self, driver: WebDriver, text: str, interval: float) -> None: ...

    def resolve_app(self, driver: WebDriver, app: str) -> str:
        """The installed app's id for an id or a visible name; raises
        ExecutionError when there is none or the name is ambiguous."""
        ...

    def find_installed_app(self, driver: WebDriver, app: str) -> str | None:
        """Like ``resolve_app``, None instead of an error."""
        ...

    def open_url(self, driver: WebDriver, url: str) -> None: ...

    def install_app(self, driver: WebDriver, path: str) -> str:
        """Install from a host path; the app's id, or "" when unknown."""
        ...

    def current_app(self, driver: WebDriver) -> str | None:
        """The id of the app in front, or None when unknown."""
        ...

    def installed_apps(self, driver: WebDriver) -> dict[str, str] | None:
        """Installed app ids mapped to their visible names ("" when the
        platform reports none), or None when the driver cannot list apps."""
        ...
