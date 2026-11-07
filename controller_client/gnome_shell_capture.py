"""Window capture on GNOME Shell, through the bundled capture extension.

Serves both GNOME session types (``CapturePlatform.GNOME_WAYLAND`` and
``GNOME_X11``). On Wayland, GNOME 41+ keeps window lists and screenshots for
its own callers; on Xorg the Shell draws its notification banners, modal
dialogs and the overview in its compositor stage, not as X windows, so the
plain X11 path could neither see nor avoid them. Either way the runner asks the small GNOME Shell extension in ``gnome_capture_helper/``
(uuid ``test-runner-capture@punk-hazard``, session-bus name
``org.testrunner.Capture1``; ``setup.sh`` and the self-update install it). It
lists windows, workspaces and monitors, saves a screen area or one window's
own content as a PNG into the runner's private folder
``$XDG_RUNTIME_DIR/test-runner-capture`` (mode 0700; the extension refuses
any other path) and reads or changes the active workspace.

Caller check: at first use the runner writes a random token
(``secrets.token_hex(32)``) into ``helper-token`` (mode 0600) in that 0700
folder; the extension reads it (owner, regular file and mode checked) and
refuses every method but ``Version`` unless the token is its first argument,
so another process of the same user cannot list, capture or switch through
it. A token refused because another runner of this user replaced the file
is written again once (only by the first of concurrent refused calls; the
others retry with that new token).

Exports (``capture_platform.py``): ``INVENTORY``, ``GRAB`` and
``create_switcher``. A missing bus name or a helper older than
``HELPER_VERSION_MIN`` raises ``CaptureUnavailableError(HELPER_MISSING)``
from every method (``switch_to`` included, before anything changes).

All D-Bus traffic runs on one private asyncio loop in the daemon thread
``gnome-capture-helper``, with its own session bus connection, as
``manual_hotkeys_portal.py`` does; the capture worker thread waits for each
call. Nothing connects or starts at import time. Temporary PNG files are
removed right after they are read.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import secrets
import stat
import sys
import threading
from collections.abc import Coroutine, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final

from PIL import Image

from controller_client.capture_platform import (
    WindowGrabBackend,
    WindowInventoryBackend,
)
from controller_client.desktop_switch import (
    DESKTOP_SWITCH_BUDGET_SECONDS,
    DesktopSwitcher,
    DesktopSwitchError,
    poll_until,
)
from controller_client.process_names import ProcessNames
from controller_client.protocol import (
    CAPTURE_DESKTOP_NAME_MAX_CHARS,
    CAPTURE_WINDOW_ID_MAX_CHARS,
    CaptureStatus,
)
from controller_client.session_bus import session_bus_address
from controller_client.window_capture_policy import unobscured_by_stacking
from controller_client.window_model import (
    CaptureUnavailableError,
    DesktopInfo,
    DesktopWindow,
    Rect,
    WindowDesktop,
)

if sys.platform != "win32" or TYPE_CHECKING:
    # No wheel on Windows; the core imports this module on Linux only.
    # TYPE_CHECKING keeps the names defined for the win32 mypy pass.
    from dbus_fast import Message, MessageType
    from dbus_fast.aio import MessageBus

logger = logging.getLogger(__name__)

HELPER_UUID: Final = "test-runner-capture@punk-hazard"
HELPER_BUS_NAME: Final = "org.testrunner.Capture1"
HELPER_OBJECT_PATH: Final = "/org/testrunner/Capture1"
HELPER_INTERFACE: Final = "org.testrunner.Capture1"
# The oldest helper this runner works with. A self-update installs a newer
# extension, but the Shell keeps running the old one until the next login,
# so newer helpers must stay compatible; bump this only when this runner
# needs a method an older helper lacks. 2: every method but Version takes
# the caller token first, and ListWindows reports overlays.
HELPER_VERSION_MIN: Final = 2
# Folder under $XDG_RUNTIME_DIR the extension writes into (extension.js
# RUNNER_DIR_NAME).
RUNNER_DIR_NAME: Final = "test-runner-capture"
# The caller token file in that folder (extension.js TOKEN_FILE_NAME).
TOKEN_FILE_NAME: Final = "helper-token"

HELPER_MISSING_DETAIL: Final = (
    "enable the Test Runner capture extension (setup script installs it) and "
    "log out and back in once"
)
_NO_SESSION_BUS: Final = (
    "no desktop session bus; start the Test Runner from a terminal inside the "
    "desktop session"
)
_NO_RUNTIME_DIR: Final = (
    "no desktop session folder (XDG_RUNTIME_DIR); start the Test Runner from a "
    "terminal inside the desktop session"
)
_RUNNER_DIR_UNUSABLE: Final = (
    "the Test Runner's capture folder under XDG_RUNTIME_DIR cannot be used"
)
_HELPER_REFUSED: Final = (
    "the Test Runner capture extension refused the capture folder; start the "
    "Test Runner from a terminal inside the desktop session"
)
_HELPER_FAILED: Final = "the Test Runner capture extension could not capture"
_HELPER_NO_ANSWER: Final = "the Test Runner capture extension did not answer"
_HELPER_BAD_REPLY: Final = "the Test Runner capture extension sent an invalid reply"
_IMAGE_UNREADABLE: Final = "the captured image could not be read"
_WINDOW_GONE: Final = "the window is gone"
_WORKSPACE_UNKNOWN: Final = "the window's workspace is unknown"
_WORKSPACE_EMPTY: Final = (
    "the current workspace has no window, so GNOME would remove it on a switch; "
    "show a workspace with a window first"
)
_SWITCH_NOT_VERIFIED: Final = "the workspace switch could not be verified"

_HELPER_UNAUTHORIZED: Final = (
    "the Test Runner capture extension did not accept this Test Runner; "
    "restart the Test Runner"
)
_ERROR_UNAUTHORIZED: Final = "org.testrunner.Capture1.Error.Unauthorized"
_ERROR_REFUSED: Final = "org.testrunner.Capture1.Error.Refused"
_ERROR_NOT_FOUND: Final = "org.testrunner.Capture1.Error.NotFound"
# The name is not owned, or an older helper lacks the method.
_HELPER_MISSING_ERRORS: Final[frozenset[str]] = frozenset(
    {
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.UnknownMethod",
        "org.freedesktop.DBus.Error.UnknownObject",
        "org.freedesktop.DBus.Error.UnknownInterface",
        "org.gnome.gjs.NotImplementedError",
    }
)

_CONNECT_TIMEOUT_S: Final = 5.0
_QUICK_CALL_TIMEOUT_S: Final = 3.0
_LIST_TIMEOUT_S: Final = 5.0
_CAPTURE_TIMEOUT_S: Final = 15.0
# Extra wait on the worker side, past the call's own timeout.
_RESULT_SLACK_S: Final = 2.0
# Bounds on what is parsed from the helper's JSON.
_LIST_JSON_MAX_CHARS: Final = 4 * 1024 * 1024
_WINDOWS_MAX: Final = 1000
_MONITORS_MAX: Final = 32
_WORKSPACES_MAX: Final = 256
_TOKEN_TEMP_PREFIX: Final = ".helper-token-"


class _UnauthorizedError(CaptureUnavailableError):
    """The helper refused the token (another runner may have replaced it)."""

    def __init__(self) -> None:
        super().__init__(CaptureStatus.CAPTURE_FAILED, _HELPER_UNAUTHORIZED)


def _helper_missing() -> CaptureUnavailableError:
    return CaptureUnavailableError(CaptureStatus.HELPER_MISSING, HELPER_MISSING_DETAIL)


def _error_from_reply(reply: Message) -> CaptureUnavailableError:
    name = reply.error_name if isinstance(reply.error_name, str) else ""
    if name in _HELPER_MISSING_ERRORS:
        return _helper_missing()
    if name == _ERROR_UNAUTHORIZED:
        return _UnauthorizedError()
    if name == _ERROR_NOT_FOUND:
        return CaptureUnavailableError(CaptureStatus.WINDOW_NOT_FOUND, _WINDOW_GONE)
    if name == _ERROR_REFUSED:
        return CaptureUnavailableError(CaptureStatus.CAPTURE_FAILED, _HELPER_REFUSED)
    logger.warning("GNOME capture helper error: %s", name or "unknown")
    return CaptureUnavailableError(CaptureStatus.CAPTURE_FAILED, _HELPER_FAILED)


def _runner_dir() -> Path:
    """``$XDG_RUNTIME_DIR/test-runner-capture``, a 0700 folder of this user."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not runtime or not os.path.isabs(runtime):
        raise CaptureUnavailableError(CaptureStatus.NOT_INTERACTIVE, _NO_RUNTIME_DIR)
    path = Path(runtime) / RUNNER_DIR_NAME
    try:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or (
            sys.platform != "win32" and info.st_uid != os.getuid()
        ):
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, _RUNNER_DIR_UNUSABLE
            )
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(path, 0o700)
    except OSError as exc:
        logger.warning("GNOME capture folder unusable: %s", exc)
        raise CaptureUnavailableError(
            CaptureStatus.CAPTURE_FAILED, _RUNNER_DIR_UNUSABLE
        ) from exc
    return path


