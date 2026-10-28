"""JSON-lines messages between the Test Runner and its person-step window.

The window runs as its own process (Tk must own a main thread, which on macOS
the runner's asyncio loop already holds). The runner writes commands to the
window's stdin and reads events from its stdout, one UTF-8 JSON object per
line. The window never talks to the server; every answer goes back through the
runner, which re-validates it.

Stdlib only (plus the shared protocol), so the window process starts fast.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from controller_client.exceptions import ProtocolError
from controller_client.protocol import (
    PERSON_STEP_ACTION_MAX_BYTES,
    PERSON_STEP_RESULT_MESSAGE_MAX_CHARS,
    PersonStepActionPayload,
    PersonStepStatePayload,
    parse_person_step_action_payload,
    parse_person_step_state_payload,
)

# Largest runner -> window line. A full snapshot (5 steps at the protocol's
# display caps, every character escaped) stays well below it.
IPC_TO_WINDOW_MAX_BYTES: Final[int] = 2 * 1024 * 1024
# Largest window -> runner line: one answer (note + output) at most.
IPC_TO_RUNNER_MAX_BYTES: Final[int] = PERSON_STEP_ACTION_MAX_BYTES

_MAX_SEQ: Final[int] = 2**31 - 1


class WindowIpcType(StrEnum):
    # runner -> window
    STATE = "state"
    SHOW = "show"
    HIDE = "hide"
    RESULT = "result"
    CONNECTION = "connection"
    QUIT = "quit"
    # window -> runner
    READY = "ready"
    HIDDEN = "hidden"
    ACTION = "action"
    CLOSED_BY_USER = "closed_by_user"


# --- runner -> window -------------------------------------------------------


@dataclass(frozen=True)
class StateCommand:
    """Replace the window's steps (newest first); empty hides the window."""

    steps: tuple[PersonStepStatePayload, ...]


@dataclass(frozen=True)
class ShowCommand:
    """Make the window visible (it raises itself once per new step round)."""


@dataclass(frozen=True)
class HideCommand:
    """Withdraw the window now and answer ``HiddenEvent(seq)``."""

    seq: int


@dataclass(frozen=True)
class ResultCommand:
    """The server's answer to the person's last action."""

    ok: bool
    message: str


@dataclass(frozen=True)
class ConnectionCommand:
    connected: bool


@dataclass(frozen=True)
class QuitCommand:
    pass


WindowCommand = (
    StateCommand
    | ShowCommand
    | HideCommand
    | ResultCommand
    | ConnectionCommand
    | QuitCommand
)


# --- window -> runner -------------------------------------------------------


@dataclass(frozen=True)
class ReadyEvent:
    """Tk started; the window exists and is withdrawn."""


@dataclass(frozen=True)
class HiddenEvent:
    seq: int


@dataclass(frozen=True)
class ActionEvent:
    """The person answered a step; the window has already hidden itself."""

    action: PersonStepActionPayload


@dataclass(frozen=True)
class ClosedByUserEvent:
    """The person closed the window; it stays hidden until a new step."""


WindowEvent = ReadyEvent | HiddenEvent | ActionEvent | ClosedByUserEvent


# --- encoding ---------------------------------------------------------------


def _encode(payload: dict[str, object], max_bytes: int) -> bytes:
    # ensure_ascii=False keeps non-ASCII text at its UTF-8 size; lone
    # surrogates (which UTF-8 cannot carry) become "?".
    line = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8", errors="replace"
        )
        + b"\n"
    )
    if len(line) > max_bytes:
        raise ProtocolError(
            f"Window message '{payload.get('type')}' is {len(line)} bytes "
            f"(limit {max_bytes})"
        )
    return line


def _decode(line: bytes, max_bytes: int) -> tuple[WindowIpcType, dict[str, object]]:
    if len(line) > max_bytes:
        raise ProtocolError(f"Window message longer than {max_bytes} bytes")
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError(f"Invalid window message: {e}") from e
    if not isinstance(data, dict):
        raise ProtocolError("Invalid window message: expected an object")
    payload: dict[str, object] = data
    raw_type = payload.get("type")
    if not isinstance(raw_type, str):
        raise ProtocolError("Invalid window message: missing 'type'")
    try:
        return WindowIpcType(raw_type), payload
    except ValueError as e:
        raise ProtocolError(f"Unknown window message type: {raw_type[:40]!r}") from e


