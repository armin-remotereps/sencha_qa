"""Wayland Pass / Fail shortcuts through the xdg-desktop-portal GlobalShortcuts.

Wayland lets no client grab keys system-wide; the compositor offers it through
``org.freedesktop.portal.GlobalShortcuts`` (GNOME 48+, KDE Plasma 6), which
shows its own permission dialog the first time. All D-Bus traffic runs on a
private asyncio loop in the daemon thread ``person-window-portal`` with its own
session bus connection; ``enable`` / ``disable`` / ``close`` only record what
is wanted and schedule the work there, so the Tk thread never waits.

The portal attributes the shortcuts to an app id. Before any other portal call
the connection registers ``APP_ID`` (``org.freedesktop.host.portal.Registry``,
xdg-desktop-portal 1.19+), which must match a ``.desktop`` file, so a hidden
one is written to the user's applications folder when missing. Without the
Registry interface the portal derives the id itself.

A portal session binds only once, so each enable opens a new session and each
disable closes it (which releases the grab). ``KEEP_SESSION_OPEN`` switches to
one session for the window's lifetime, for compositors that show their dialog
again for every new session; disable then only ignores activations, and the
combinations stay swallowed while the window lives.
"""

import asyncio
import logging
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Final

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from controller_client.manual_hotkeys_base import (
    OFF_STATUS,
    HotkeyKind,
    HotkeyQueue,
    HotkeyState,
    HotkeyStatus,
    default_triggers,
    focus_only,
)
from controller_client.update_layout import (
    RUNNING_PACKAGE_DIR,
    base_for_install_root,
    start_script_path,
)

logger = logging.getLogger(__name__)

APP_ID: Final = "com.punkhazard.TestRunner"
# Session per enable (the spec's model). Flip when a compositor turns out to
# show its dialog for every new session (verify on GNOME 50 and Plasma 6).
KEEP_SESSION_OPEN: Final = False

_PORTAL_NAME: Final = "org.freedesktop.portal.Desktop"
_PORTAL_PATH: Final = "/org/freedesktop/portal/desktop"
_SHORTCUTS_IFACE: Final = "org.freedesktop.portal.GlobalShortcuts"
_REQUEST_IFACE: Final = "org.freedesktop.portal.Request"
_SESSION_IFACE: Final = "org.freedesktop.portal.Session"
_REGISTRY_IFACE: Final = "org.freedesktop.host.portal.Registry"
_PROPERTIES_IFACE: Final = "org.freedesktop.DBus.Properties"
_DBUS_NAME: Final = "org.freedesktop.DBus"
_DBUS_PATH: Final = "/org/freedesktop/DBus"

# Plain calls answer at once; a request's Response may wait for the person in
# the desktop's dialog.
_CALL_TIMEOUT_S: Final = 5.0
_RESPONSE_TIMEOUT_S: Final = 60.0
_RESPONSE_SUCCESS: Final = 0
_RESPONSE_CANCELLED: Final = 1

_PREFERRED_TRIGGERS: Final[Mapping[HotkeyKind, str]] = {
    HotkeyKind.PASS: "CTRL+SHIFT+p",
    HotkeyKind.FAIL: "CTRL+SHIFT+f",
}
_DESCRIPTIONS: Final[Mapping[HotkeyKind, str]] = {
    HotkeyKind.PASS: "Pass the current step",
    HotkeyKind.FAIL: "Fail the current step",
}
_PENDING_STATUS: Final = HotkeyStatus(
    state=HotkeyState.PENDING, reason="Waiting for the desktop's shortcut service"
)
# Only these characters go into the desktop entry's Exec line unquoted: none
# is reserved by the Exec field (quoting, field codes) or the key file format.
_EXEC_SAFE_PATH: Final = re.compile(r"[A-Za-z0-9._/+-]+")
# close() waits this long for the loop thread; _shutdown stays well within it.
_THREAD_JOIN_TIMEOUT_S: Final = 1.0
# How long _shutdown lets cancelled work unwind, and closing the session take.
_SHUTDOWN_STEP_S: Final = 0.3

_Results = Mapping[str, object]
_Work = Callable[[], Coroutine[object, object, None]]