class _CallerToken:
    """The caller token the helper checks, in ``helper-token`` of the
    runner's folder (0600). Written at first use and on ``renew``; the file
    is replaced atomically (a fresh 0600 file renamed over it), never
    followed through a link."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None

    def current(self) -> str:
        with self._lock:
            if self._token is None:
                self._token = _write_token()
            return self._token

    def renew(self, stale: str) -> str:
        """A new token in place of ``stale``, the one the helper refused.
        Written only while ``stale`` is still current, so concurrent refused
        calls write it once and do not invalidate each other's retry."""
        with self._lock:
            token = self._token
            if token is None or token == stale:
                token = _write_token()
                self._token = token
            return token


def _write_token() -> str:
    token = secrets.token_hex(32)
    folder = _runner_dir()
    temp = folder / f"{_TOKEN_TEMP_PREFIX}{secrets.token_hex(8)}"
    try:
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.write(fd, token.encode("ascii"))
            if sys.platform != "win32":
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(temp, folder / TOKEN_FILE_NAME)
    except OSError as exc:
        logger.warning("Could not write the GNOME capture token: %s", exc)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise CaptureUnavailableError(
            CaptureStatus.CAPTURE_FAILED, _RUNNER_DIR_UNUSABLE
        ) from exc
    return token


