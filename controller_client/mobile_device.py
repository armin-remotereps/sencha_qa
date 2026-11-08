"""The phone-only interface of a phone or tablet target.

A phone target's backend implements both ``DesktopBackend`` (screenshots,
tap via ``click``, double tap, drag, scroll as a swipe, typing, the size
methods behind ``pointer_mapping``) and ``MobileDevice`` below (gestures and
app lifecycle a desktop has no use for), so the desktop backends never stub
phone actions. Coordinates are input-space points, mapped by the executor.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from appium.webdriver.webdriver import WebDriver

from controller_client.desktop_backend import DesktopBackend
from controller_client.protocol import (
    DeviceInfoPayload,
    DeviceOrientation,
    DeviceStatusPayload,
    MobileButton,
    RunnerTarget,
    ScrollDirection,
)


class MobileDevice(Protocol):
    @property
    def target(self) -> RunnerTarget:
        """``android`` or ``ios``."""
        ...

    def device_info(self) -> DeviceInfoPayload:
        """The device as reported in the handshake; never raises."""
        ...

    def start(self) -> None:
        """Create the driver session (blocking; may take minutes on iOS).

        Raises DeviceDisconnectedError when the device is gone; a failure the
        person must fix is reported through ``status()``."""
        ...

    def stop(self) -> None:
        """Quit the session and release the device; never raises."""
        ...

    def reacquire(self) -> None:
        """Undo ``stop`` (a self-update that did not hand over): the device
        monitor then starts a new session; never raises."""
        ...

    def status(self) -> DeviceStatusPayload:
        """The current device state without using the session; never raises."""
        ...

    def swipe(
        self,
        direction: ScrollDirection,
        x: int | None,
        y: int | None,
        distance: float,
        duration: float,
    ) -> None:
        """Move one finger in ``direction`` from (x, y), or the screen centre."""
        ...

    def long_press(self, x: int, y: int, duration: float) -> None: ...

    def press_button(self, button: MobileButton) -> None:
        """Raise ExecutionError naming an alternative when the platform has
        no such button (iOS has no back button)."""
        ...

    def rotate(self, orientation: DeviceOrientation) -> None: ...

    def hide_keyboard(self) -> bool:
        """Hide the on-screen keyboard; False when none was shown."""
        ...

    def open_url(self, url: str) -> None:
        """Open an already validated URL or deep link on the device."""
        ...

    def install_app(self, path: str) -> str:
        """Install an app from a host path; returns its package or bundle id."""
        ...

    def launch_app(self, app: str) -> str:
        """Bring an app to the front by package/bundle id or visible name;
        returns the resolved id."""
        ...

    def close_app(self, app: str, force: bool) -> str:
        """Terminate an app (never a protected helper or the launcher);
        returns a message for the agent."""
        ...

    def is_app_installed(self, app: str) -> str | None:
        """The installed app's id matching ``app``, or None."""
        ...

    def foreground_app(self) -> str | None:
        """The id of the app in front (package or bundle id), or None."""
        ...

    def installed_apps(self) -> dict[str, str] | None:
        """The installed apps' ids (package or bundle id) mapped to their
        visible names ("" when the platform reports none), or None when the
        phone's driver cannot list apps."""
        ...

    def cleanup(self) -> list[str]:
        """Terminate the apps launched since the last cleanup, hide the
        keyboard and go home; returns one line per thing done."""
        ...

    def web_call[T](self, operation: Callable[[WebDriver], T]) -> T:
        """Run ``operation`` on the phone's Appium session like every phone
        action (action lock, budget, errors translated, a lost session
        recreated). The web browser layer's only way into the session; an
        operation that switches context must switch back to NATIVE_APP
        before it returns."""
        ...


class MobileTargetBackend(DesktopBackend, MobileDevice, Protocol):
    """What ``target.select_mobile_target`` takes: both interfaces at once."""
