"""JSON-lines messages between the Test Runner and its person-step window.

The window runs as its own process (Tk must own a main thread, which on macOS
the runner's asyncio loop already holds). The runner writes commands to the
window's stdin and reads events from its stdout, one UTF-8 JSON object per
line. The window never talks to the server; every answer goes back through the
runner, which re-validates it.

Stdlib only (plus the shared protocol), so the window process starts fast.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from controller_client.exceptions import ProtocolError
from controller_client.protocol import (
    MANUAL_CASE_MAX_STEPS,
    MANUAL_CASE_SUBMIT_MAX_BYTES,
    MANUAL_CHAT_SEND_MAX_BYTES,
    MANUAL_CLAIM_TOKEN_MAX_CHARS,
    MANUAL_DISCUSSION_SNAPSHOT_MAX_BYTES,
    MANUAL_RUN_STATE_SAFE_BYTES,
    MANUAL_SHOT_KEY_MAX_CHARS,
    MANUAL_STEP_MAX_SECONDS,
    MANUAL_STEP_MAX_SHOTS,
    MANUAL_STEP_NOTE_MAX_CHARS,
    MANUAL_VERDICT_NOTE_MAX_CHARS,
    PERSON_CASE_ACTION_MAX_BYTES,
    PERSON_STEP_ACTION_MAX_BYTES,
    PERSON_STEP_QUESTION_MAX_BYTES,
    PERSON_STEP_RESULT_MESSAGE_MAX_CHARS,
    CasePauseActionPayload,
    ManualActiveCasePayload,
    ManualCaseActionPayload,
    ManualCaseListItemPayload,
    ManualCaseVerdictWire,
    ManualChatSendPayload,
    ManualRunInfoPayload,
    ManualRunStateMessage,
    ManualShotKind,
    ManualStepOutcome,
    PersonCaseStatePayload,
    PersonChatSendPayload,
    PersonHelpActionPayload,
    PersonStepActionPayload,
    PersonStepQuestionAskPayload,
    PersonStepStatePayload,
    parse_case_pause_action_payload,
    parse_manual_case_action_payload,
    parse_manual_chat_send_payload,
    parse_manual_run_state_payload,
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
# The server keeps a ``manual_run_state`` (estimated) below this budget by
# leaving the steps' how-to out, then adds the claimed case's discussion (at
# most MANUAL_DISCUSSION_SNAPSHOT_MAX_BYTES); room is left for the window's
# own fields.
assert (
    MANUAL_RUN_STATE_SAFE_BYTES + MANUAL_DISCUSSION_SNAPSHOT_MAX_BYTES
    < IPC_TO_WINDOW_MAX_BYTES
), "MANUAL_RUN_STATE_SAFE_BYTES does not fit in IPC_TO_WINDOW_MAX_BYTES"
# Largest window -> runner line: one answer (note + output), one question, one
# pause, discussion message or help resolution, or one manual draft, hand-in
# (every step note at its cap, fully escaped) or manual discussion message.
IPC_TO_RUNNER_MAX_BYTES: Final[int] = max(
    PERSON_STEP_ACTION_MAX_BYTES,
    PERSON_STEP_QUESTION_MAX_BYTES,
    PERSON_CASE_ACTION_MAX_BYTES,
    MANUAL_CASE_SUBMIT_MAX_BYTES,
    MANUAL_CHAT_SEND_MAX_BYTES,
)
# A manual screenshot thumbnail (PNG, base64) sent to the window.
MANUAL_THUMBNAIL_MAX_CHARS: Final[int] = 1024 * 1024
_SHOT_KEY_RE: Final[re.Pattern[str]] = re.compile(
    rf"[A-Za-z0-9_-]{{1,{MANUAL_SHOT_KEY_MAX_CHARS}}}"
)
_CLAIM_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    rf"[A-Za-z0-9_-]{{1,{MANUAL_CLAIM_TOKEN_MAX_CHARS}}}"
)
_BASE64_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9+/=]*")

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
    # Manual run (MANUAL_RUN_V1)
    MANUAL_STATE = "manual_state"  # runner -> window
    MANUAL_CAPTURE_RESULT = "manual_capture_result"  # runner -> window
    MANUAL_THUMBNAIL = "manual_thumbnail"  # runner -> window
    MANUAL_RESULT = "manual_result"  # runner -> window
    MANUAL_CASE_ACTION = "manual_case_action"  # window -> runner
    MANUAL_CAPTURE = "manual_capture"  # window -> runner
    MANUAL_DRAFT = "manual_draft"  # window -> runner
    MANUAL_SUBMIT = "manual_submit"  # window -> runner
    MANUAL_THUMBNAIL_REQUEST = "manual_thumbnail_request"  # window -> runner
    # Manual pause and discuss (MANUAL_PAUSE_DISCUSS_V1). Pause / Continue
    # travel as MANUAL_CASE_ACTION with the action "pause" / "continue".
    MANUAL_CHAT_SEND = "manual_chat_send"  # window -> runner
    # Mark sheet (Test Runner 0.17.0): staged screenshots become a step's mark
    # on Save, or are thrown away on Cancel.
    MANUAL_MARK_COMMIT = "manual_mark_commit"  # window -> runner
    MANUAL_MARK_DISCARD = "manual_mark_discard"  # window -> runner


class CaseRequestKind(StrEnum):
    """The window's case requests; at most one of each kind is pending."""

    PAUSE = "pause"
    CHAT = "chat"
    HELP = "help"