_TOKEN: Final = _CallerToken()


class _HelperClient:
    """The session bus connection to the helper, on a private loop thread.

    ``call`` blocks the calling (worker) thread until the reply. The loop
    thread starts on the first call; the connection is reused and dropped on
    any transport error, and the helper's ``Version`` is checked once per
    connection.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Loop thread only.
        self._bus: MessageBus | None = None
        self._version_ok = False

    def call(
        self, member: str, signature: str, body: list[object], timeout: float
    ) -> list[object]:
        """Call ``member`` with the caller token prepended (signature ``s`` +
        ``signature``); a refused token is written again and the call
        repeated once."""
        token = _TOKEN.current()
        try:
            return self._run(
                self._call(member, "s" + signature, [token, *body], timeout),
                timeout,
            )
        except _UnauthorizedError:
            logger.warning("GNOME capture helper refused the token; renewing it")
            token = _TOKEN.renew(token)
            return self._run(
                self._call(member, "s" + signature, [token, *body], timeout),
                timeout,
            )

    def version(self) -> int | None:
        """The running helper's version, or None when it is absent."""
        try:
            return self._run(self._version(), _QUICK_CALL_TIMEOUT_S)
        except CaptureUnavailableError:
            return None

    def _run[T](self, work: Coroutine[object, object, T], timeout: float) -> T:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(work, loop)
        try:
            return future.result(_CONNECT_TIMEOUT_S + timeout + _RESULT_SLACK_S)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, _HELPER_NO_ANSWER
            ) from None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=self._run_loop,
                    args=(loop,),
                    name="gnome-capture-helper",
                    daemon=True,
                )
                thread.start()
                self._loop = loop
            return self._loop

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    # -- loop thread -----------------------------------------------------

    async def _call(
        self, member: str, signature: str, body: list[object], timeout: float
    ) -> list[object]:
        bus = await self._connect()
        if not self._version_ok:
            version = await self._read_version(bus)
            if version < HELPER_VERSION_MIN:
                logger.info("GNOME capture helper %d is too old", version)
                raise _helper_missing()
            self._version_ok = True
        reply = await self._send(bus, member, signature, body, timeout)
        result: list[object] = list(reply.body)
        return result

    async def _version(self) -> int:
        return await self._read_version(await self._connect())

    async def _read_version(self, bus: MessageBus) -> int:
        reply = await self._send(bus, "Version", "", [], _QUICK_CALL_TIMEOUT_S)
        body = reply.body
        version = body[0] if body else None
        if not isinstance(version, int):
            raise _helper_missing()
        return version

    async def _connect(self) -> MessageBus:
        if self._bus is not None and self._bus.connected:
            return self._bus
        self._drop_bus()
        address = session_bus_address()
        if address is None:
            raise CaptureUnavailableError(
                CaptureStatus.NOT_INTERACTIVE, _NO_SESSION_BUS
            )
        try:
            bus = MessageBus(bus_address=address)
        except Exception as exc:
            logger.warning("Session bus address unusable: %s", exc)
            raise CaptureUnavailableError(
                CaptureStatus.NOT_INTERACTIVE, _NO_SESSION_BUS
            ) from exc
        try:
            await asyncio.wait_for(bus.connect(), _CONNECT_TIMEOUT_S)
        except Exception as exc:
            logger.warning("Could not connect to the session bus: %s", exc)
            try:
                bus.disconnect()
            except Exception as close_exc:
                logger.debug("Session bus disconnect failed: %s", close_exc)
            raise CaptureUnavailableError(
                CaptureStatus.NOT_INTERACTIVE, _NO_SESSION_BUS
            ) from exc
        self._bus = bus
        return bus

    async def _send(
        self,
        bus: MessageBus,
        member: str,
        signature: str,
        body: list[object],
        timeout: float,
    ) -> Message:
        message = Message(
            destination=HELPER_BUS_NAME,
            path=HELPER_OBJECT_PATH,
            interface=HELPER_INTERFACE,
            member=member,
            signature=signature,
            body=body,
        )
        try:
            reply = await asyncio.wait_for(bus.call(message), timeout)
        except TimeoutError:
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, _HELPER_NO_ANSWER
            ) from None
        except Exception as exc:
            logger.warning("GNOME capture helper call %s failed: %s", member, exc)
            self._drop_bus()
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, _HELPER_FAILED
            ) from exc
        if reply is None:
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, _HELPER_BAD_REPLY
            )
        if reply.message_type == MessageType.ERROR:
            raise _error_from_reply(reply)
        return reply

    def _drop_bus(self) -> None:
        bus, self._bus = self._bus, None
        self._version_ok = False
        if bus is not None:
            try:
                bus.disconnect()
            except Exception as exc:
                logger.debug("Session bus disconnect failed: %s", exc)


