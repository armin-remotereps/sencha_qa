from __future__ import annotations

import logging
import os
import tempfile
import threading
from collections.abc import Callable
from types import TracebackType
from typing import TypeVar

from django.conf import settings
from vncdotool import api

from environments.types import ContainerPorts

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_DISCONNECT_TIMEOUT_SECONDS: int = 5


class VncSessionManager:
    """Persistent VNC connection manager for an agent run.

    Lazily connects on first operation.  Auto-reconnects once if
    the underlying connection is dead.

    Usage::

        with VncSessionManager(ports) as vnc:
            screenshot_bytes = vnc.capture_screen()
            vnc.mouse_click(100, 200)
    """

    def __init__(self, ports: ContainerPorts) -> None:
        self._ports = ports
        self._client: api.VNCDoToolClient | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> VncSessionManager:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._client is not None

    def connect(self) -> None:
        with self._lock:
            if self._client is not None:
                return
            self._connect_locked()

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def capture_screen(self) -> bytes:
        return self._with_auto_reconnect("capture_screen", self._do_capture_screen)

    def mouse_move(self, x: int, y: int) -> None:
        self._with_auto_reconnect("mouse_move", lambda: self._do_mouse_move(x, y))

    def mouse_click(self, x: int, y: int, button: int = 1) -> None:
        self._with_auto_reconnect(
            "mouse_click", lambda: self._do_mouse_click(x, y, button)
        )

    def type_text(self, text: str) -> None:
        self._with_auto_reconnect("type_text", lambda: self._do_type_text(text))

    def key_press(self, key: str) -> None:
        self._with_auto_reconnect("key_press", lambda: self._do_key_press(key))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _with_auto_reconnect(
        self,
        operation_name: str,
        operation: Callable[[], _T],
    ) -> _T:
        """Execute operation under lock with one automatic reconnection on failure."""
        with self._lock:
            self._ensure_connected()
            try:
                return operation()
            except Exception as first_err:
                logger.warning(
                    "VNC %s failed, reconnecting: %s", operation_name, first_err
                )
                self._connect_locked()
                return operation()

    def _ensure_connected(self) -> None:
        if self._client is None:
            self._connect_locked()

    def _connect_locked(self) -> None:
        self._close_locked()
        server = f"localhost::{self._ports.vnc}"
        password: str = settings.ENV_VNC_PASSWORD
        self._client = api.connect(server, password=password, timeout=30)
        logger.debug("VNC session connected to port %d", self._ports.vnc)

    def _close_locked(self) -> None:
        if self._client is not None:
            client = self._client
            self._client = None
            try:
                t = threading.Thread(target=client.disconnect, daemon=True)
                t.start()
                t.join(timeout=_DISCONNECT_TIMEOUT_SECONDS)
                if t.is_alive():
                    logger.warning(
                        "VNC disconnect timed out after %ds",
                        _DISCONNECT_TIMEOUT_SECONDS,
                    )
            except Exception:
                logger.debug("Error closing VNC client", exc_info=True)
            try:
                api.shutdown()
            except Exception:
                logger.debug("Error shutting down VNC reactor", exc_info=True)

    def _do_capture_screen(self) -> bytes:
        if self._client is None:
            msg = "VNC client is not connected"
            raise RuntimeError(msg)
        # vncdotool.captureScreen requires a filepath, not a file-like object
        fd, tmpfile = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            self._client.captureScreen(tmpfile)
            with open(tmpfile, "rb") as f:
                return f.read()
        finally:
            try:
                os.unlink(tmpfile)
            except OSError:
                pass

    def _do_mouse_move(self, x: int, y: int) -> None:
        if self._client is None:
            msg = "VNC client is not connected"
            raise RuntimeError(msg)
        self._client.mouseMove(x, y)

    def _do_mouse_click(self, x: int, y: int, button: int) -> None:
        if self._client is None:
            msg = "VNC client is not connected"
            raise RuntimeError(msg)
        self._client.mouseMove(x, y)
        self._client.mousePress(button)

    def _do_type_text(self, text: str) -> None:
        if self._client is None:
            msg = "VNC client is not connected"
            raise RuntimeError(msg)
        self._client.type(text)

    def _do_key_press(self, key: str) -> None:
        if self._client is None:
            msg = "VNC client is not connected"
            raise RuntimeError(msg)
        self._client.keyPress(key)