class ManualResultKind(StrEnum):
    """What a ``ManualResultCommand`` answers (a capture: a late notice)."""

    CLAIM = "claim"
    RELEASE = "release"
    SUBMIT = "submit"
    CAPTURE = "capture"
    # MANUAL_PAUSE_DISCUSS_V1
    PAUSE = "pause"
    CONTINUE = "continue"
    CHAT = "chat"
    MARK = "mark"


def manual_pause_discuss_supported(active: ManualActiveCasePayload) -> bool:
    """The server sent the claimed case's pause and discussion fields.

    A server without MANUAL_PAUSE_DISCUSS_V1 sends none of them (they parse to
    their defaults); a server with it always sends ``discussion``.
    """
    return (
        active.discussion is not None
        or active.paused
        or active.can_pause
        or active.can_continue
        or bool(active.pause_blocker)
    )


# --- manual run view (runner -> window) -------------------------------------


@dataclass(frozen=True)
class ManualDraftTexts:
    """What the person typed or chose for a claimed case (the window owns it)."""

    # Step number -> note.
    notes: Mapping[int, str] = field(default_factory=dict)
    # Step number -> seconds on the window's step timer.
    seconds: Mapping[int, int] = field(default_factory=dict)
    verdict: ManualCaseVerdictWire | None = None
    verdict_note: str = ""


@dataclass(frozen=True)
class ManualMarkView:
    """A step the person marked: its outcome and its screenshots (1 or more,
    in the order taken)."""

    step_num: int
    outcome: ManualStepOutcome
    keys: tuple[str, ...]


@dataclass(frozen=True)
class ManualDraftView:
    """The runner's draft of the claimed case (marks are the runner's)."""

    pivot_id: int
    claim_token: str
    marks: tuple[ManualMarkView, ...]
    # Blank: no case screenshot.
    case_shot_key: str
    texts: ManualDraftTexts


@dataclass(frozen=True)
class ManualUploadView:
    # Hand-ins in the spool (sent or not) and their screenshots still to upload.
    waiting_cases: int = 0
    waiting_shots: int = 0
    # Hand-ins the server has not taken yet.
    unsent_cases: int = 0
    # An upload or send is in flight.
    sending: bool = False
    # The last problem worth showing (blank when none).
    message: str = ""


@dataclass(frozen=True)
class ManualWindowState:
    """What the window's Manual run tab shows."""

    run: ManualRunInfoPayload | None
    cases: tuple[ManualCaseListItemPayload, ...]
    # The claimed case; None also while its hand-in waits in the spool.
    active: ManualActiveCasePayload | None
    draft: ManualDraftView | None
    # Cases handed in from this runner that the server still shows open.
    submitted_pivots: tuple[int, ...] = ()
    upload: ManualUploadView = ManualUploadView()
    # The spool is at CONTROLLER_MANUAL_SPOOL_MAX_MB: no new screenshots.
    spool_full: bool = False

    @property
    def has_run(self) -> bool:
        return self.run is not None


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
    ``questions``) or a ``ManualStateCommand`` (``manual``: a question about
    the claimed manual case; its own pending slot).
    """

    ok: bool
    message: str
    manual: bool = False


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


@dataclass(frozen=True)
class ManualStateCommand:
    """Replace the Manual run tab's content; ``run`` None leaves the view."""

    state: ManualWindowState