_CLIENT: Final = _HelperClient()


def helper_version() -> int | None:
    """The running helper's ``Version`` (None when not reachable); for
    diagnostics. Blocking."""
    return _CLIENT.version()


# -- the helper's ListWindows JSON ----------------------------------------


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_rect(value: object) -> Rect | None:
    if not isinstance(value, dict):
        return None
    x, y = _as_int(value.get("x")), _as_int(value.get("y"))
    width, height = _as_int(value.get("width")), _as_int(value.get("height"))
    if x is None or y is None or width is None or height is None:
        return None
    return Rect(x, y, max(width, 0), max(height, 0))


class _Listing:
    """One parsed ``ListWindows`` answer (helper workspace indexes 0-based)."""

    def __init__(self, data: dict[str, object]) -> None:
        count = _as_int(data.get("workspace_count"))
        active = _as_int(data.get("active_workspace"))
        if count is None or not 1 <= count <= _WORKSPACES_MAX:
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, _HELPER_BAD_REPLY
            )
        self.workspace_count = count
        self.active = active if active is not None and 0 <= active < count else None
        raw_names = data.get("workspace_names")
        names = raw_names if isinstance(raw_names, list) else []
        self.names = tuple(
            _as_str(name).strip()[:CAPTURE_DESKTOP_NAME_MAX_CHARS]
            for name in names[:count]
        )
        raw_monitors = data.get("monitors")
        monitors = raw_monitors if isinstance(raw_monitors, list) else []
        self.monitors = [
            rect
            for rect in (_as_rect(item) for item in monitors[:_MONITORS_MAX])
            if rect is not None and not rect.is_empty
        ]
        self.primary = _as_int(data.get("primary_monitor"))
        raw_windows = data.get("windows")
        self.windows: list[dict[str, object]] = [
            item
            for item in (raw_windows if isinstance(raw_windows, list) else [])[
                -_WINDOWS_MAX:
            ]
            if isinstance(item, dict)
        ]

    def name_of(self, index: int) -> str | None:
        """The name of helper workspace ``index`` (0-based), None if unnamed."""
        if 0 <= index < len(self.names) and self.names[index]:
            return self.names[index]
        return None


