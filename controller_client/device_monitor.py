"""Watch the phone or tablet and report its state to the server.

One per runner process, started by ``main`` beside the client and
independent of the WebSocket connection. It polls ``device.status()`` (which
never uses the Appium session) every few seconds, reports the state at once,
on every change and at least every DEVICE_STATUS_INTERVAL_SECONDS, and
(re)creates the session: at startup, when the device comes back, and after a
failure with a growing delay while the device is present. It never starts a
session once the device was released (``status`` then reads DISCONNECTED).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final

from controller_client.mobile_device import MobileDevice
from controller_client.protocol import (
    DEVICE_STATUS_INTERVAL_SECONDS,
    DeviceState,
    DeviceStatusPayload,
)

logger = logging.getLogger(__name__)

_FIRST_RETRY_SECONDS: Final[float] = 5.0
_MAX_RETRY_SECONDS: Final[float] = 60.0
_ABSENT_STATES: Final[frozenset[DeviceState]] = frozenset(
    {DeviceState.DISCONNECTED, DeviceState.UNAUTHORIZED}
)
_STATUS_FAILED: Final[DeviceStatusPayload] = DeviceStatusPayload(
    DeviceState.UNAVAILABLE, "The Test Runner could not read the device's state"
)


class DeviceMonitor:
    def __init__(
        self,
        device: MobileDevice,
        on_status: Callable[[DeviceStatusPayload], Awaitable[None]],
        poll_seconds: float = 5.0,
    ) -> None:
        self._device = device
        self._on_status = on_status
        self._poll_seconds = poll_seconds
        self._start_task: asyncio.Task[None] | None = None
        self._started_once = False
        self._retry_delay = _FIRST_RETRY_SECONDS
        self._next_start_at = 0.0
        self._last_sent: DeviceStatusPayload | None = None
        self._last_sent_at = 0.0

    async def run(self) -> None:
        """Poll and report until cancelled."""
        previous: DeviceStatusPayload | None = None
        try:
            while True:
                status = await self._read_status()
                self._plan_start(previous, status)
                await self._report(status)
                previous = status
                await self._wait()
        finally:
            if self._start_task is not None:
                # The thread finishes on its own; nothing awaits it any more.
                self._start_task.cancel()

    async def _read_status(self) -> DeviceStatusPayload:
        try:
            return await asyncio.to_thread(self._device.status)
        except Exception:
            logger.exception("Reading the device status failed")
            return _STATUS_FAILED

    def _plan_start(
        self, previous: DeviceStatusPayload | None, status: DeviceStatusPayload
    ) -> None:
        if self._start_task is not None:
            return
        state = status.state
        if state in _ABSENT_STATES:
            # Once it is back, start at once.
            self._retry_delay = _FIRST_RETRY_SECONDS
            self._next_start_at = 0.0
            return
        if state is DeviceState.READY:
            self._retry_delay = _FIRST_RETRY_SECONDS
            return
        first = not self._started_once and state is DeviceState.STARTING
        retry = (
            state is DeviceState.UNAVAILABLE and time.monotonic() >= self._next_start_at
        )
        if first or retry:
            self._launch_start()

    def _launch_start(self) -> None:
        self._started_once = True
        self._next_start_at = time.monotonic() + self._retry_delay
        self._retry_delay = min(self._retry_delay * 2, _MAX_RETRY_SECONDS)
        self._start_task = asyncio.create_task(self._start())

    async def _start(self) -> None:
        try:
            await asyncio.to_thread(self._device.start)
        except Exception as exc:
            # DeviceDisconnectedError, or a failure status() reports.
            logger.warning("Starting the device session failed: %s", exc)

    async def _report(self, status: DeviceStatusPayload) -> None:
        now = time.monotonic()
        due = now - self._last_sent_at >= DEVICE_STATUS_INTERVAL_SECONDS
        if status == self._last_sent and not due:
            return
        if status != self._last_sent:
            logger.info("Device %s %s", status.state.value, status.detail)
        self._last_sent = status
        self._last_sent_at = now
        try:
            await self._on_status(status)
        except Exception:
            logger.exception("Sending the device status failed")

    async def _wait(self) -> None:
        """Sleep one poll, waking early when a session start finishes."""
        task = self._start_task
        if task is None:
            await asyncio.sleep(self._poll_seconds)
            return
        done, _ = await asyncio.wait({task}, timeout=self._poll_seconds)
        if task in done:
            self._start_task = None