class _PortalError(Exception):
    """A user-safe reason; ``sticky`` ones are not retried in this process."""

    def __init__(self, reason: str, *, sticky: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.sticky = sticky


def _session_bus_address() -> str | None:
    # dbus-fast falls back to X11 autolaunch files, absent on Wayland; systemd
    # user sessions always put the bus at $XDG_RUNTIME_DIR/bus.
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if address:
        return address
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime_dir:
        socket_path = os.path.join(runtime_dir, "bus")
        if os.path.exists(socket_path):
            return f"unix:path={socket_path}"
    return None


def _desktop_entry_text() -> str:
    script = start_script_path(base_for_install_root(RUNNING_PACKAGE_DIR.parent))
    exec_line = "true"
    if script.is_file() and _EXEC_SAFE_PATH.fullmatch(str(script)):
        exec_line = str(script)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Test Runner\n"
        "Comment=Punk Hazard Test Runner (Manual run shortcuts)\n"
        f"Exec={exec_line}\n"
        "NoDisplay=true\n"
        "Terminal=false\n"
    )


def ensure_desktop_entry() -> None:
    """Write ``~/.local/share/applications/<APP_ID>.desktop`` if missing."""
    folder = Path.home() / ".local" / "share" / "applications"
    target = folder / f"{APP_ID}.desktop"
    if target.exists():
        return
    folder.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=folder, prefix=f".{APP_ID}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_desktop_entry_text())
        os.chmod(temp, 0o644)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _variant_value(results: _Results, key: str) -> object:
    value = results.get(key)
    return value.value if isinstance(value, Variant) else None


def _parse_shortcuts(results: _Results) -> dict[HotkeyKind, str]:
    """Bound kinds and their trigger labels from a ``shortcuts`` result."""
    entries = _variant_value(results, "shortcuts")
    bound: dict[HotkeyKind, str] = {}
    defaults = default_triggers()
    if not isinstance(entries, list):
        return bound
    for entry in entries:
        if not (isinstance(entry, list) and len(entry) == 2):
            continue
        shortcut_id, properties = entry
        if not (isinstance(shortcut_id, str) and isinstance(properties, dict)):
            continue
        try:
            kind = HotkeyKind(shortcut_id)
        except ValueError:
            continue
        label = _variant_value(properties, "trigger_description")
        text = label.strip() if isinstance(label, str) else ""
        bound[kind] = text or defaults[kind]
    return bound


def _shortcut_specs() -> list[object]:
    return [
        [
            kind.value,
            {
                "description": Variant("s", _DESCRIPTIONS[kind]),
                "preferred_trigger": Variant("s", _PREFERRED_TRIGGERS[kind]),
            },
        ]
        for kind in HotkeyKind
    ]


def _match_rule(interface: str, member: str, path: str | None = None) -> str:
    rule = f"type='signal',interface='{interface}',member='{member}'"
    return rule + (f",path='{path}'" if path else "")