def _list() -> _Listing:
    body = _CLIENT.call("ListWindows", "", [], _LIST_TIMEOUT_S)
    text = body[0] if body else None
    if not isinstance(text, str) or len(text) > _LIST_JSON_MAX_CHARS:
        raise CaptureUnavailableError(CaptureStatus.CAPTURE_FAILED, _HELPER_BAD_REPLY)
    try:
        data: object = json.loads(text)
    except ValueError:
        raise CaptureUnavailableError(
            CaptureStatus.CAPTURE_FAILED, _HELPER_BAD_REPLY
        ) from None
    if not isinstance(data, dict):
        raise CaptureUnavailableError(CaptureStatus.CAPTURE_FAILED, _HELPER_BAD_REPLY)
    return _Listing(data)


def _windows_of(listing: _Listing) -> list[DesktopWindow]:
    names = ProcessNames()
    result: list[DesktopWindow] = []
    # The helper lists bottom to top; the inventory is topmost first.
    for item in reversed(listing.windows):
        window_id = _as_str(item.get("id"))
        rect = _as_rect(item.get("frame"))
        # "app": a normal window; "overlay": any other visible window or
        # Shell popup (the helper leaves out the shell's panels and docks).
        app = item.get("kind") == "app"
        # Shell popups have ids "s1", "s2", ...; windows have Mutter ids.
        digits = window_id if app else window_id.removeprefix("s")
        if (
            not digits.isdigit()
            or len(window_id) > CAPTURE_WINDOW_ID_MAX_CHARS
            or rect is None
            or rect.is_empty
        ):
            continue
        pid = _as_int(item.get("pid"))
        pid = pid if pid is not None and pid > 0 else None
        on_all = item.get("on_all_workspaces") is True
        workspace = _as_int(item.get("workspace"))
        if on_all:
            desktop = WindowDesktop(current=True, index=None, name=None)
        elif workspace is not None and 0 <= workspace < listing.workspace_count:
            desktop = WindowDesktop(
                current=workspace == listing.active,
                index=workspace + 1,
                name=listing.name_of(workspace),
            )
        else:
            desktop = WindowDesktop(current=False, index=None, name=None)
        if not app and not desktop.current:
            continue
        result.append(
            DesktopWindow(
                window_id=window_id,
                pid=pid,
                # Only the pid identifies the process: the sandboxed app id
                # and WM_CLASS are chosen by the client itself.
                process=names.name(pid),
                title=_as_str(item.get("title")) if app else "",
                rect=rect,
                minimized=item.get("minimized") is True,
                focused=item.get("focused") is True,
                z=len(result),
                desktop=desktop,
                capturable=app and pid is not None,
                drawn=item.get("drawn") is not False,
            )
        )
    return result


class _GnomeInventory:
    def list_windows(self) -> tuple[DesktopInfo, list[DesktopWindow]]:
        listing = _list()
        info = DesktopInfo(
            current=None if listing.active is None else listing.active + 1,
            count=listing.workspace_count,
            names=listing.names if any(listing.names) else (),
        )
        return info, _windows_of(listing)


# -- grabs ----------------------------------------------------------------


def _capture_to_image(member: str, signature: str, args: list[object]) -> Image.Image:
    """Ask the helper to write a PNG (path appended), read it, delete it."""
    path = _runner_dir() / f"{secrets.token_hex(12)}.png"
    try:
        _CLIENT.call(member, signature, [*args, str(path)], _CAPTURE_TIMEOUT_S)
        try:
            image = Image.open(path)
            with image:
                image.load()
                return image.convert("RGB")
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            logger.warning("GNOME capture image unreadable: %s", exc)
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, _IMAGE_UNREADABLE
            ) from exc
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove a GNOME capture file: %s", exc)


