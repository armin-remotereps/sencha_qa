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
    PERSON_CASE_ACTION_MAX_BYTES,
    PERSON_STEP_ACTION_MAX_BYTES,
    PERSON_STEP_QUESTION_MAX_BYTES,
    PERSON_STEP_RESULT_MESSAGE_MAX_CHARS,
    CasePauseActionPayload,
    PersonCaseStatePayload,
    PersonChatSendPayload,
    PersonHelpActionPayload,
    PersonStepActionPayload,
    PersonStepQuestionAskPayload,
    PersonStepStatePayload,
    parse_case_pause_action_payload,
    parse_person_chat_send_payload,
    parse_person_help_action_payload,
    parse_person_step_action_payload,
    parse_person_step_question_payload,
    parse_person_step_state_payload,
)

# Largest runner -> window line. A full snapshot (5 steps at the protocol's
# display caps, case steps included, every character escaped: about 3.3 MB)
# stays below it; with ``cases`` only pathological escaping goes over, and
# the runner then sends the snapshot without them (person_window).
IPC_TO_WINDOW_MAX_BYTES: Final[int] = 4 * 1024 * 1024
# Largest window -> runner line: one answer (note + output), one question, or
# one pause, discussion message or help resolution.
IPC_TO_RUNNER_MAX_BYTES: Final[int] = max(
    PERSON_STEP_ACTION_MAX_BYTES,
    PERSON_STEP_QUESTION_MAX_BYTES,
    PERSON_CASE_ACTION_MAX_BYTES,
)

# The window's base text size in points (CONTROLLER_PERSON_WINDOW_TEXT_SIZE);
# A-/A+ change it for the session within the same range.
TEXT_SIZE_DEFAULT: Final[int] = 14
TEXT_SIZE_MIN: Final[int] = 10
TEXT_SIZE_MAX: Final[int] = 28


def clamp_text_size(size: int) -> int:
    return max(TEXT_SIZE_MIN, min(TEXT_SIZE_MAX, size))


_MAX_SEQ: Final[int] = 2**31 - 1


class WindowIpcType(StrEnum):
    # runner -> window
    STATE = "state"
    SHOW = "show"
    HIDE = "hide"
    RESULT = "result"
    QUESTION_RESULT = "question_result"
    CONNECTION = "connection"
    QUIT = "quit"
    # window -> runner
    READY = "ready"
    HIDDEN = "hidden"
    ACTION = "action"
    QUESTION = "question"
    CLOSED_BY_USER = "closed_by_user"
    # Person window v2 (cases, discussion, pause)
    CASE_RESULT = "case_result"  # runner -> window
    PAUSE = "pause"  # window -> runner
    CHAT_SEND = "chat_send"  # window -> runner
    HELP_ACTION = "help_action"  # window -> runner


class CaseRequestKind(StrEnum):
    """The window's case requests; at most one of each kind is pending."""

    PAUSE = "pause"
    CHAT = "chat"
    HELP = "help"


# --- runner -> window -------------------------------------------------------


@dataclass(frozen=True)
class StateCommand:
    """Replace the window's steps (newest first); empty hides the window."""

    steps: tuple[PersonStepStatePayload, ...]
    # The server takes questions (PERSON_STEP_ASSIST_V1): show the Ask AI box.
    questions_enabled: bool = False
    # PERSON_CASE_CONTEXT_V1: the cases behind the steps, an open help case and
    # the running case; empty from an older server (today's Step content only).
    cases: tuple[PersonCaseStatePayload, ...] = ()


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
class QuestionResultCommand:
    """The server's acknowledgement of the person's last question.

    The answer itself arrives later in a ``StateCommand`` (the step's
    ``questions``).
    """

    ok: bool
    message: str


@dataclass(frozen=True)
class CaseResultCommand:
    """The server's answer to the person's last pause, message or resolution."""

    kind: CaseRequestKind
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
    | QuestionResultCommand
    | CaseResultCommand
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
class QuestionEvent:
    """The person asked the AI about a step; the window stays visible."""

    question: PersonStepQuestionAskPayload


@dataclass(frozen=True)
class ClosedByUserEvent:
    """The person closed the window; it stays hidden until something new."""


@dataclass(frozen=True)
class PauseEvent:
    """The person paused a case; the window stays as it is."""

    pause: CasePauseActionPayload


@dataclass(frozen=True)
class ChatSendEvent:
    """The person sent a discussion message; the window stays as it is."""

    send: PersonChatSendPayload


@dataclass(frozen=True)
class HelpActionEvent:
    """The person resolved a help request; the window has already hidden."""

    action: PersonHelpActionPayload


WindowEvent = (
    ReadyEvent
    | HiddenEvent
    | ActionEvent
    | QuestionEvent
    | ClosedByUserEvent
    | PauseEvent
    | ChatSendEvent
    | HelpActionEvent
)


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


