"""Remote control of a GNOME Wayland desktop through mutter's private D-Bus API.

Wayland gives a client no way to synthesize input or read the screen. GNOME
offers both to processes in the logged-in session through two mutter services
on the session bus: ``org.gnome.Mutter.RemoteDesktop`` (pointer and keyboard
events) and ``org.gnome.Mutter.ScreenCast`` (a PipeWire stream of the primary
monitor). The two sessions are linked, so absolute pointer coordinates are in
the stream frame's pixel space.

All D-Bus traffic runs on one private asyncio loop in a daemon thread; the
public methods are synchronous and thread-safe because the runner calls them
from ``asyncio.to_thread`` workers. Nothing connects until the first call.

Mutter ends both sessions when the screen locks, the monitor layout changes or
the shell restarts, and when this process's bus connection closes. The bus is
kept for the process lifetime, so a session this module gives up on (a failed
start, or one rebuilt after an error) is stopped explicitly; otherwise its
virtual input device and stream would stay alive. A closed session is rebuilt
once on the next call.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Final, TypeVar

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from controller_client.exceptions import DesktopSessionError

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Linux evdev button codes, as NotifyPointerButton expects them.
BTN_LEFT: Final = 0x110
BTN_RIGHT: Final = 0x111
BTN_MIDDLE: Final = 0x112

_DBUS_NAME: Final = "org.freedesktop.DBus"
_DBUS_PATH: Final = "/org/freedesktop/DBus"
_PROPERTIES_IFACE: Final = "org.freedesktop.DBus.Properties"

_RD_NAME: Final = "org.gnome.Mutter.RemoteDesktop"
_RD_PATH: Final = "/org/gnome/Mutter/RemoteDesktop"
_RD_IFACE: Final = "org.gnome.Mutter.RemoteDesktop"
_RD_SESSION_IFACE: Final = "org.gnome.Mutter.RemoteDesktop.Session"

_SC_NAME: Final = "org.gnome.Mutter.ScreenCast"
_SC_PATH: Final = "/org/gnome/Mutter/ScreenCast"
_SC_IFACE: Final = "org.gnome.Mutter.ScreenCast"
_SC_SESSION_IFACE: Final = "org.gnome.Mutter.ScreenCast.Session"
_SC_STREAM_IFACE: Final = "org.gnome.Mutter.ScreenCast.Stream"

# ScreenCast "cursor-mode": 0 hides the cursor, matching the screenshots the
# pyautogui backend takes on Windows and macOS.
_CURSOR_MODE_HIDDEN: Final = 0

# Every D-Bus call is bounded on its own; the outer budgets only back these up
# and are larger, so the specific message is the one that surfaces.
_CALL_TIMEOUT_S: Final = 5.0
_NODE_TIMEOUT_S: Final = 5.0
_OUTER_MARGIN_S: Final = 1.0
# The bus connect, two CreateSession, Get, RecordMonitor, three AddMatch and
# Start.
_START_CALLS: Final = 9
# Stop and three RemoveMatch: for the old session before a rebuild, and for
# the new one when its start fails.
_DROP_CALLS: Final = 4
_START_TIMEOUT_S: Final = (
    (_START_CALLS + _DROP_CALLS) * _CALL_TIMEOUT_S + _NODE_TIMEOUT_S + _OUTER_MARGIN_S
)
# Connecting to the bus and two NameHasOwner.
_LOOKUP_TIMEOUT_S: Final = 3 * _CALL_TIMEOUT_S + _OUTER_MARGIN_S

_NO_SESSION_BUS: Final = (
    "Could not reach the desktop session's D-Bus. Start the Test Runner from a "
    "terminal inside the logged-in GNOME desktop session, not over SSH or as a "
    "system service."
)
_NOT_GNOME: Final = (
    "This is not a GNOME Wayland session (GNOME's remote desktop service "
    f"{_RD_NAME} is missing). The Test Runner on Linux needs Ubuntu's default "
    "GNOME desktop on Wayland."
)
_LOCKED: Final = (
    "The screen is locked or the login screen is showing; GNOME stops remote "
    "control there. Unlock it, and disable the lock screen and automatic screen "
    "blanking on test machines."
)
_MISSING_NAME_ERRORS: Final = frozenset(
    {
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
    }
)


class _CallTimeoutError(DesktopSessionError):
    """GNOME did not answer in time; the session itself may still be fine."""


@dataclass
class _StartHandoff:
    """Who keeps a session whose start finishes as the caller gives up.

    Decided under a lock shared by the caller and the loop thread, so the
    session is either handed to the caller or stopped, never dropped.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    abandoned: bool = False
    delivered: bool = False