class _GnomeGrab:
    def grab_rect(self, rect: Rect) -> Image.Image:
        if rect.is_empty:
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, "the area to capture is empty"
            )
        return _capture_to_image(
            "CaptureArea",
            "iiiis",
            [rect.x, rect.y, rect.width, rect.height],
        )

    def grab_window_only(self, window: DesktopWindow) -> Image.Image:
        # The window actor's own content: current even when covered.
        return _capture_to_image("CaptureWindow", "ss", [window.window_id])

    def is_unobscured(
        self, window: DesktopWindow, windows: Sequence[DesktopWindow]
    ) -> bool:
        return unobscured_by_stacking(window, windows)

    def monitor_rect(self, window: DesktopWindow) -> Rect:
        listing = _list()
        if not listing.monitors:
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, _HELPER_BAD_REPLY
            )
        best = max(listing.monitors, key=lambda m: m.intersection(window.rect).area)
        if best.intersection(window.rect).is_empty:
            primary = listing.primary
            if primary is not None and 0 <= primary < len(listing.monitors):
                return listing.monitors[primary]
            return listing.monitors[0]
        return best


# -- desktop switching ----------------------------------------------------


def _active_workspace() -> int:
    body = _CLIENT.call("ActiveWorkspace", "", [], _QUICK_CALL_TIMEOUT_S)
    index = body[0] if body else None
    if isinstance(index, bool) or not isinstance(index, int):
        raise CaptureUnavailableError(CaptureStatus.CAPTURE_FAILED, _HELPER_BAD_REPLY)
    return index


def _activate_workspace(index: int) -> None:
    _CLIENT.call("ActivateWorkspace", "i", [index], _QUICK_CALL_TIMEOUT_S)


def _is_active(index: int) -> bool:
    try:
        return _active_workspace() == index
    except CaptureUnavailableError:
        return False


class _GnomeSwitcher:
    """Switches the shown workspace through the helper (0-based indexes)."""

    def __init__(self, info: DesktopInfo, windows: Sequence[DesktopWindow]) -> None:
        self._info = info
        self._windows = list(windows)
        self._changed = False
        self._recorded: int | None = None

    @property
    def changed(self) -> bool:
        return self._changed

    def switch_to(self, window: DesktopWindow) -> None:
        if window.desktop.current:
            return
        index = window.desktop.index
        if index is None or (self._info.count is not None and index > self._info.count):
            raise DesktopSwitchError(_WORKSPACE_UNKNOWN)
        target = index - 1
        # Raises CaptureUnavailableError (helper_missing, ...) before any change.
        recorded = _active_workspace()
        self._refuse_vanishing_workspace(recorded)
        self._recorded = recorded
        if recorded == target:
            return
        self._changed = True
        try:
            _activate_workspace(target)
        except CaptureUnavailableError as exc:
            raise DesktopSwitchError(exc.detail) from exc
        if not poll_until(
            lambda: _is_active(target), budget_seconds=DESKTOP_SWITCH_BUDGET_SECONDS
        ):
            raise DesktopSwitchError(_SWITCH_NOT_VERIFIED)

    def restore(self) -> bool:
        recorded = self._recorded
        if not self._changed or recorded is None:
            return True
        try:
            _activate_workspace(recorded)
            restored = poll_until(
                lambda: _is_active(recorded),
                budget_seconds=DESKTOP_SWITCH_BUDGET_SECONDS,
            )
        except Exception as exc:
            logger.warning("GNOME workspace switch back failed: %s", exc)
            return False
        if not restored:
            logger.warning(
                "GNOME workspace switch back to %d not verified", recorded + 1
            )
        return restored

    def _refuse_vanishing_workspace(self, recorded: int) -> None:
        """GNOME's dynamic workspaces remove an empty workspace once it is no
        longer shown, unless it is the last one; switching back would then
        land elsewhere, so refuse before changing anything."""
        count = self._info.count
        if count is None or recorded >= count - 1:
            return
        has_window = any(
            w.desktop.index == recorded + 1 for w in self._windows if w.desktop.index
        )
        if not has_window:
            raise DesktopSwitchError(_WORKSPACE_EMPTY)


def create_switcher(
    info: DesktopInfo, windows: Sequence[DesktopWindow]
) -> DesktopSwitcher:
    return _GnomeSwitcher(info, windows)


INVENTORY: WindowInventoryBackend = _GnomeInventory()
GRAB: WindowGrabBackend = _GnomeGrab()