@dataclass(frozen=True)
class ManualCaptureResultCommand:
    """The outcome of the window's ``ManualCaptureEvent`` with the same seq."""

    seq: int
    ok: bool
    # Why no screenshot was taken (blank when ok).
    message: str
    # The new screenshot (ok only) and its PNG thumbnail in base64.
    key: str
    thumbnail_b64: str


@dataclass(frozen=True)
class ManualThumbnailCommand:
    """A screenshot's thumbnail the window asked for (blank: no longer there)."""

    key: str
    thumbnail_b64: str


@dataclass(frozen=True)
class ManualResultCommand:
    """The answer to a claim, release or hand-in (or a capture notice)."""

    kind: ManualResultKind
    ok: bool
    message: str


WindowCommand = (
    StateCommand
    | ShowCommand
    | HideCommand
    | ResultCommand
    | QuestionResultCommand
    | CaseResultCommand
    | ConnectionCommand
    | QuitCommand
    | ManualStateCommand
    | ManualCaptureResultCommand
    | ManualThumbnailCommand
    | ManualResultCommand
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


@dataclass(frozen=True)
class ManualCaseActionEvent:
    """Start (claim) or release a manual case; the window stays as it is."""

    action: ManualCaseActionPayload


@dataclass(frozen=True)
class ManualCaptureEvent:
    """Take a screenshot for a step (Pass / Fail / Retake) or the case.

    The runner hides the window, captures, shows it again and answers with a
    ``ManualCaptureResultCommand`` of the same ``seq``.
    """

    seq: int
    pivot_id: int
    claim_token: str
    kind: ManualShotKind
    # None for the case screenshot.
    step_num: int | None
    # A mark sheet's screenshot: kept apart from the step's mark until a
    # ``ManualMarkCommitEvent`` saves it (step screenshots only).
    staged: bool = False


@dataclass(frozen=True)
class ManualMarkCommitEvent:
    """Save a mark sheet: these staged screenshots (in order) become the
    step's mark with this outcome, replacing any earlier one.

    Answered with a ``ManualResultCommand`` of kind ``mark``.
    """

    pivot_id: int
    claim_token: str
    step_num: int
    outcome: ManualStepOutcome
    keys: tuple[str, ...]


@dataclass(frozen=True)
class ManualMarkDiscardEvent:
    """Cancel a mark sheet: throw these staged screenshots away (no reply)."""

    pivot_id: int
    claim_token: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class ManualDraftEvent:
    """The person's notes, verdict and step times so far (kept by the runner)."""

    pivot_id: int
    claim_token: str
    texts: ManualDraftTexts
    # The step the window shows (reported to the server with the marks).
    current_step: int | None = None


@dataclass(frozen=True)
class ManualSubmitEvent:
    """Hand the claimed case in with these final texts."""

    pivot_id: int
    claim_token: str
    texts: ManualDraftTexts


@dataclass(frozen=True)
class ManualThumbnailRequestEvent:
    key: str


@dataclass(frozen=True)
class ManualChatSendEvent:
    """A message to the claimed case's discussion; the window stays as it is.

    Answered with a ``ManualResultCommand`` of kind ``chat``.
    """

    send: ManualChatSendPayload


WindowEvent = (
    ReadyEvent
    | HiddenEvent
    | ActionEvent
    | QuestionEvent
    | ClosedByUserEvent
    | PauseEvent
    | ChatSendEvent
    | HelpActionEvent
    | ManualCaseActionEvent
    | ManualCaptureEvent
    | ManualDraftEvent
    | ManualSubmitEvent
    | ManualThumbnailRequestEvent
    | ManualChatSendEvent
    | ManualMarkCommitEvent
    | ManualMarkDiscardEvent
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


def _bool(data: Mapping[str, object], field: str) -> bool:
    value = data.get(field)
    if not isinstance(value, bool):
        raise ProtocolError(f"Invalid window message: '{field}' must be a bool")
    return value


def _message(data: Mapping[str, object]) -> str:
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
        case QuestionResultCommand(ok=ok, message=message, manual=manual):
            payload = {
                "type": WindowIpcType.QUESTION_RESULT.value,
                "ok": ok,
                "message": message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
                "manual": manual,
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
        case ManualStateCommand(state=state):
            payload = {"type": WindowIpcType.MANUAL_STATE.value, **_manual_state(state)}
        case ManualCaptureResultCommand(
            seq=seq, ok=ok, message=message, key=key, thumbnail_b64=thumbnail
        ):
            payload = {
                "type": WindowIpcType.MANUAL_CAPTURE_RESULT.value,
                "seq": seq,
                "ok": ok,
                "message": message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
                "key": key,
                "thumbnail_b64": thumbnail,
            }
        case ManualThumbnailCommand(key=key, thumbnail_b64=thumbnail):
            payload = {
                "type": WindowIpcType.MANUAL_THUMBNAIL.value,
                "key": key,
                "thumbnail_b64": thumbnail,
            }
        case ManualResultCommand(kind=manual_kind, ok=ok, message=message):
            payload = {
                "type": WindowIpcType.MANUAL_RESULT.value,
                "kind": manual_kind.value,
                "ok": ok,
                "message": message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
            }
    return _encode(payload, IPC_TO_WINDOW_MAX_BYTES)


def manual_texts_to_wire(texts: ManualDraftTexts) -> dict[str, object]:
    return {
        "notes": {str(num): note for num, note in texts.notes.items()},
        "seconds": {str(num): seconds for num, seconds in texts.seconds.items()},
        "verdict": texts.verdict.value if texts.verdict is not None else "",
        "verdict_note": texts.verdict_note,
    }


def _manual_state(state: ManualWindowState) -> dict[str, object]:
    draft = state.draft
    upload = state.upload
    return {
        "state": {
            "run": state.run.to_wire() if state.run is not None else None,
            "cases": [case.to_wire() for case in state.cases],
            "active": state.active.to_wire() if state.active is not None else None,
            "pending_uploads": [],
        },
        "draft": (
            {
                "pivot_id": draft.pivot_id,
                "claim_token": draft.claim_token,
                "marks": [
                    {
                        "step_num": mark.step_num,
                        "outcome": mark.outcome.value,
                        "keys": list(mark.keys),
                    }
                    for mark in draft.marks
                ],
                "case_shot_key": draft.case_shot_key,
                "texts": manual_texts_to_wire(draft.texts),
            }
            if draft is not None
            else None
        ),
        "submitted_pivots": list(state.submitted_pivots),
        "upload": {
            "waiting_cases": upload.waiting_cases,
            "waiting_shots": upload.waiting_shots,
            "unsent_cases": upload.unsent_cases,
            "sending": upload.sending,
            "message": upload.message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
        },
        "spool_full": state.spool_full,
    }


def _object(data: Mapping[str, object], name: str) -> dict[str, object]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ProtocolError(f"Invalid window message: '{name}' must be an object")
    return value


def _int(data: Mapping[str, object], name: str, minimum: int, maximum: int) -> int:
    value = data.get(name)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ProtocolError(f"Invalid window message: bad '{name}'")
    return value


def _str(data: Mapping[str, object], name: str, max_chars: int) -> str:
    value = data.get(name, "")
    if not isinstance(value, str):
        raise ProtocolError(f"Invalid window message: '{name}' not a string")
    return value[:max_chars]


def _pivot_id(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value:
        raise ProtocolError("Invalid window message: bad run case id")
    return value


def _step_num(value: object) -> int:
    """A step number from a JSON key ("3") or value (3)."""
    if isinstance(value, str) and value.isdecimal() and len(value) <= 4:
        value = int(value)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MANUAL_CASE_MAX_STEPS
    ):
        raise ProtocolError("Invalid window message: bad step number")
    return value


def _optional_step_num(data: Mapping[str, object], name: str) -> int | None:
    value = data.get(name)
    return None if value is None else _step_num(value)


def _shot_key(value: object, *, blank: bool = False) -> str:
    if blank and value == "":
        return ""
    if not isinstance(value, str) or not _SHOT_KEY_RE.fullmatch(value):
        raise ProtocolError("Invalid window message: bad screenshot key")
    return value


def _shot_keys(data: Mapping[str, object], *, minimum: int) -> tuple[str, ...]:
    """A mark sheet's screenshot keys: distinct, ``minimum`` to
    MANUAL_STEP_MAX_SHOTS of them."""
    value = data.get("keys")
    if not isinstance(value, list) or not (
        minimum <= len(value) <= MANUAL_STEP_MAX_SHOTS
    ):
        raise ProtocolError("Invalid window message: bad 'keys'")
    keys = tuple(_shot_key(key) for key in value)
    if len(set(keys)) != len(keys):
        raise ProtocolError("Invalid window message: repeated screenshot key")
    return keys


def _claim_token(data: Mapping[str, object]) -> str:
    value = data.get("claim_token")
    if not isinstance(value, str) or not _CLAIM_TOKEN_RE.fullmatch(value):
        raise ProtocolError("Invalid window message: bad 'claim_token'")
    return value


def _thumbnail(data: Mapping[str, object]) -> str:
    value = _str(data, "thumbnail_b64", MANUAL_THUMBNAIL_MAX_CHARS + 1)
    if len(value) > MANUAL_THUMBNAIL_MAX_CHARS or not _BASE64_RE.fullmatch(value):
        raise ProtocolError("Invalid window message: bad thumbnail")
    return value


def manual_texts_from_wire(data: Mapping[str, object]) -> ManualDraftTexts:
    """Notes and the verdict note are cut at their protocol caps (the window
    refuses longer ones before a hand-in); step times are capped."""
    raw_notes = _object(data, "notes")
    raw_seconds = _object(data, "seconds")
    if len(raw_notes) > MANUAL_CASE_MAX_STEPS or len(raw_seconds) > (
        MANUAL_CASE_MAX_STEPS
    ):
        raise ProtocolError("Invalid window message: too many steps")
    notes: dict[int, str] = {}
    for key, note in raw_notes.items():
        if not isinstance(note, str):
            raise ProtocolError("Invalid window message: a note is not a string")
        notes[_step_num(key)] = note[:MANUAL_STEP_NOTE_MAX_CHARS]
    seconds: dict[int, int] = {}
    for key, value in raw_seconds.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ProtocolError("Invalid window message: bad step time")
        seconds[_step_num(key)] = min(value, MANUAL_STEP_MAX_SECONDS)
    raw_verdict = data.get("verdict", "")
    if not isinstance(raw_verdict, str):
        raise ProtocolError("Invalid window message: bad 'verdict'")
    try:
        verdict = ManualCaseVerdictWire(raw_verdict) if raw_verdict else None
    except ValueError as e:
        raise ProtocolError("Invalid window message: unknown verdict") from e
    return ManualDraftTexts(
        notes=notes,
        seconds=seconds,
        verdict=verdict,
        verdict_note=_str(data, "verdict_note", MANUAL_VERDICT_NOTE_MAX_CHARS),
    )


def _manual_state_from_wire(data: Mapping[str, object]) -> ManualWindowState:
    server: ManualRunStateMessage = parse_manual_run_state_payload(
        _object(data, "state")
    )
    raw_draft = data.get("draft")
    draft: ManualDraftView | None = None
    if raw_draft is not None:
        if not isinstance(raw_draft, dict):
            raise ProtocolError("Invalid window message: bad 'draft'")
        raw_marks = raw_draft.get("marks", [])
        if not isinstance(raw_marks, list) or len(raw_marks) > MANUAL_CASE_MAX_STEPS:
            raise ProtocolError("Invalid window message: bad 'marks'")
        marks: list[ManualMarkView] = []
        for raw_mark in raw_marks:
            if not isinstance(raw_mark, dict):
                raise ProtocolError("Invalid window message: bad mark")
            try:
                outcome = ManualStepOutcome(str(raw_mark.get("outcome")))
            except ValueError as e:
                raise ProtocolError("Invalid window message: bad outcome") from e
            raw_keys = raw_mark.get("keys")
            if (
                not isinstance(raw_keys, list)
                or not 1 <= len(raw_keys) <= MANUAL_STEP_MAX_SHOTS
            ):
                raise ProtocolError("Invalid window message: bad mark screenshots")
            marks.append(
                ManualMarkView(
                    step_num=_step_num(raw_mark.get("step_num")),
                    outcome=outcome,
                    keys=tuple(_shot_key(key) for key in raw_keys),
                )
            )
        draft = ManualDraftView(
            pivot_id=_int(raw_draft, "pivot_id", 1, _MAX_SEQ),
            claim_token=_claim_token(raw_draft),
            marks=tuple(marks),
            case_shot_key=_shot_key(raw_draft.get("case_shot_key", ""), blank=True),
            texts=manual_texts_from_wire(_object(raw_draft, "texts")),
        )
    raw_submitted = data.get("submitted_pivots", [])
    if not isinstance(raw_submitted, list):
        raise ProtocolError("Invalid window message: bad 'submitted_pivots'")
    upload = _object(data, "upload")
    return ManualWindowState(
        run=server.run,
        cases=server.cases,
        active=server.active,
        draft=draft,
        submitted_pivots=tuple(_pivot_id(value) for value in raw_submitted),
        upload=ManualUploadView(
            waiting_cases=_int(upload, "waiting_cases", 0, _MAX_SEQ),
            waiting_shots=_int(upload, "waiting_shots", 0, _MAX_SEQ),
            unsent_cases=_int(upload, "unsent_cases", 0, _MAX_SEQ),
            sending=_bool(upload, "sending"),
            message=_message(upload),
        ),
        spool_full=_bool(data, "spool_full"),
    )


def _manual_result_kind(data: dict[str, object]) -> ManualResultKind:
    value = data.get("kind")
    if not isinstance(value, str):
        raise ProtocolError("Invalid window message: missing 'kind'")
    try:
        return ManualResultKind(value)
    except ValueError as e:
        raise ProtocolError(f"Unknown manual result kind: {value[:40]!r}") from e


def _shot_kind(data: dict[str, object]) -> ManualShotKind:
    value = data.get("kind")
    if not isinstance(value, str):
        raise ProtocolError("Invalid window message: missing 'kind'")
    try:
        return ManualShotKind(value)
    except ValueError as e:
        raise ProtocolError(f"Unknown screenshot kind: {value[:40]!r}") from e


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
            return QuestionResultCommand(
                ok=_bool(data, "ok"),
                message=_message(data),
                manual=data.get("manual") is True,
            )
        case WindowIpcType.CASE_RESULT:
            return CaseResultCommand(
                kind=_request_kind(data), ok=_bool(data, "ok"), message=_message(data)
            )
        case WindowIpcType.CONNECTION:
            return ConnectionCommand(connected=_bool(data, "connected"))
        case WindowIpcType.QUIT:
            return QuitCommand()
        case WindowIpcType.MANUAL_STATE:
            return ManualStateCommand(state=_manual_state_from_wire(data))
        case WindowIpcType.MANUAL_CAPTURE_RESULT:
            ok = _bool(data, "ok")
            return ManualCaptureResultCommand(
                seq=_seq(data),
                ok=ok,
                message=_message(data),
                key=_shot_key(data.get("key", ""), blank=not ok),
                thumbnail_b64=_thumbnail(data),
            )
        case WindowIpcType.MANUAL_THUMBNAIL:
            return ManualThumbnailCommand(
                key=_shot_key(data.get("key")), thumbnail_b64=_thumbnail(data)
            )
        case WindowIpcType.MANUAL_RESULT:
            return ManualResultCommand(
                kind=_manual_result_kind(data),
                ok=_bool(data, "ok"),
                message=_message(data),
            )
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
        case QuestionEvent(question=question) if question.is_manual:
            payload = {
                "type": WindowIpcType.QUESTION.value,
                "pivot_id": question.pivot_id,
                "claim_token": question.claim_token,
                "step_num": question.step_num,
                "question": question.question,
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
        case ManualCaseActionEvent(action=manual_action):
            payload = {
                "type": WindowIpcType.MANUAL_CASE_ACTION.value,
                "action": manual_action.action.value,
                "pivot_id": manual_action.pivot_id,
                "claim_token": manual_action.claim_token,
            }
        case ManualCaptureEvent(
            seq=seq,
            pivot_id=pivot_id,
            claim_token=claim_token,
            kind=shot_kind,
            step_num=step_num,
            staged=staged,
        ):
            payload = {
                "type": WindowIpcType.MANUAL_CAPTURE.value,
                "seq": seq,
                "pivot_id": pivot_id,
                "claim_token": claim_token,
                "kind": shot_kind.value,
                "step_num": step_num,
                "staged": staged,
            }
        case ManualDraftEvent(
            pivot_id=pivot_id,
            claim_token=claim_token,
            texts=texts,
            current_step=current_step,
        ):
            payload = {
                "type": WindowIpcType.MANUAL_DRAFT.value,
                "pivot_id": pivot_id,
                "claim_token": claim_token,
                "texts": manual_texts_to_wire(texts),
                "current_step": current_step,
            }
        case ManualSubmitEvent(pivot_id=pivot_id, claim_token=claim_token, texts=texts):
            payload = {
                "type": WindowIpcType.MANUAL_SUBMIT.value,
                "pivot_id": pivot_id,
                "claim_token": claim_token,
                "texts": manual_texts_to_wire(texts),
            }
        case ManualThumbnailRequestEvent(key=key):
            payload = {"type": WindowIpcType.MANUAL_THUMBNAIL_REQUEST.value, "key": key}
        case ManualChatSendEvent(send=chat):
            payload = {
                "type": WindowIpcType.MANUAL_CHAT_SEND.value,
                "pivot_id": chat.pivot_id,
                "claim_token": chat.claim_token,
                "text": chat.text,
            }
        case ManualMarkCommitEvent(
            pivot_id=pivot_id,
            claim_token=claim_token,
            step_num=step_num,
            outcome=outcome,
            keys=keys,
        ):
            payload = {
                "type": WindowIpcType.MANUAL_MARK_COMMIT.value,
                "pivot_id": pivot_id,
                "claim_token": claim_token,
                "step_num": step_num,
                "outcome": outcome.value,
                "keys": list(keys),
            }
        case ManualMarkDiscardEvent(
            pivot_id=pivot_id, claim_token=claim_token, keys=keys
        ):
            payload = {
                "type": WindowIpcType.MANUAL_MARK_DISCARD.value,
                "pivot_id": pivot_id,
                "claim_token": claim_token,
                "keys": list(keys),
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
        case WindowIpcType.MANUAL_CASE_ACTION:
            return ManualCaseActionEvent(action=parse_manual_case_action_payload(data))
        case WindowIpcType.MANUAL_CAPTURE:
            shot_kind = _shot_kind(data)
            step_num = _optional_step_num(data, "step_num")
            if (shot_kind is ManualShotKind.CASE) != (step_num is None):
                raise ProtocolError("Invalid window message: step and kind disagree")
            staged = data.get("staged", False) is True
            if staged and step_num is None:
                raise ProtocolError("Invalid window message: staged case screenshot")
            return ManualCaptureEvent(
                seq=_seq(data),
                pivot_id=_int(data, "pivot_id", 1, _MAX_SEQ),
                claim_token=_claim_token(data),
                kind=shot_kind,
                step_num=step_num,
                staged=staged,
            )
        case WindowIpcType.MANUAL_DRAFT:
            return ManualDraftEvent(
                pivot_id=_int(data, "pivot_id", 1, _MAX_SEQ),
                claim_token=_claim_token(data),
                texts=manual_texts_from_wire(_object(data, "texts")),
                current_step=_optional_step_num(data, "current_step"),
            )
        case WindowIpcType.MANUAL_SUBMIT:
            return ManualSubmitEvent(
                pivot_id=_int(data, "pivot_id", 1, _MAX_SEQ),
                claim_token=_claim_token(data),
                texts=manual_texts_from_wire(_object(data, "texts")),
            )
        case WindowIpcType.MANUAL_THUMBNAIL_REQUEST:
            return ManualThumbnailRequestEvent(key=_shot_key(data.get("key")))
        case WindowIpcType.MANUAL_CHAT_SEND:
            return ManualChatSendEvent(send=parse_manual_chat_send_payload(data))
        case WindowIpcType.MANUAL_MARK_COMMIT:
            try:
                outcome = ManualStepOutcome(str(data.get("outcome")))
            except ValueError as e:
                raise ProtocolError("Invalid window message: bad outcome") from e
            return ManualMarkCommitEvent(
                pivot_id=_int(data, "pivot_id", 1, _MAX_SEQ),
                claim_token=_claim_token(data),
                step_num=_step_num(data.get("step_num")),
                outcome=outcome,
                keys=_shot_keys(data, minimum=1),
            )
        case WindowIpcType.MANUAL_MARK_DISCARD:
            return ManualMarkDiscardEvent(
                pivot_id=_int(data, "pivot_id", 1, _MAX_SEQ),
                claim_token=_claim_token(data),
                keys=_shot_keys(data, minimum=0),
            )
    raise ProtocolError(f"Not a window event: {kind.value}")
