"""macOS: the Pass / Fail shortcuts as Carbon hot keys (window process).

``RegisterEventHotKey`` registers ⌘⇧P / ⌘⇧F system-wide and swallows them; it
needs no Accessibility or Input Monitoring permission. The handler is
installed on the application event target, so Carbon calls it on the main
thread from the Cocoa run loop that Tk pumps (``mainloop`` / ``update``):
every call here must come from the Tk main thread. The handler only puts a
``HotkeyKind`` on the queue.

Key codes are physical ANSI positions (the keys labelled P and F on a US
layout), not characters, so the combination stays on the same keys whatever
the input source.
"""

import ctypes
import logging
import sys
import threading
from typing import Final

from controller_client.manual_hotkeys_base import (
    OFF_STATUS,
    HotkeyKind,
    HotkeyQueue,
    HotkeyState,
    HotkeyStatus,
    default_triggers,
    focus_only,
)

logger = logging.getLogger(__name__)

_CARBON_PATH: Final[str] = "/System/Library/Frameworks/Carbon.framework/Carbon"


def _four_char_code(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


# Events.h: kVK_ANSI_P / kVK_ANSI_F, cmdKey | shiftKey.
_KEY_CODES: Final[dict[HotkeyKind, int]] = {
    HotkeyKind.PASS: 0x23,
    HotkeyKind.FAIL: 0x03,
}
_MODIFIERS: Final[int] = (1 << 8) | (1 << 9)
# EventHotKeyID: our signature plus one id per kind.
_SIGNATURE: Final[int] = _four_char_code("PHmr")
_HOTKEY_IDS: Final[dict[HotkeyKind, int]] = {HotkeyKind.PASS: 1, HotkeyKind.FAIL: 2}
_KINDS_BY_ID: Final[dict[int, HotkeyKind]] = {v: k for k, v in _HOTKEY_IDS.items()}

_K_EVENT_CLASS_KEYBOARD: Final[int] = _four_char_code("keyb")
_K_EVENT_HOT_KEY_PRESSED: Final[int] = 5
_K_EVENT_PARAM_DIRECT_OBJECT: Final[int] = _four_char_code("----")
_TYPE_EVENT_HOT_KEY_ID: Final[int] = _four_char_code("hkid")
_NO_ERR: Final[int] = 0
_EVENT_NOT_HANDLED_ERR: Final[int] = -9874
_EVENT_HOT_KEY_EXISTS_ERR: Final[int] = -9878

_UNAVAILABLE_REASON: Final[str] = "System-wide shortcuts are not available on this Mac"


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


# OSStatus (*EventHandlerProcPtr)(EventHandlerCallRef, EventRef, void *).
_EventHandlerProc = ctypes.CFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)

# Every installed handler's callback (by its id), until RemoveEventHandler
# succeeded: Carbon holds a raw pointer to it, so it must outlive its owner.
_LIVE_CALLBACKS: "dict[int, ctypes._CFunctionType]" = {}


def _load_carbon() -> ctypes.CDLL | None:
    """Carbon with the prototypes used here; None when it cannot be loaded."""
    if sys.platform != "darwin":
        return None
    try:
        carbon = ctypes.CDLL(_CARBON_PATH)
        carbon.GetApplicationEventTarget.argtypes = []
        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        carbon.InstallEventHandler.argtypes = [
            ctypes.c_void_p,  # EventTargetRef
            _EventHandlerProc,  # EventHandlerUPP
            ctypes.c_size_t,  # ItemCount
            ctypes.POINTER(_EventTypeSpec),
            ctypes.c_void_p,  # userData
            ctypes.POINTER(ctypes.c_void_p),  # EventHandlerRef *
        ]
        carbon.InstallEventHandler.restype = ctypes.c_int32
        carbon.RemoveEventHandler.argtypes = [ctypes.c_void_p]
        carbon.RemoveEventHandler.restype = ctypes.c_int32
        carbon.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32,  # key code
            ctypes.c_uint32,  # modifiers
            _EventHotKeyID,  # passed by value
            ctypes.c_void_p,  # EventTargetRef
            ctypes.c_uint32,  # OptionBits
            ctypes.POINTER(ctypes.c_void_p),  # EventHotKeyRef *
        ]
        carbon.RegisterEventHotKey.restype = ctypes.c_int32
        carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        carbon.UnregisterEventHotKey.restype = ctypes.c_int32
        carbon.GetEventParameter.argtypes = [
            ctypes.c_void_p,  # EventRef
            ctypes.c_uint32,  # EventParamName
            ctypes.c_uint32,  # EventParamType (desired)
            ctypes.c_void_p,  # EventParamType * (actual), may be NULL
            ctypes.c_size_t,  # buffer size
            ctypes.c_void_p,  # ByteCount * (actual), may be NULL
            ctypes.c_void_p,  # buffer
        ]
        carbon.GetEventParameter.restype = ctypes.c_int32
    except (OSError, AttributeError) as e:
        logger.warning("Cannot load Carbon for the manual shortcuts: %s", e)
        return None
    return carbon