@dataclass(frozen=True)
class StreamInfo:
    node_id: int
    stream_path: str


@dataclass
class _Session:
    """One linked RemoteDesktop + ScreenCast session on one bus connection."""

    bus: MessageBus
    rd_path: str
    sc_path: str
    stream_path: str
    # Unique bus names that answered CreateSession; only they may close us.
    senders: frozenset[str]
    node_future: asyncio.Future[int]
    node_id: int = 0
    closed: bool = False
    # Mutter sent Closed, so there is nothing left to stop.
    ended_by_mutter: bool = False
    match_rules: list[str] = field(default_factory=list)
    handler: Callable[[Message], None] | None = None

    @property
    def alive(self) -> bool:
        return not self.closed and self.bus.connected


def _session_bus_address() -> str:
    """The session bus address, also when only ``XDG_RUNTIME_DIR`` is set.

    dbus-fast reads ``DBUS_SESSION_BUS_ADDRESS`` and otherwise falls back to
    X11 autolaunch files, which do not exist on Wayland. systemd user sessions
    always put the bus at ``$XDG_RUNTIME_DIR/bus``.
    """
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if address:
        return address
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime_dir:
        socket_path = os.path.join(runtime_dir, "bus")
        if os.path.exists(socket_path):
            return f"unix:path={socket_path}"
    raise DesktopSessionError(
        "Neither DBUS_SESSION_BUS_ADDRESS nor XDG_RUNTIME_DIR/bus is available. "
        + _NO_SESSION_BUS
    )


def _error_from_reply(reply: Message, what: str) -> DesktopSessionError:
    name = reply.error_name if isinstance(reply.error_name, str) else "unknown"
    body = reply.body
    text = body[0] if body and isinstance(body[0], str) else ""
    if "inhibited" in text.lower():
        return DesktopSessionError(_LOCKED)
    if name in _MISSING_NAME_ERRORS:
        return DesktopSessionError(_NOT_GNOME)
    return DesktopSessionError(f"GNOME refused {what}: {name}: {text}")


def _match_rule(sender: str, path: str, interface: str, member: str) -> str:
    return (
        f"type='signal',sender='{sender}',path='{path}',"
        f"interface='{interface}',member='{member}'"
    )