def _bool(data: dict[str, object], field: str) -> bool:
    value = data.get(field)
    if not isinstance(value, bool):
        raise ProtocolError(f"Invalid window message: '{field}' must be a bool")
    return value


def _seq(data: dict[str, object]) -> int:
    value = data.get("seq")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= _MAX_SEQ
    ):
        raise ProtocolError("Invalid window message: bad 'seq'")
    return value


def next_seq(seq: int) -> int:
    """The hide sequence number after ``seq`` (wraps within the valid range)."""
    return 0 if seq >= _MAX_SEQ else seq + 1


def encode_command(command: WindowCommand) -> bytes:
    payload: dict[str, object]
    match command:
        case StateCommand(steps=steps):
            payload = {
                "type": WindowIpcType.STATE.value,
                "steps": [step.to_wire() for step in steps],
            }
        case ShowCommand():
            payload = {"type": WindowIpcType.SHOW.value}
        case HideCommand(seq=seq):
            payload = {"type": WindowIpcType.HIDE.value, "seq": seq}
        case ResultCommand(ok=ok, message=message):
            payload = {
                "type": WindowIpcType.RESULT.value,
                "ok": ok,
                "message": message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
            }
        case ConnectionCommand(connected=connected):
            payload = {"type": WindowIpcType.CONNECTION.value, "connected": connected}
        case QuitCommand():
            payload = {"type": WindowIpcType.QUIT.value}
    return _encode(payload, IPC_TO_WINDOW_MAX_BYTES)


def decode_command(line: bytes) -> WindowCommand:
    """Parse one runner -> window line (window side); ProtocolError if invalid."""
    kind, data = _decode(line, IPC_TO_WINDOW_MAX_BYTES)
    match kind:
        case WindowIpcType.STATE:
            return StateCommand(steps=parse_person_step_state_payload(data).steps)
        case WindowIpcType.SHOW:
            return ShowCommand()
        case WindowIpcType.HIDE:
            return HideCommand(seq=_seq(data))
        case WindowIpcType.RESULT:
            message = data.get("message", "")
            if not isinstance(message, str):
                raise ProtocolError("Invalid window message: 'message' not a string")
            return ResultCommand(
                ok=_bool(data, "ok"),
                message=message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
            )
        case WindowIpcType.CONNECTION:
            return ConnectionCommand(connected=_bool(data, "connected"))
        case WindowIpcType.QUIT:
            return QuitCommand()
    raise ProtocolError(f"Not a window command: {kind.value}")


def encode_event(event: WindowEvent) -> bytes:
    payload: dict[str, object]
    match event:
        case ReadyEvent():
            payload = {"type": WindowIpcType.READY.value}
        case HiddenEvent(seq=seq):
            payload = {"type": WindowIpcType.HIDDEN.value, "seq": seq}
        case ActionEvent(action=action):
            payload = {
                "type": WindowIpcType.ACTION.value,
                "action": action.action.value,
                "person_step_id": action.person_step_id,
                "round": action.round,
                "outcome": (action.outcome.value if action.outcome is not None else ""),
                "note": action.note,
                "output": action.output,
            }
        case ClosedByUserEvent():
            payload = {"type": WindowIpcType.CLOSED_BY_USER.value}
    return _encode(payload, IPC_TO_RUNNER_MAX_BYTES)


def decode_event(line: bytes) -> WindowEvent:
    """Parse one window -> runner line (runner side); ProtocolError if invalid.

    An action is validated with the shared protocol parser (caps, outcome,
    note required for "different"), exactly as the server will.
    """
    kind, data = _decode(line, IPC_TO_RUNNER_MAX_BYTES)
    match kind:
        case WindowIpcType.READY:
            return ReadyEvent()
        case WindowIpcType.HIDDEN:
            return HiddenEvent(seq=_seq(data))
        case WindowIpcType.ACTION:
            return ActionEvent(action=parse_person_step_action_payload(data))
        case WindowIpcType.CLOSED_BY_USER:
            return ClosedByUserEvent()
    raise ProtocolError(f"Not a window event: {kind.value}")