class MacHotkeys:
    """⌘⇧P / ⌘⇧F through Carbon hot keys; see ``HotkeyBackend``.

    ``dry_run`` (``--probe-shortcuts``): ``enable`` registers and releases
    right away and reports the result.
    """

    def __init__(self, events: HotkeyQueue, *, dry_run: bool) -> None:
        self._events = events
        self._dry_run = dry_run
        self._carbon = _load_carbon()
        self._callback = _EventHandlerProc(self._on_event)
        self._handler_ref: int | None = None
        self._hotkey_refs: dict[HotkeyKind, int] = {}
        self._closed = False
        self._status: HotkeyStatus = (
            OFF_STATUS if self._carbon is not None else focus_only(_UNAVAILABLE_REASON)
        )

    @property
    def status(self) -> HotkeyStatus:
        return self._status

    def enable(self) -> None:
        carbon = self._carbon
        if carbon is None or self._closed or self._hotkey_refs:
            return
        if threading.current_thread() is not threading.main_thread():
            # Carbon's event manager traps off the main thread.
            logger.warning("Manual shortcuts enabled off the main thread; ignored")
            self._status = focus_only(_UNAVAILABLE_REASON)
            return
        try:
            self._status = self._register(carbon)
        except (OSError, AttributeError, TypeError, ctypes.ArgumentError) as e:
            logger.warning("Cannot register the manual shortcuts: %s", e)
            self._release(carbon)
            self._status = focus_only(_UNAVAILABLE_REASON)
            return
        if self._dry_run:
            self._release(carbon)

    def disable(self) -> None:
        carbon = self._carbon
        if carbon is None:
            return
        self._release(carbon)
        if self._status.state is HotkeyState.ACTIVE and not self._dry_run:
            self._status = OFF_STATUS

    def close(self) -> None:
        carbon = self._carbon
        if carbon is None or self._closed:
            return
        self._closed = True
        self._release(carbon)
        handler_ref, self._handler_ref = self._handler_ref, None
        if handler_ref is not None:
            try:
                err = carbon.RemoveEventHandler(handler_ref)
            except (OSError, ctypes.ArgumentError) as e:
                logger.warning("Cannot remove the manual shortcut handler: %s", e)
            else:
                if err == _NO_ERR:
                    _LIVE_CALLBACKS.pop(id(self._callback), None)
                else:
                    logger.warning("RemoveEventHandler failed: %d", err)
        if self._status.state is HotkeyState.ACTIVE:
            self._status = OFF_STATUS

    def _register(self, carbon: ctypes.CDLL) -> HotkeyStatus:
        """Install the handler once, then register both combinations.

        All or nothing: when one combination cannot be registered, both are
        released and the window's own key bindings take over.
        """
        target = carbon.GetApplicationEventTarget()
        if self._handler_ref is None:
            spec = _EventTypeSpec(_K_EVENT_CLASS_KEYBOARD, _K_EVENT_HOT_KEY_PRESSED)
            handler_ref = ctypes.c_void_p()
            _LIVE_CALLBACKS[id(self._callback)] = self._callback
            err = carbon.InstallEventHandler(
                target,
                self._callback,
                1,
                ctypes.byref(spec),
                None,
                ctypes.byref(handler_ref),
            )
            if err != _NO_ERR or not handler_ref.value:
                _LIVE_CALLBACKS.pop(id(self._callback), None)
                logger.warning("InstallEventHandler failed: %d", err)
                return focus_only(_UNAVAILABLE_REASON)
            self._handler_ref = handler_ref.value
        triggers = default_triggers()
        for kind, key_code in _KEY_CODES.items():
            hotkey_ref = ctypes.c_void_p()
            err = carbon.RegisterEventHotKey(
                key_code,
                _MODIFIERS,
                _EventHotKeyID(_SIGNATURE, _HOTKEY_IDS[kind]),
                target,
                0,
                ctypes.byref(hotkey_ref),
            )
            if err != _NO_ERR or not hotkey_ref.value:
                self._release(carbon)
                if err == _EVENT_HOT_KEY_EXISTS_ERR:
                    return focus_only(f"{triggers[kind]} is used by another app")
                logger.warning("RegisterEventHotKey(%s) failed: %d", kind, err)
                return focus_only(_UNAVAILABLE_REASON)
            self._hotkey_refs[kind] = hotkey_ref.value
        return HotkeyStatus(state=HotkeyState.ACTIVE)

    def _release(self, carbon: ctypes.CDLL) -> None:
        refs, self._hotkey_refs = self._hotkey_refs, {}
        for kind, hotkey_ref in refs.items():
            try:
                err = carbon.UnregisterEventHotKey(hotkey_ref)
            except (OSError, ctypes.ArgumentError) as e:
                logger.warning("Cannot release %s: %s", kind, e)
                continue
            if err != _NO_ERR:
                logger.warning("UnregisterEventHotKey(%s) failed: %d", kind, err)

    def _on_event(
        self,
        call_ref: int | None,
        event_ref: int | None,
        user_data: int | None,
    ) -> int:
        """Carbon handler: queue the pressed kind (main thread, never raises)."""
        carbon = self._carbon
        if carbon is None or event_ref is None:
            return _EVENT_NOT_HANDLED_ERR
        hotkey_id = _EventHotKeyID()
        try:
            err = carbon.GetEventParameter(
                event_ref,
                _K_EVENT_PARAM_DIRECT_OBJECT,
                _TYPE_EVENT_HOT_KEY_ID,
                None,
                ctypes.sizeof(hotkey_id),
                None,
                ctypes.byref(hotkey_id),
            )
        except (OSError, ctypes.ArgumentError) as e:
            logger.warning("Cannot read the pressed manual shortcut: %s", e)
            return _EVENT_NOT_HANDLED_ERR
        kind = _KINDS_BY_ID.get(hotkey_id.id)
        if err != _NO_ERR or hotkey_id.signature != _SIGNATURE or kind is None:
            return _EVENT_NOT_HANDLED_ERR
        self._events.put(kind)
        return _NO_ERR