def _message(data: dict[str, object]) -> str:
    message = data.get("message", "")
    if not isinstance(message, str):
        raise ProtocolError("Invalid window message: 'message' not a string")
    return message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS]


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
        case StateCommand(
            steps=steps, questions_enabled=questions_enabled, cases=cases
        ):
            payload = {
                "type": WindowIpcType.STATE.value,
                "steps": [step.to_wire() for step in steps],
                "questions_enabled": questions_enabled,
                "cases": [case.to_wire() for case in cases],
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
        case QuestionResultCommand(ok=ok, message=message):
            payload = {
                "type": WindowIpcType.QUESTION_RESULT.value,
                "ok": ok,
                "message": message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
            }
        case CaseResultCommand(kind=kind, ok=ok, message=message):
            payload = {
                "type": WindowIpcType.CASE_RESULT.value,
                "kind": kind.value,
                "ok": ok,
                "message": message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
            }
        case ConnectionCommand(connected=connected):
            payload = {"type": WindowIpcType.CONNECTION.value, "connected": connected}
        case QuitCommand():
            payload = {"type": WindowIpcType.QUIT.value}
    return _encode(payload, IPC_TO_WINDOW_MAX_BYTES)


def _request_kind(data: dict[str, object]) -> CaseRequestKind:
    value = data.get("kind")
    if not isinstance(value, str):
        raise ProtocolError("Invalid window message: missing 'kind'")
    try:
        return CaseRequestKind(value)
    except ValueError as e:
        raise ProtocolError(f"Unknown request kind: {value[:40]!r}") from e


def decode_command(line: bytes) -> WindowCommand:
    """Parse one runner -> window line (window side); ProtocolError if invalid."""
    kind, data = _decode(line, IPC_TO_WINDOW_MAX_BYTES)
    match kind:
        case WindowIpcType.STATE:
            state = parse_person_step_state_payload(data)
            return StateCommand(
                steps=state.steps,
                questions_enabled=state.questions_enabled,
                cases=state.cases,
            )
        case WindowIpcType.SHOW:
            return ShowCommand()
        case WindowIpcType.HIDE:
            return HideCommand(seq=_seq(data))
        case WindowIpcType.RESULT:
            return ResultCommand(ok=_bool(data, "ok"), message=_message(data))
        case WindowIpcType.QUESTION_RESULT:
            return QuestionResultCommand(ok=_bool(data, "ok"), message=_message(data))
        case WindowIpcType.CASE_RESULT:
            return CaseResultCommand(
                kind=_request_kind(data), ok=_bool(data, "ok"), message=_message(data)
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
        case QuestionEvent(question=question):
            payload = {
                "type": WindowIpcType.QUESTION.value,
                "person_step_id": question.person_step_id,
                "round": question.round,
                "question": question.question,
            }
        case ClosedByUserEvent():
            payload = {"type": WindowIpcType.CLOSED_BY_USER.value}
        case PauseEvent(pause=pause):
            payload = {
                "type": WindowIpcType.PAUSE.value,
                "pivot_id": pause.pivot_id,
                "message": pause.message,
            }
        case ChatSendEvent(send=send):
            payload = {
                "type": WindowIpcType.CHAT_SEND.value,
                "pivot_id": send.pivot_id,
                "help_request_id": send.help_request_id,
                "text": send.text,
            }
        case HelpActionEvent(action=action):
            payload = {
                "type": WindowIpcType.HELP_ACTION.value,
                "pivot_id": action.pivot_id,
                "help_request_id": action.help_request_id,
                "action": action.action.value,
                "note": action.note,
                "continue_mode": (
                    action.continue_mode.value
                    if action.continue_mode is not None
                    else ""
                ),
                "plan_revision_id": action.plan_revision_id or 0,
                "use_plan_revision": action.use_plan_revision,
            }
    return _encode(payload, IPC_TO_RUNNER_MAX_BYTES)


def decode_event(line: bytes) -> WindowEvent:
    """Parse one window -> runner line (runner side); ProtocolError if invalid.

    An action, a question, a pause, a discussion message or a help resolution
    is validated with the shared protocol parser (caps, outcome, note required
    for "different", non-empty texts, known actions and modes), exactly as the
    server will.
    """
    kind, data = _decode(line, IPC_TO_RUNNER_MAX_BYTES)
    match kind:
        case WindowIpcType.READY:
            return ReadyEvent()
        case WindowIpcType.HIDDEN:
            return HiddenEvent(seq=_seq(data))
        case WindowIpcType.ACTION:
            return ActionEvent(action=parse_person_step_action_payload(data))
        case WindowIpcType.QUESTION:
            return QuestionEvent(question=parse_person_step_question_payload(data))
        case WindowIpcType.CLOSED_BY_USER:
            return ClosedByUserEvent()
        case WindowIpcType.PAUSE:
            return PauseEvent(pause=parse_case_pause_action_payload(data))
        case WindowIpcType.CHAT_SEND:
            return ChatSendEvent(send=parse_person_chat_send_payload(data))
        case WindowIpcType.HELP_ACTION:
            return HelpActionEvent(action=parse_person_help_action_payload(data))
    raise ProtocolError(f"Not a window event: {kind.value}")
