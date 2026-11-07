"""Whether a Linux session can show and capture windows (every Linux path).

In order:

1. no ``DISPLAY`` / ``WAYLAND_DISPLAY`` (``CapturePlatform.NO_DISPLAY``), or
   a session whose capture path needs the session bus (Wayland, and GNOME on
   Xorg, which captures through the GNOME Shell helper) without one:
   ``not_interactive``;
2. KDE Plasma on Wayland or an unknown Wayland compositor:
   ``compositor_unsupported`` (checked before the lock, because it is
   permanent and needs no D-Bus);
3. a locked screen: logind ``Session.LockedHint`` on the system bus (the
   session from ``XDG_SESSION_ID``, else ``GetSessionByPID``), falling back to
   ``org.freedesktop.ScreenSaver.GetActive`` and then
   ``org.gnome.ScreenSaver.GetActive`` on the session bus: ``not_interactive``.
   When none of them answers the screen counts as unlocked; the blank check
   after the grab still catches a black frame.

``problem`` never raises and stays well under a second: every D-Bus step has
a short timeout and the whole lock check a fixed budget. The D-Bus work runs
on a private asyncio loop in the calling (worker) thread, or in a short-lived
thread when the caller already runs a loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from collections.abc import Mapping
from typing import Final

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from controller_client.capture_platform import (
    COMPOSITOR_UNSUPPORTED_DETAIL,
    NO_DISPLAY_DETAIL,
    UNSUPPORTED_OS_DETAIL,
    CapturePlatform,
    CaptureSessionBackend,
    detect_capture_platform,
)
from controller_client.protocol import CaptureStatus
from controller_client.session_bus import session_bus_address

logger = logging.getLogger(__name__)

_LOCKED_DETAIL: Final = "the screen of the test machine is locked; unlock it"

# One D-Bus step (connect or call).
_STEP_TIMEOUT_S: Final = 0.3
# Loop turns given to dbus_fast to close a disconnected socket.
_CLOSE_TURNS: Final = 3
# The whole lock check.
_LOCK_BUDGET_S: Final = 0.8

_NOT_LINUX: Final = frozenset(
    {
        CapturePlatform.WINDOWS,
        CapturePlatform.MACOS,
        CapturePlatform.UNSUPPORTED_OS,
    }
)

_LOGIN1_NAME: Final = "org.freedesktop.login1"
_LOGIN1_PATH: Final = "/org/freedesktop/login1"
_LOGIN1_MANAGER: Final = "org.freedesktop.login1.Manager"
_LOGIN1_SESSION: Final = "org.freedesktop.login1.Session"
_PROPERTIES_IFACE: Final = "org.freedesktop.DBus.Properties"
# (bus name, object path, interface) of the screensaver fallbacks.
_SCREENSAVERS: Final = (
    (
        "org.freedesktop.ScreenSaver",
        "/org/freedesktop/ScreenSaver",
        "org.freedesktop.ScreenSaver",
    ),
    ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver"),
)


async def _open_bus(bus_type: BusType, address: str | None) -> MessageBus | None:
    """A connected bus, or None when it cannot be reached in time."""
    try:
        bus = MessageBus(bus_address=address, bus_type=bus_type)
    except Exception as exc:
        logger.debug("D-Bus address unavailable: %s", exc)
        return None
    try:
        await asyncio.wait_for(bus.connect(), _STEP_TIMEOUT_S)
    except Exception as exc:
        logger.debug("D-Bus connection failed: %s", exc)
        bus.disconnect()
        return None
    return bus


async def _close_bus(bus: MessageBus) -> None:
    """Disconnect a connected bus and give the loop a few turns to see the
    shut-down socket and close it before the private loop ends."""
    bus.disconnect()
    for _ in range(_CLOSE_TURNS):
        await asyncio.sleep(0)


async def _call(bus: MessageBus, message: Message) -> list[object] | None:
    """The reply body, or None on an error reply, timeout or failure."""
    try:
        reply = await asyncio.wait_for(bus.call(message), _STEP_TIMEOUT_S)
    except Exception as exc:
        logger.debug("D-Bus call %s failed: %s", message.member, exc)
        return None
    if reply is None or reply.message_type == MessageType.ERROR:
        return None
    body: list[object] = list(reply.body)
    return body


async def _logind_locked(environ: Mapping[str, str]) -> bool | None:
    bus = await _open_bus(BusType.SYSTEM, None)
    if bus is None:
        return None
    try:
        session_id = environ.get("XDG_SESSION_ID", "")
        if session_id:
            message = Message(
                destination=_LOGIN1_NAME,
                path=_LOGIN1_PATH,
                interface=_LOGIN1_MANAGER,
                member="GetSession",
                signature="s",
                body=[session_id],
            )
        else:
            message = Message(
                destination=_LOGIN1_NAME,
                path=_LOGIN1_PATH,
                interface=_LOGIN1_MANAGER,
                member="GetSessionByPID",
                signature="u",
                body=[os.getpid()],
            )
        body = await _call(bus, message)
        if not body or not isinstance(body[0], str):
            return None
        reply = await _call(
            bus,
            Message(
                destination=_LOGIN1_NAME,
                path=body[0],
                interface=_PROPERTIES_IFACE,
                member="Get",
                signature="ss",
                body=[_LOGIN1_SESSION, "LockedHint"],
            ),
        )
        value = reply[0] if reply else None
        if isinstance(value, Variant) and isinstance(value.value, bool):
            return value.value
        return None
    finally:
        await _close_bus(bus)


async def _screensaver_locked(environ: Mapping[str, str]) -> bool | None:
    address = session_bus_address(environ)
    if address is None:
        return None
    bus = await _open_bus(BusType.SESSION, address)
    if bus is None:
        return None
    try:
        for name, path, interface in _SCREENSAVERS:
            body = await _call(
                bus,
                Message(
                    destination=name,
                    path=path,
                    interface=interface,
                    member="GetActive",
                ),
            )
            if body and isinstance(body[0], bool):
                return body[0]
        return None
    finally:
        await _close_bus(bus)


async def _locked(environ: Mapping[str, str]) -> bool:
    locked = await _logind_locked(environ)
    if locked is None:
        locked = await _screensaver_locked(environ)
    return bool(locked)


def _run_lock_check(environ: Mapping[str, str]) -> bool:
    """Run ``_locked`` on a private loop within the budget; False on any
    failure or timeout."""

    async def bounded() -> bool:
        try:
            return await asyncio.wait_for(_locked(environ), _LOCK_BUDGET_S)
        except TimeoutError:
            logger.debug("Screen lock check timed out")
            return False

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(bounded())
    # Called from a thread that runs a loop: never block that loop's
    # asyncio.run; use a short-lived thread instead.
    result: list[bool] = []

    def worker() -> None:
        try:
            result.append(asyncio.run(bounded()))
        except Exception:
            logger.debug("Screen lock check failed", exc_info=True)

    thread = threading.Thread(target=worker, name="capture-lock-check", daemon=True)
    thread.start()
    thread.join(_LOCK_BUDGET_S + 0.5)
    return bool(result and result[0])


class _LinuxCaptureSession:
    """``CaptureSessionBackend`` for every Linux capture path."""

    def problem(self) -> tuple[CaptureStatus, str] | None:
        try:
            return self._problem(os.environ)
        except Exception:
            logger.warning("Linux capture session check failed", exc_info=True)
            return None

    def _problem(self, environ: Mapping[str, str]) -> tuple[CaptureStatus, str] | None:
        platform = detect_capture_platform(sys.platform, environ)
        if platform in _NOT_LINUX:
            return CaptureStatus.UNSUPPORTED, UNSUPPORTED_OS_DETAIL
        if platform == CapturePlatform.NO_DISPLAY:
            return CaptureStatus.NOT_INTERACTIVE, NO_DISPLAY_DETAIL
        # Every path but plain X11 (GNOME_X11 included) talks D-Bus.
        if platform != CapturePlatform.X11 and session_bus_address(environ) is None:
            return CaptureStatus.NOT_INTERACTIVE, NO_DISPLAY_DETAIL
        if platform in (CapturePlatform.KDE_WAYLAND, CapturePlatform.OTHER_WAYLAND):
            return CaptureStatus.COMPOSITOR_UNSUPPORTED, COMPOSITOR_UNSUPPORTED_DETAIL
        if _run_lock_check(environ):
            return CaptureStatus.NOT_INTERACTIVE, _LOCKED_DETAIL
        return None


SESSION: CaptureSessionBackend = _LinuxCaptureSession()