class PortalHotkeys:
    """GlobalShortcuts portal backend (see the module docstring)."""

    def __init__(
        self, events: HotkeyQueue, *, x11_window_id: int, dry_run: bool
    ) -> None:
        self._events = events
        # Tk is an X client (XWayland), so the dialog's parent is its frame.
        self._parent_window = f"x11:{x11_window_id:x}" if x11_window_id > 0 else ""
        self._dry_run = dry_run
        # Shared with the Tk thread, under _lock.
        self._lock = threading.Lock()
        self._status: HotkeyStatus = OFF_STATUS
        self._wanted = False
        self._closed = False
        self._generation = 0
        # A failure retrying cannot fix (no portal, denied in the dialog).
        self._failure: HotkeyStatus | None = None
        # What to show while the session is open and wanted.
        self._bound: HotkeyStatus | None = None
        # Loop thread only.
        self._loop = asyncio.new_event_loop()
        self._reconciling = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._bus: MessageBus | None = None
        self._session: str | None = None
        self._attempted = -1
        self._responses: dict[str, asyncio.Future[tuple[int, _Results]]] = {}
        self._thread = threading.Thread(
            target=self._run_loop, name="person-window-portal", daemon=True
        )
        self._thread.start()

    # -- Tk thread -------------------------------------------------------

    @property
    def status(self) -> HotkeyStatus:
        with self._lock:
            return self._status

    def enable(self) -> None:
        with self._lock:
            if self._closed or self._wanted:
                return
            self._wanted = True
            self._generation += 1
            if self._failure is not None:
                self._status = self._failure
                return
            self._status = self._bound or _PENDING_STATUS
        self._schedule(self._reconcile)

    def disable(self) -> None:
        with self._lock:
            if self._closed or not self._wanted:
                return
            self._wanted = False
            self._status = OFF_STATUS
        self._schedule(self._reconcile)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._wanted = False
            self._status = OFF_STATUS
        self._schedule(self._shutdown)
        if threading.current_thread() is self._thread:
            return
        self._thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
        if self._thread.is_alive():
            logger.warning("The portal shortcut thread did not stop in time")

    def _schedule(self, work: _Work) -> None:
        try:
            self._loop.call_soon_threadsafe(self._spawn, work)
        except RuntimeError:
            # The loop already stopped (closed); nothing left to do.
            logger.debug("Portal loop is gone; dropped a shortcut request")

    # -- loop thread -----------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def _spawn(self, work: _Work) -> None:
        task = self._loop.create_task(work())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _snapshot(self) -> tuple[bool, bool, int]:
        with self._lock:
            return self._wanted, self._closed, self._generation

    def _publish(self, status: HotkeyStatus, generation: int) -> None:
        with self._lock:
            if self._wanted and not self._closed and generation == self._generation:
                self._status = status

    def _set_bound(self, status: HotkeyStatus | None) -> None:
        with self._lock:
            self._bound = status

    async def _reconcile(self) -> None:
        """Bring the session in line with what the Tk thread wants."""
        async with self._reconciling:
            while True:
                wanted, closed, generation = self._snapshot()
                if closed:
                    return
                if wanted and self._session is not None:
                    with self._lock:
                        bound = self._bound
                    if bound is not None:
                        self._publish(bound, generation)
                    return
                if wanted:
                    if generation == self._attempted:
                        return
                    self._attempted = generation
                    self._publish(await self._attempt(), generation)
                    continue
                if self._session is not None and not KEEP_SESSION_OPEN:
                    await self._close_session()
                    continue
                return

    async def _attempt(self) -> HotkeyStatus:
        try:
            bus = await self._ensure_bus()
            await self._check_portal(bus)
            if self._dry_run:
                return await self._dry_check(bus)
            return await self._bind(bus)
        except _PortalError as exc:
            status = focus_only(exc.reason)
            if exc.sticky:
                with self._lock:
                    self._failure = status
            return status
        except Exception:
            logger.exception("Global shortcut portal failed")
            self._drop_bus()
            return focus_only("The desktop's shortcut service failed")

    async def _shutdown(self) -> None:
        try:
            # A pending BindShortcuts may hold _reconciling for up to
            # _RESPONSE_TIMEOUT_S: cancel the in-flight work first.
            current = asyncio.current_task()
            others = [task for task in self._tasks if task is not current]
            for task in others:
                task.cancel()
            if others:
                await asyncio.wait(others, timeout=_SHUTDOWN_STEP_S)
            if not self._reconciling.locked():
                async with self._reconciling:
                    if self._session is not None:
                        await asyncio.wait_for(self._close_session(), _SHUTDOWN_STEP_S)
        except Exception:
            logger.debug("Portal shutdown did not close the session", exc_info=True)
        finally:
            # Disconnecting also ends the portal session (releases the keys).
            try:
                self._drop_bus()
            except Exception:
                logger.debug("Portal bus disconnect failed", exc_info=True)
            self._loop.stop()

    # -- bus -------------------------------------------------------------

    def _drop_bus(self) -> None:
        bus, self._bus = self._bus, None
        self._session = None
        self._set_bound(None)
        if bus is not None:
            bus.disconnect()

    async def _ensure_bus(self) -> MessageBus:
        bus = self._bus
        if bus is not None and bus.connected:
            return bus
        self._drop_bus()
        address = _session_bus_address()
        if address is None:
            raise _PortalError("No desktop session bus was found", sticky=True)
        bus = MessageBus(bus_address=address, bus_type=BusType.SESSION)
        try:
            await asyncio.wait_for(bus.connect(), _CALL_TIMEOUT_S)
        except Exception as exc:
            bus.disconnect()
            logger.warning("Session bus connection failed: %s", exc)
            raise _PortalError("Could not reach the desktop session bus") from exc
        self._bus = bus
        bus.add_message_handler(self._on_message)
        try:
            await asyncio.to_thread(ensure_desktop_entry)
        except OSError as exc:
            logger.warning("Could not write the %s desktop entry: %s", APP_ID, exc)
        await self._register(bus)
        for interface, member in (
            (_SHORTCUTS_IFACE, "Activated"),
            (_SESSION_IFACE, "Closed"),
        ):
            await self._dbus_match(bus, "AddMatch", _match_rule(interface, member))
        return bus

    async def _register(self, bus: MessageBus) -> None:
        # Must precede every other portal call on this connection.
        reply = await self._call_raw(
            bus,
            Message(
                destination=_PORTAL_NAME,
                path=_PORTAL_PATH,
                interface=_REGISTRY_IFACE,
                member="Register",
                signature="sa{sv}",
                body=[APP_ID, {}],
            ),
        )
        if reply.message_type == MessageType.ERROR:
            # Older portals lack the interface; the portal derives the id.
            logger.info("Portal Registry.Register unavailable: %s", reply.error_name)

    async def _check_portal(self, bus: MessageBus) -> None:
        reply = await self._call_raw(
            bus,
            Message(
                destination=_PORTAL_NAME,
                path=_PORTAL_PATH,
                interface=_PROPERTIES_IFACE,
                member="Get",
                signature="ss",
                body=[_SHORTCUTS_IFACE, "version"],
            ),
        )
        value = reply.body[0] if reply.body else None
        version = value.value if isinstance(value, Variant) else None
        if reply.message_type == MessageType.ERROR or not isinstance(version, int):
            logger.info("GlobalShortcuts portal missing: %s", reply.error_name)
            raise _PortalError(
                "This desktop has no global shortcut service", sticky=True
            )

    async def _call_raw(self, bus: MessageBus, message: Message) -> Message:
        try:
            return await asyncio.wait_for(bus.call(message), _CALL_TIMEOUT_S)
        except TimeoutError:
            raise _PortalError(
                "The desktop's shortcut service did not answer"
            ) from None
        except Exception as exc:
            logger.warning("Portal call %s failed: %s", message.member, exc)
            self._drop_bus()
            raise _PortalError(
                "Lost the connection to the desktop's shortcut service"
            ) from exc

    async def _dbus_match(self, bus: MessageBus, member: str, rule: str) -> None:
        await self._call_raw(
            bus,
            Message(
                destination=_DBUS_NAME,
                path=_DBUS_PATH,
                interface=_DBUS_NAME,
                member=member,
                signature="s",
                body=[rule],
            ),
        )

    async def _request(
        self,
        bus: MessageBus,
        member: str,
        signature: str,
        args: list[object],
        options: dict[str, Variant],
    ) -> tuple[int, _Results]:
        """Call a portal method that answers through a Request's Response."""
        token = f"punkhazard_{secrets.token_hex(8)}"
        sender = (bus.unique_name or "").lstrip(":").replace(".", "_")
        path = f"{_PORTAL_PATH}/request/{sender}/{token}"
        future: asyncio.Future[tuple[int, _Results]] = self._loop.create_future()
        self._responses[path] = future
        rule = _match_rule(_REQUEST_IFACE, "Response", path)
        await self._dbus_match(bus, "AddMatch", rule)
        try:
            reply = await self._call_raw(
                bus,
                Message(
                    destination=_PORTAL_NAME,
                    path=_PORTAL_PATH,
                    interface=_SHORTCUTS_IFACE,
                    member=member,
                    signature=signature,
                    body=[*args, {**options, "handle_token": Variant("s", token)}],
                ),
            )
            if reply.message_type == MessageType.ERROR:
                logger.warning("Portal %s refused: %s", member, reply.error_name)
                raise _PortalError("The desktop refused the shortcut request")
            handle = reply.body[0] if reply.body else None
            if isinstance(handle, str) and handle != path:
                # Portals before 0.9 return an unpredictable handle.
                self._responses[handle] = future
            try:
                return await asyncio.wait_for(future, _RESPONSE_TIMEOUT_S)
            except TimeoutError:
                await self._close_object(bus, path, _REQUEST_IFACE)
                raise _PortalError(
                    "The desktop's shortcut dialog was not answered in time"
                ) from None
        finally:
            for key in [k for k, v in self._responses.items() if v is future]:
                del self._responses[key]
            if self._bus is bus:
                try:
                    await self._dbus_match(bus, "RemoveMatch", rule)
                except _PortalError:
                    logger.debug("Portal RemoveMatch failed")

    async def _close_object(self, bus: MessageBus, path: str, interface: str) -> None:
        try:
            await self._call_raw(
                bus,
                Message(
                    destination=_PORTAL_NAME,
                    path=path,
                    interface=interface,
                    member="Close",
                ),
            )
        except _PortalError:
            logger.debug("Portal Close of %s failed", path)

    # -- sessions --------------------------------------------------------

    async def _create_session(self, bus: MessageBus) -> str:
        code, results = await self._request(
            bus,
            "CreateSession",
            "a{sv}",
            [],
            {
                "session_handle_token": Variant(
                    "s", f"punkhazard_{secrets.token_hex(8)}"
                )
            },
        )
        session = _variant_value(results, "session_handle")
        if code != _RESPONSE_SUCCESS or not isinstance(session, str) or not session:
            raise _PortalError("The desktop refused a shortcut session")
        return session

    async def _close_session(self) -> None:
        session, self._session = self._session, None
        self._set_bound(None)
        bus = self._bus
        if session is not None and bus is not None and bus.connected:
            await self._close_object(bus, session, _SESSION_IFACE)

    async def _bind(self, bus: MessageBus) -> HotkeyStatus:
        self._session = await self._create_session(bus)
        try:
            code, results = await self._request(
                bus,
                "BindShortcuts",
                "oa(sa{sv})sa{sv}",
                [self._session, _shortcut_specs(), self._parent_window],
                {},
            )
        except BaseException:
            await self._close_session()
            raise
        if code != _RESPONSE_SUCCESS:
            await self._close_session()
            if code == _RESPONSE_CANCELLED:
                raise _PortalError(
                    "Shortcut permission was denied in the desktop's dialog",
                    sticky=True,
                )
            raise _PortalError("The desktop did not bind the shortcuts")
        bound = _parse_shortcuts(results)
        missing = [kind for kind in HotkeyKind if kind not in bound]
        triggers = {**default_triggers(), **bound}
        if missing:
            names = " and ".join(default_triggers()[kind] for kind in missing)
            status = HotkeyStatus(
                state=HotkeyState.FOCUS_ONLY,
                reason=f"The desktop did not assign {names}",
                triggers=triggers,
            )
        else:
            status = HotkeyStatus(state=HotkeyState.ACTIVE, triggers=triggers)
        self._set_bound(status)
        return status

    async def _dry_check(self, bus: MessageBus) -> HotkeyStatus:
        """Interfaces present; earlier bindings listed, nothing bound."""
        session = await self._create_session(bus)
        try:
            code, results = await self._request(
                bus, "ListShortcuts", "oa{sv}", [session], {}
            )
        finally:
            await self._close_object(bus, session, _SESSION_IFACE)
        bound = _parse_shortcuts(results) if code == _RESPONSE_SUCCESS else {}
        reason = "Portal available (not bound in a dry run)"
        if all(kind in bound for kind in HotkeyKind):
            reason += "; the shortcuts were approved before"
        return HotkeyStatus(
            state=HotkeyState.ACTIVE,
            reason=reason,
            triggers={**default_triggers(), **bound},
        )

    # -- signals ---------------------------------------------------------

    def _on_message(self, message: Message) -> None:
        if message.message_type != MessageType.SIGNAL:
            return
        body = message.body
        if message.interface == _REQUEST_IFACE and message.member == "Response":
            future = self._responses.get(message.path or "")
            if future is None or future.done():
                return
            code = body[0] if body and isinstance(body[0], int) else -1
            results = body[1] if len(body) > 1 and isinstance(body[1], dict) else {}
            future.set_result((code, results))
            return
        if self._session is None:
            return
        if message.interface == _SESSION_IFACE and message.member == "Closed":
            if message.path != self._session:
                return
            # The desktop ended the session (shortcuts revoked, portal restart).
            self._session = None
            self._set_bound(None)
            _, _, generation = self._snapshot()
            self._publish(
                focus_only("The desktop ended the shortcut session"), generation
            )
            return
        if message.interface == _SHORTCUTS_IFACE and message.member == "Activated":
            if not body or body[0] != self._session:
                return
            shortcut_id = body[1] if len(body) > 1 else None
            if not isinstance(shortcut_id, str):
                return
            try:
                kind = HotkeyKind(shortcut_id)
            except ValueError:
                return
            with self._lock:
                deliver = self._wanted and not self._closed
            if deliver:
                self._events.put(kind)