class GnomeRemoteSession:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bus: MessageBus | None = None
        self._session: _Session | None = None
        # Strong references to the session cleanup tasks on the loop.
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    # -- public, synchronous API ------------------------------------------

    def ensure_started(self) -> StreamInfo:
        with self._lock:
            session = self._ensure_session()
            return StreamInfo(node_id=session.node_id, stream_path=session.stream_path)

    def pointer_move(
        self, x: float, y: float, *, stream_path: str | None = None
    ) -> None:
        """Move the pointer; with ``stream_path``, only on that session."""
        with self._lock:
            session = self._session_for(stream_path)
            self._notify(
                session,
                "NotifyPointerMotionAbsolute",
                "sdd",
                [session.stream_path, float(x), float(y)],
                "the pointer move",
            )

    def pointer_button(
        self, button_code: int, pressed: bool, *, stream_path: str | None = None
    ) -> None:
        with self._lock:
            session = self._session_for(stream_path)
            self._notify(
                session,
                "NotifyPointerButton",
                "ib",
                [button_code, pressed],
                "the mouse button event",
            )

    def key(
        self, keysym: int, pressed: bool, *, stream_path: str | None = None
    ) -> None:
        with self._lock:
            session = self._session_for(stream_path)
            self._notify(
                session,
                "NotifyKeyboardKeysym",
                "ub",
                [keysym, pressed],
                "the key event",
            )

    def active_stream_path(self) -> str | None:
        """The live session's stream path, without starting or rebuilding one."""
        with self._lock:
            session = self._session
            if session is None or not session.alive:
                return None
            return session.stream_path

    def mutter_services_present(self) -> bool:
        """Whether both mutter services own their names on the session bus."""
        with self._lock:
            return self._run(
                self._services_present(), _LOOKUP_TIMEOUT_S, "the service lookup"
            )

    def probe(self) -> str:
        """Start the session for diagnose and describe the result."""
        with self._lock:
            if not self._run(
                self._services_present(), _LOOKUP_TIMEOUT_S, "the service lookup"
            ):
                raise DesktopSessionError(_NOT_GNOME)
            session = self._ensure_session()
            return (
                "RemoteDesktop session started, primary monitor stream node "
                f"{session.node_id}"
            )

    # -- sync helpers (called with the lock held) ----------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="gnome-remote-session", daemon=True
            )
            thread.start()
            self._loop = loop
        return self._loop

    def _run(
        self, coro: Coroutine[object, object, _T], timeout: float, what: str
    ) -> _T:
        future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise _timeout_error(what, timeout) from None

    def _ensure_session(self) -> _Session:
        session = self._session
        if session is not None and session.alive:
            return session
        if session is not None:
            if not session.bus.connected:
                logger.warning("The session bus connection was lost")
            logger.info("Rebuilding the GNOME remote desktop session")
        self._session = None
        budget = _START_TIMEOUT_S
        if session is not None:
            budget += _DROP_CALLS * _CALL_TIMEOUT_S
        handoff = _StartHandoff()
        future = asyncio.run_coroutine_threadsafe(
            self._start_for(session, handoff), self._ensure_loop()
        )
        try:
            new_session = future.result(timeout=budget)
        except concurrent.futures.TimeoutError:
            with handoff.lock:
                handoff.abandoned = True
                delivered = handoff.delivered
            if not delivered:
                # The loop side stops whatever it had created.
                future.cancel()
                raise _timeout_error("the remote desktop start", budget) from None
            # It started just as we gave up: use it.
            new_session = future.result(timeout=_CALL_TIMEOUT_S)
        self._session = new_session
        return new_session

    def _session_for(self, stream_path: str | None) -> _Session:
        """The session to send an event on.

        An action that spans several events (a drag, a hotkey) passes the
        stream it started on: if that session closed meanwhile, a rebuilt one
        has a fresh virtual device without the held button or keys, so the
        action fails instead of finishing on it.
        """
        if stream_path is None:
            return self._ensure_session()
        session = self._session
        if session is None or not session.alive or session.stream_path != stream_path:
            raise DesktopSessionError(
                "GNOME closed the remote control session in the middle of this "
                "action (screen locked, monitor change or shell restart), so it "
                "did not finish. Take a new screenshot and retry. " + _LOCKED
            )
        return session

    def _notify(
        self,
        session: _Session,
        member: str,
        signature: str,
        body: list[object],
        what: str,
    ) -> None:
        try:
            self._run(
                self._call(
                    session.bus,
                    _RD_NAME,
                    session.rd_path,
                    _RD_SESSION_IFACE,
                    member,
                    signature,
                    body,
                    what,
                ),
                _CALL_TIMEOUT_S + _OUTER_MARGIN_S,
                what,
            )
        except _CallTimeoutError:
            # A slow answer does not mean the session is gone; keep it so a
            # following release still reaches the device that pressed.
            raise
        except DesktopSessionError:
            # The session may be gone without a Closed signal reaching us
            # (mutter restarted, "Session not started", "Unknown stream").
            # Rebuild on the next call instead of failing forever; the rebuild
            # stops this one.
            session.closed = True
            raise

    # -- loop-thread coroutines ------------------------------------------

    async def _get_bus(self) -> MessageBus:
        bus = self._bus
        if bus is not None and bus.connected:
            return bus
        address = _session_bus_address()
        bus = MessageBus(bus_address=address, bus_type=BusType.SESSION)
        try:
            await asyncio.wait_for(bus.connect(), _CALL_TIMEOUT_S)
        except TimeoutError:
            bus.disconnect()
            raise _timeout_error(
                "the session bus connection", _CALL_TIMEOUT_S
            ) from None
        except Exception as exc:
            bus.disconnect()
            raise DesktopSessionError(
                f"Could not connect to the session bus: {exc}. " + _NO_SESSION_BUS
            ) from exc
        self._bus = bus
        return bus

    async def _call_reply(
        self,
        bus: MessageBus,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str,
        body: list[object],
        what: str,
    ) -> Message:
        message = Message(
            destination=destination,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body,
        )
        try:
            reply = await asyncio.wait_for(bus.call(message), _CALL_TIMEOUT_S)
        except TimeoutError:
            raise _timeout_error(what, _CALL_TIMEOUT_S) from None
        except Exception as exc:
            raise DesktopSessionError(
                f"Lost the session bus connection during {what}: {exc}"
            ) from exc
        if reply.message_type == MessageType.ERROR:
            raise _error_from_reply(reply, what)
        return reply

    async def _call(
        self,
        bus: MessageBus,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str,
        body: list[object],
        what: str,
    ) -> list[object]:
        reply = await self._call_reply(
            bus, destination, path, interface, member, signature, body, what
        )
        return list(reply.body)

    async def _add_match(self, bus: MessageBus, rule: str) -> None:
        await self._call(
            bus, _DBUS_NAME, _DBUS_PATH, _DBUS_NAME, "AddMatch", "s", [rule], "AddMatch"
        )

    async def _services_present(self) -> bool:
        bus = await self._get_bus()
        for name in (_RD_NAME, _SC_NAME):
            body = await self._call(
                bus,
                _DBUS_NAME,
                _DBUS_PATH,
                _DBUS_NAME,
                "NameHasOwner",
                "s",
                [name],
                f"the lookup of {name}",
            )
            if not (body and isinstance(body[0], bool) and body[0]):
                return False
        return True

    async def _drop_session(self, session: _Session) -> None:
        """Stop a session unless mutter ended it, and forget its handler and rules.

        Best effort: a failure here must not hide the error that got us here.
        """
        if session.handler is not None:
            session.bus.remove_message_handler(session.handler)
            session.handler = None
        if not session.bus.connected:
            return
        if not session.ended_by_mutter:
            try:
                # Stopping the RemoteDesktop session also stops the linked
                # ScreenCast session and releases its virtual input device.
                await self._call(
                    session.bus,
                    _RD_NAME,
                    session.rd_path,
                    _RD_SESSION_IFACE,
                    "Stop",
                    "",
                    [],
                    "the remote desktop stop",
                )
            except DesktopSessionError as exc:
                logger.debug("Could not stop session %s: %s", session.rd_path, exc)
        for rule in session.match_rules:
            try:
                await self._call(
                    session.bus,
                    _DBUS_NAME,
                    _DBUS_PATH,
                    _DBUS_NAME,
                    "RemoveMatch",
                    "s",
                    [rule],
                    "RemoveMatch",
                )
            except DesktopSessionError:
                logger.debug("Could not remove match rule %s", rule)
        session.match_rules.clear()

    async def _shielded_drop(self, session: _Session) -> None:
        """Drop a session even if the awaiting coroutine is cancelled.

        The task is kept referenced: a shield alone does not keep the inner
        task alive once the outer one is cancelled.
        """
        task = asyncio.get_running_loop().create_task(self._drop_session(session))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        await asyncio.shield(task)

    async def _start_for(
        self, old: _Session | None, handoff: _StartHandoff
    ) -> _Session:
        session = await self._start_session(old)
        with handoff.lock:
            if not handoff.abandoned:
                handoff.delivered = True
                return session
        session.closed = True
        await self._shielded_drop(session)
        raise DesktopSessionError("The remote desktop start was abandoned.")

    async def _start_session(self, old: _Session | None) -> _Session:
        if old is not None:
            # Shielded like a failed start: a cancelled rebuild still stops
            # the old session.
            await self._shielded_drop(old)
        bus = await self._get_bus()

        rd_reply = await self._call_reply(
            bus,
            _RD_NAME,
            _RD_PATH,
            _RD_IFACE,
            "CreateSession",
            "",
            [],
            "the remote desktop session",
        )
        rd_path = _single_str(rd_reply.body, "RemoteDesktop.CreateSession")
        session = _Session(
            bus=bus,
            rd_path=rd_path,
            sc_path="",
            stream_path="",
            senders=frozenset(),
            node_future=asyncio.get_running_loop().create_future(),
        )
        try:
            await self._start_created(session, rd_reply)
        except BaseException:
            session.closed = True
            # Shielded: a timeout in the caller cancels this coroutine, and
            # the created session must still be stopped.
            await self._shielded_drop(session)
            raise
        logger.info(
            "GNOME remote desktop session started: stream node %d, stream %s",
            session.node_id,
            session.stream_path,
        )
        return session

    async def _start_created(self, session: _Session, rd_reply: Message) -> None:
        """Link the screen cast, subscribe to signals and start the session."""
        bus = session.bus
        rd_path = session.rd_path
        prop = await self._call(
            bus,
            _RD_NAME,
            rd_path,
            _PROPERTIES_IFACE,
            "Get",
            "ss",
            [_RD_SESSION_IFACE, "SessionId"],
            "the remote desktop session id",
        )
        variant = prop[0] if prop else None
        if not (isinstance(variant, Variant) and isinstance(variant.value, str)):
            raise DesktopSessionError(
                "GNOME returned no SessionId for the remote desktop session."
            )
        session_id = variant.value

        sc_reply = await self._call_reply(
            bus,
            _SC_NAME,
            _SC_PATH,
            _SC_IFACE,
            "CreateSession",
            "a{sv}",
            [{"remote-desktop-session-id": Variant("s", session_id)}],
            "the screen cast session",
        )
        sc_path = _single_str(sc_reply.body, "ScreenCast.CreateSession")

        stream_body = await self._call(
            bus,
            _SC_NAME,
            sc_path,
            _SC_SESSION_IFACE,
            "RecordMonitor",
            "sa{sv}",
            ["", {"cursor-mode": Variant("u", _CURSOR_MODE_HIDDEN)}],
            "the primary monitor recording",
        )
        stream_path = _single_str(stream_body, "ScreenCast.Session.RecordMonitor")

        session.sc_path = sc_path
        session.stream_path = stream_path
        session.senders = frozenset(
            sender
            for sender in (rd_reply.sender, sc_reply.sender)
            if isinstance(sender, str)
        )

        def on_message(message: Message) -> None:
            _handle_signal(session, message)

        # Handler and match rules go in before Start: PipeWireStreamAdded is
        # emitted while Start runs.
        bus.add_message_handler(on_message)
        session.handler = on_message
        rules = [
            _match_rule(_SC_NAME, stream_path, _SC_STREAM_IFACE, "PipeWireStreamAdded"),
            _match_rule(_RD_NAME, rd_path, _RD_SESSION_IFACE, "Closed"),
            _match_rule(_SC_NAME, sc_path, _SC_SESSION_IFACE, "Closed"),
        ]
        for rule in rules:
            await self._add_match(bus, rule)
            session.match_rules.append(rule)

        await self._call(
            bus,
            _RD_NAME,
            rd_path,
            _RD_SESSION_IFACE,
            "Start",
            "",
            [],
            "the remote desktop start",
        )
        try:
            session.node_id = await asyncio.wait_for(
                session.node_future, _NODE_TIMEOUT_S
            )
        except TimeoutError:
            raise DesktopSessionError(
                "GNOME started remote control but did not publish the screen "
                f"stream within {_NODE_TIMEOUT_S:g} s. Check that PipeWire is "
                "running in the desktop session (systemctl --user status "
                "pipewire)."
            ) from None


def _timeout_error(what: str, timeout: float) -> _CallTimeoutError:
    return _CallTimeoutError(
        f"GNOME did not answer {what} within {timeout:g} s. The desktop session "
        "may be frozen or the screen locked; check the test machine's screen."
    )


def _single_str(body: list[object], what: str) -> str:
    value = body[0] if body else None
    if not isinstance(value, str) or not value:
        raise DesktopSessionError(f"GNOME returned an unexpected reply to {what}.")
    return value


def _handle_signal(session: _Session, message: Message) -> None:
    """Route mutter's signals for one session; runs on the loop thread."""
    if message.message_type != MessageType.SIGNAL:
        return
    if session.senders and message.sender not in session.senders:
        return
    member = message.member
    path = message.path
    if (
        member == "PipeWireStreamAdded"
        and path == session.stream_path
        and message.interface == _SC_STREAM_IFACE
    ):
        body = message.body
        node_id = body[0] if body else None
        if (
            isinstance(node_id, int)
            and not isinstance(node_id, bool)
            and not session.node_future.done()
        ):
            session.node_future.set_result(node_id)
        return
    if member == "Closed" and (
        (path == session.rd_path and message.interface == _RD_SESSION_IFACE)
        or (path == session.sc_path and message.interface == _SC_SESSION_IFACE)
    ):
        if not session.closed:
            logger.warning(
                "GNOME closed the remote desktop session %s (screen locked, "
                "monitor change or shell restart); it is rebuilt on the next "
                "action",
                path,
            )
        session.closed = True
        session.ended_by_mutter = True
        if not session.node_future.done():
            session.node_future.set_exception(
                DesktopSessionError(
                    "GNOME closed the remote desktop session while it was "
                    "starting. " + _LOCKED
                )
            )
