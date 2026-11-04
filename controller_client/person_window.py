"""The runner-side manager of the person-step window process.

The window (``person_window_app``) runs in its own process, started lazily
with the runner's own interpreter on the first open person step, and talks
JSON lines over its stdin/stdout (``person_window_ipc``). This manager keeps
the latest snapshot, decides when the window may be visible and hides it
before every agent action:

- ``hidden_for_action()`` hides the window and waits for its ack (with a
  short timeout and a compositor settle) so it is never in a screenshot, a
  vision parse or the target of an input action. It re-shows after 5 s
  without agent actions; on macOS, where showing a Tk window activates the
  Python app (``person_window_app``), only a new step round or a server reply
  the person must see re-shows it after 5 s; anything else waits for 30 s
  without agent actions.
- A step the person answered (or a window they closed) stays hidden until a
  new step or round arrives, or the server refuses the answer.
- A server reply the person must see is shown once when no step is open:
  after that show is hidden (by an agent action or the person) the window
  stays hidden until a new step or round arrives.
- Window v2 (``cases``): an open help case (role help, with its discussion)
  also makes the window want to be visible, until the person closes it or
  resolves it; a running case alone never does (it only enables Pause). The
  "news" that re-shows after the short delay is a new step round, a new help
  request, or an agent turn that ended with a new reply. Pausing and sending
  a discussion message never hide or raise the window; a
  help resolution hides it like an answer. A closed discussion stays hidden
  until a new help request or a finished turn with a new reply.
- Manual run (``apply_manual_state``): while the project has a manual run
  the window wants to be visible (its Manual run tab is where the tester
  works). Its requests (claim, release, capture, notes, hand-in, thumbnails;
  ``manual_requests()``) never hide or raise it. A tester screenshot goes
  through ``hidden_for_capture()``: a hide that must be confirmed by the
  window's ack (else no screenshot), and the window is shown again at once
  afterwards, without the agent re-show delays.
- A dead window process is restarted while a step is open, at most
  ``_MAX_STARTS`` times per ``_START_WINDOW_SECONDS``; then the runner keeps
  working without it (people answer on Punk Hazard).
"""

import asyncio
import contextlib
import logging
import subprocess
import sys
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from controller_client.exceptions import ProtocolError
from controller_client.person_window_ipc import (
    IPC_TO_RUNNER_MAX_BYTES,
    IPC_TO_WINDOW_MAX_BYTES,
    TEXT_SIZE_DEFAULT,
    ActionEvent,
    CaseRequestKind,
    CaseResultCommand,
    ChatSendEvent,
    ClosedByUserEvent,
    ConnectionCommand,
    HelpActionEvent,
    HiddenEvent,
    HideCommand,
    ManualCaptureEvent,
    ManualCaptureResultCommand,
    ManualCaseActionEvent,
    ManualChatSendEvent,
    ManualDraftEvent,
    ManualResultCommand,
    ManualResultKind,
    ManualStateCommand,
    ManualSubmitEvent,
    ManualThumbnailCommand,
    ManualThumbnailRequestEvent,
    ManualWindowState,
    PauseEvent,
    QuitCommand,
    ReadyEvent,
    ResultCommand,
    ShowCommand,
    StateCommand,
    WindowCommand,
    clamp_text_size,
    decode_event,
    encode_command,
    next_seq,
)
from controller_client.protocol import (
    CasePauseActionPayload,
    DiscussionMessageKind,
    PersonCaseRole,
    PersonCaseStatePayload,
    PersonChatSendPayload,
    PersonHelpActionKind,
    PersonHelpActionPayload,
    PersonHelpPayload,
    PersonStepActionPayload,
    PersonStepActionResultPayload,
    PersonStepStatePayload,
)

logger = logging.getLogger(__name__)

_APP_MODULE: Final[str] = "controller_client.person_window_app"
# ``-m controller_client...`` resolves from the directory holding the package.
_PACKAGE_PARENT: Final[Path] = Path(__file__).resolve().parent.parent

_READY_TIMEOUT_SECONDS: Final[float] = 10.0
_HIDE_ACK_TIMEOUT_SECONDS: Final[float] = 1.5
# Let the compositor finish unmapping (macOS / GNOME animate it).
_HIDE_SETTLE_SECONDS: Final[float] = 0.15
_RESHOW_QUIET_SECONDS: Final[float] = 5.0
# A show that brings nothing new (no new step round) on macOS, where every
# show activates the window's app: wait until the agent has been idle longer.
_RESHOW_IDLE_SECONDS: Final[float] = 30.0 if sys.platform == "darwin" else 5.0
_STOP_TIMEOUT_SECONDS: Final[float] = 2.0
_RESTART_DELAY_SECONDS: Final[float] = 1.0
# One start plus three restarts per ten minutes.
_MAX_STARTS: Final[int] = 4
_START_WINDOW_SECONDS: Final[float] = 600.0
# A window that stops reading its stdin is killed rather than buffered for.
_MAX_WRITE_BUFFER_BYTES: Final[int] = 2 * IPC_TO_WINDOW_MAX_BYTES
_PROBE_TIMEOUT_SECONDS: Final[float] = 10.0
_PROBE_DETAIL_MAX_CHARS: Final[int] = 500

_REFUSED_STALE: Final[str] = "This step was already answered or is no longer waiting."
_REFUSED_INVALID: Final[str] = (
    "The window sent an answer the Test Runner could not read. Answer on "
    "Punk Hazard instead."
)

_CASE_STALE: Final[str] = "This case changed; look at it again."
_PAUSE_REFUSED: Final[str] = "This case cannot be paused now."
_HELP_STALE: Final[str] = "This help request is no longer open."
_HELP_MODE_REFUSED: Final[str] = "That way to continue is not offered for this case."
_HELP_PLAN_STALE: Final[str] = "The redesigned plan changed; look at it again."
_CHAT_REFUSED: Final[str] = "You cannot send a message now."

_StepKey = tuple[int, int]
# What the person is asked to look at: ("step", step id, round) or ("help",
# help request id, the newest agent reply seen at the end of a turn).
_AttentionKey = tuple[str, int, int]

# A window case request, forwarded to the server by the client.
PersonCaseRequest = (
    CasePauseActionPayload | PersonChatSendPayload | PersonHelpActionPayload
)
# A Manual run request from the window, handled by the client (claim,
# release, pause, continue and discussion messages go to the server; the rest
# stays on this machine until a hand-in).
ManualWindowRequest = (
    ManualCaseActionEvent
    | ManualCaptureEvent
    | ManualDraftEvent
    | ManualSubmitEvent
    | ManualThumbnailRequestEvent
    | ManualChatSendEvent
)


def _key(step: PersonStepStatePayload) -> _StepKey:
    return (step.person_step_id, step.round)


def _step_attention(step: PersonStepStatePayload) -> _AttentionKey:
    return ("step", step.person_step_id, step.round)


def _help_of(case: PersonCaseStatePayload) -> PersonHelpPayload | None:
    """The case's open help request when the window should show it."""
    return case.help if case.role is PersonCaseRole.HELP else None


def _newest_reply(help_state: PersonHelpPayload) -> int:
    return max(
        (
            message.message_id
            for message in help_state.messages
            if message.kind is DiscussionMessageKind.ASSISTANT
        ),
        default=0,
    )


async def _spawn_app(
    *args: str, stdin: int, stderr: int | None, limit: int
) -> asyncio.subprocess.Process:
    if sys.platform == "win32":
        # No console window, also when the runner itself runs detached.
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            _APP_MODULE,
            *args,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            cwd=str(_PACKAGE_PARENT),
            limit=limit,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        _APP_MODULE,
        *args,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
        cwd=str(_PACKAGE_PARENT),
        limit=limit,
    )


async def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    await process.wait()


@dataclass(frozen=True)
class WindowProbeResult:
    ok: bool
    # "Tk 8.6.x" when ok; otherwise one user-facing reason.
    detail: str


async def probe_person_window(
    timeout: float = _PROBE_TIMEOUT_SECONDS,
) -> WindowProbeResult:
    """Check in a subprocess that tkinter imports and a Tk window can open.

    Never raises. The window runs with this same interpreter, so a pass here
    means the window process can start.
    """
    try:
        process = await _spawn_app(
            "--probe",
            stdin=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            limit=IPC_TO_RUNNER_MAX_BYTES + 1,
        )
    except OSError as e:
        return WindowProbeResult(ok=False, detail=f"Could not start Python: {e}")
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        await _kill(process)
        return WindowProbeResult(
            ok=False,
            detail=f"Opening a Tk window did not finish within {timeout:.0f} s "
            "(is the display reachable?)",
        )
    detail = out.decode("utf-8", errors="replace").strip()
    if process.returncode == 0:
        return WindowProbeResult(ok=True, detail=detail[:_PROBE_DETAIL_MAX_CHARS])
    if not detail:
        lines = err.decode("utf-8", errors="replace").strip().splitlines()
        detail = lines[-1] if lines else f"exit code {process.returncode}"
    return WindowProbeResult(ok=False, detail=detail[:_PROBE_DETAIL_MAX_CHARS])


class PersonWindow:
    """Owns the window process; every method runs on the runner's event loop.

    The sync methods never block (they are called from the serial message
    loop); process start-up happens in a background task.
    """

    def __init__(self, *, text_size: int = TEXT_SIZE_DEFAULT) -> None:
        # Passed to the window process at spawn (CONTROLLER_PERSON_WINDOW_TEXT_SIZE).
        self._text_size = clamp_text_size(text_size)
        self._steps: tuple[PersonStepStatePayload, ...] = ()
        self._cases: tuple[PersonCaseStatePayload, ...] = ()
        # Per open help request: the newest agent reply seen while no turn
        # ran (a turn that ends with a newer one is news).
        self._reply_marks: dict[int, int] = {}
        self._connected = True
        self._process: asyncio.subprocess.Process | None = None
        self._ready = False
        self._supervisor: asyncio.Task[None] | None = None
        # What the manager last told the window (the window may hide itself
        # earlier on an answer or close; that is reported as an event).
        self._visible = False
        # What the person answered, resolved or closed the window on: no
        # re-show for it.
        self._suppressed: frozenset[_AttentionKey] = frozenset()
        # A server reply the person must see (refused, or saved but not queued).
        self._result_needs_attention = False
        # What the window was told to show (once shown, not "new").
        self._shown: frozenset[_AttentionKey] = frozenset()
        self._holds = 0
        # When the last agent action ended (loop time); None before any.
        self._last_action_end: float | None = None
        self._reshow: asyncio.TimerHandle | None = None
        self._hide_seq = 0
        # True: the window acknowledged the hide; False: it went away first.
        self._hide_waiters: dict[int, asyncio.Future[bool]] = {}
        self._actions: asyncio.Queue[PersonStepActionPayload] = asyncio.Queue()
        self._case_requests: asyncio.Queue[PersonCaseRequest] = asyncio.Queue()
        # Manual run (MANUAL_RUN_V1): the Manual run tab's content, its
        # requests, a capture notice the window missed while it restarted,
        # and a show owed to a capture that ended while the window was down.
        self._manual: ManualWindowState | None = None
        self._manual_requests: asyncio.Queue[ManualWindowRequest] = asyncio.Queue()
        self._manual_notice = ""
        self._show_after_restart = False
        self._starts: deque[float] = deque()
        self._disabled = False
        self._stopping = False

    # --- public API ---------------------------------------------------------

    @property
    def pid(self) -> int | None:
        """The window process (the close policy protects it), when running."""
        process = self._process
        if process is None or process.returncode is not None:
            return None
        return process.pid

    def apply_state(
        self,
        steps: Sequence[PersonStepStatePayload],
        *,
        cases: Sequence[PersonCaseStatePayload] = (),
    ) -> None:
        """The latest ``person_step_state`` snapshot (newest first).

        ``cases``: the cases behind the steps, open help cases and the running
        case (PERSON_CASE_CONTEXT_V1; empty from an older server).
        """
        if self._disabled or self._stopping:
            return
        self._steps = tuple(steps)
        self._cases = tuple(cases)
        self._update_reply_marks()
        keys = self._attention_keys()
        self._suppressed &= keys
        self._shown &= keys
        self._send(self._state_command())
        if not self._has_content():
            # The window hides itself when nothing is open for the person.
            self._visible = False
        self._sync_visibility()
        self._ensure_supervisor()

    def apply_manual_state(self, state: ManualWindowState | None) -> None:
        """The Manual run tab's content (None: no manual run to show)."""
        if self._disabled or self._stopping:
            return
        state = state if state is not None and state.has_run else None
        if state == self._manual:
            # Upload progress and snapshots often change nothing shown.
            return
        self._manual = state
        keys = self._attention_keys()
        self._suppressed &= keys
        self._shown &= keys
        self._send(
            ManualStateCommand(
                state=state
                or ManualWindowState(run=None, cases=(), active=None, draft=None)
            )
        )
        if not self._has_content():
            self._visible = False
        self._sync_visibility()
        self._ensure_supervisor()

    async def manual_requests(self) -> AsyncIterator[ManualWindowRequest]:
        """Every Manual run request from the window, forever."""
        while True:
            yield await self._manual_requests.get()

    def show_manual_result(
        self, kind: ManualResultKind, ok: bool, message: str
    ) -> None:
        """The answer to a claim, release, pause, continue, discussion message
        or hand-in; never changes visibility."""
        self._send(ManualResultCommand(kind=kind, ok=ok, message=message))

    def show_capture_result(
        self, seq: int, ok: bool, message: str, key: str = "", thumbnail_b64: str = ""
    ) -> None:
        """The outcome of a capture request.

        A failure the window cannot get now (it is restarting after a hide
        that timed out) is shown as a notice once it is back.
        """
        if not self._ready and not ok:
            self._manual_notice = message
            return
        self._send(
            ManualCaptureResultCommand(
                seq=seq, ok=ok, message=message, key=key, thumbnail_b64=thumbnail_b64
            )
        )

    def send_thumbnail(self, key: str, thumbnail_b64: str) -> None:
        self._send(ManualThumbnailCommand(key=key, thumbnail_b64=thumbnail_b64))

    def _ensure_supervisor(self) -> None:
        if self._has_content() and self._supervisor is None:
            self._supervisor = asyncio.get_running_loop().create_task(
                self._supervise(), name="person-window"
            )

    def set_connected(self, connected: bool) -> None:
        """While False the window disables its buttons and says so."""
        self._connected = connected
        self._send(ConnectionCommand(connected=connected))

    def show_result(self, result: PersonStepActionResultPayload) -> None:
        """The server's reply to the person's last answer (or a local timeout)."""
        self._send(ResultCommand(ok=result.ok, message=result.message))
        if not result.ok:
            # Refused: bring the step back so the person can see why.
            self._suppressed = frozenset()
            self._result_needs_attention = True
        else:
            self._result_needs_attention = not result.queued
        self._sync_visibility()

    async def actions(self) -> AsyncIterator[PersonStepActionPayload]:
        """Every validated answer the person sends, forever."""
        while True:
            yield await self._actions.get()

    async def case_requests(self) -> AsyncIterator[PersonCaseRequest]:
        """Every validated pause, discussion message and help resolution."""
        while True:
            yield await self._case_requests.get()

    def show_case_result(self, kind: CaseRequestKind, ok: bool, message: str) -> None:
        """The server's reply to a case request (or a local refusal or timeout).

        A refused help resolution brings the window back so the person sees
        why; a pause or a message never changes visibility.
        """
        self._send(CaseResultCommand(kind=kind, ok=ok, message=message))
        if kind is CaseRequestKind.HELP and not ok:
            self._suppressed = frozenset(
                key for key in self._suppressed if key[0] != "help"
            )
            self._result_needs_attention = True
            self._sync_visibility()

    @contextlib.asynccontextmanager
    async def hidden_for_action(self) -> AsyncIterator[None]:
        """Keep the window hidden around one agent action (nesting allowed)."""
        self._holds += 1
        self._cancel_reshow()
        try:
            await self.ensure_hidden()
            yield
        finally:
            self._holds -= 1
            if self._holds == 0:
                self._last_action_end = asyncio.get_running_loop().time()
                self._sync_visibility()

    @contextlib.asynccontextmanager
    async def hidden_for_capture(self) -> AsyncIterator[bool]:
        """Keep the window hidden around one tester screenshot (Manual run).

        Yields whether the hide was confirmed: the window acknowledged it (a
        hide is always sent, even when the manager thinks the window is
        hidden), followed by the compositor settle. False when the window is
        not ready, went away or did not acknowledge in time (it is killed and
        restarted): the caller then takes no screenshot. Leaving shows the
        window again at once (no agent re-show delay); holds nest with agent
        actions.
        """
        self._holds += 1
        self._cancel_reshow()
        try:
            confirmed = await self.ensure_hidden(always=True)
            yield confirmed
        finally:
            self._holds -= 1
            if self._holds == 0:
                self._show_after_capture()

    async def ensure_hidden(self, *, always: bool = False) -> bool:
        """Hide the window and wait for its ack; True when the hide was confirmed.

        Without ``always`` there is no IPC when the manager already thinks the
        window is hidden (or it is not running), and that counts as hidden:
        the agent path, which ignores the result. With ``always`` a hide is
        sent whenever the window runs, and only its ack confirms it (the
        manual capture path).
        """
        running = self._ready and self._process is not None
        if not running or (not self._visible and not always):
            self._visible = False
            return not always
        self._visible = False
        self._hide_seq = next_seq(self._hide_seq)
        seq = self._hide_seq
        ack: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._hide_waiters[seq] = ack
        self._send(HideCommand(seq=seq))
        confirmed = False
        try:
            confirmed = await asyncio.wait_for(ack, _HIDE_ACK_TIMEOUT_SECONDS)
        except TimeoutError:
            # A window that cannot confirm it is hidden may still be on screen:
            # never let it reach a screenshot. The supervisor restarts it.
            logger.warning(
                "The person-step window did not confirm it was hidden within "
                "%.1f s; stopping it",
                _HIDE_ACK_TIMEOUT_SECONDS,
            )
            process = self._process
            if process is not None:
                await _kill(process)
            # Not ready from now on (the supervisor notices the exit later):
            # replies meant for it wait for the restarted window.
            self._ready = False
        finally:
            self._hide_waiters.pop(seq, None)
        await asyncio.sleep(_HIDE_SETTLE_SECONDS)
        return confirmed

    async def stop(self) -> None:
        """Close the window process for good (idempotent)."""
        self._stopping = True
        self._cancel_reshow()
        process = self._process
        if process is not None and process.returncode is None:
            self._send(QuitCommand())
            try:
                await asyncio.wait_for(process.wait(), _STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), _STOP_TIMEOUT_SECONDS)
                except TimeoutError:
                    await _kill(process)
        supervisor = self._supervisor
        if supervisor is not None:
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
        self._release_hide_waiters()

    # --- visibility -----------------------------------------------------------

    def _help_cases(
        self,
    ) -> tuple[tuple[PersonCaseStatePayload, PersonHelpPayload], ...]:
        return tuple(
            (case, help_state)
            for case in self._cases
            if (help_state := _help_of(case)) is not None
        )

    def _has_content(self) -> bool:
        """Something the window shows for the person (not a running case alone)."""
        return bool(self._steps) or bool(self._help_cases()) or self._manual is not None

    def _update_reply_marks(self) -> None:
        marks: dict[int, int] = {}
        for _case, help_state in self._help_cases():
            request_id = help_state.help_request_id
            previous = self._reply_marks.get(request_id)
            if help_state.turn_live and previous is not None:
                # Replies during a turn are news only once the turn ends.
                marks[request_id] = previous
            else:
                marks[request_id] = _newest_reply(help_state)
        self._reply_marks = marks

    def _attention_keys(self) -> frozenset[_AttentionKey]:
        keys = {_step_attention(step) for step in self._steps}
        keys.update(
            ("help", request_id, reply)
            for request_id, reply in self._reply_marks.items()
        )
        manual = self._manual
        if manual is not None and manual.run is not None:
            keys.add(("manual", manual.run.run_id, 0))
        return frozenset(keys)

    def _wants_visible(self) -> bool:
        if self._result_needs_attention:
            return True
        return bool(self._attention_keys() - self._suppressed)

    def _sync_visibility(self) -> None:
        if not self._ready:
            return
        if not self._wants_visible():
            if self._visible:
                self._visible = False
                self._hide_seq = next_seq(self._hide_seq)
                self._send(HideCommand(seq=self._hide_seq))
            return
        if self._visible or self._holds > 0:
            return
        loop = asyncio.get_running_loop()
        keys = self._attention_keys()
        if self._show_after_restart:
            # A tester screenshot ended while the window was down: show it as
            # soon as it is back, as after any capture.
            self._show_after_restart = False
        elif self._last_action_end is not None:
            # Something new for the person: a new step round, a new help
            # request or a finished turn with a new reply, or a server reply
            # about their answer (shown once when nothing is open).
            news = bool(keys - self._shown) or self._result_needs_attention
            quiet = _RESHOW_QUIET_SECONDS if news else _RESHOW_IDLE_SECONDS
            delay = self._last_action_end + quiet - loop.time()
            if delay > 0:
                self._cancel_reshow()
                self._reshow = loop.call_later(delay, self._sync_visibility)
                return
        self._visible = True
        self._shown |= keys
        if not self._has_content():
            # Nothing open: the reply is shown this once, never re-shown.
            self._result_needs_attention = False
        self._send(ShowCommand())

    def _show_after_capture(self) -> None:
        """Show the window at once after a tester screenshot (no agent delay)."""
        if not self._wants_visible():
            return
        if not self._ready:
            self._show_after_restart = True
            return
        self._cancel_reshow()
        self._visible = True
        self._shown |= self._attention_keys()
        self._send(ShowCommand())

    def _cancel_reshow(self) -> None:
        if self._reshow is not None:
            self._reshow.cancel()
            self._reshow = None

    # --- process ----------------------------------------------------------------

    def _may_start(self) -> bool:
        now = asyncio.get_running_loop().time()
        while self._starts and now - self._starts[0] > _START_WINDOW_SECONDS:
            self._starts.popleft()
        if len(self._starts) >= _MAX_STARTS:
            return False
        self._starts.append(now)
        return True

    async def _supervise(self) -> None:
        try:
            while self._has_content() and not self._stopping:
                if not self._may_start():
                    logger.error(
                        "The person-step window keeps closing; it stays off for "
                        "this Test Runner session. Answer person steps on Punk "
                        "Hazard."
                    )
                    self._disabled = True
                    self._steps = ()
                    self._cases = ()
                    self._manual = None
                    return
                try:
                    process = await _spawn_app(
                        "--text-size",
                        str(self._text_size),
                        stdin=asyncio.subprocess.PIPE,
                        stderr=None,
                        limit=IPC_TO_RUNNER_MAX_BYTES + 1,
                    )
                except OSError as e:
                    logger.error("Could not start the person-step window: %s", e)
                    await asyncio.sleep(_RESTART_DELAY_SECONDS)
                    continue
                self._process = process
                try:
                    await self._serve(process)
                finally:
                    self._process = None
                    self._ready = False
                    self._visible = False
                    self._release_hide_waiters()
                    await _kill(process)
                if not self._stopping:
                    logger.warning(
                        "The person-step window closed (exit code %s)",
                        process.returncode,
                    )
                    await asyncio.sleep(_RESTART_DELAY_SECONDS)
        finally:
            self._supervisor = None

    async def _serve(self, process: asyncio.subprocess.Process) -> None:
        stdout = process.stdout
        if stdout is None:
            return
        try:
            first = await asyncio.wait_for(stdout.readline(), _READY_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.error("The person-step window did not start in time")
            return
        except ValueError:
            logger.error("The person-step window sent an over-long message")
            return
        try:
            ready = decode_event(first) if first else None
        except ProtocolError as e:
            logger.error("The person-step window did not start: %s", e)
            return
        if not isinstance(ready, ReadyEvent):
            logger.error("The person-step window did not start")
            return
        self._ready = True
        self._send(ConnectionCommand(connected=self._connected))
        self._send(self._state_command())
        if self._manual is not None:
            self._send(ManualStateCommand(state=self._manual))
        if self._manual_notice:
            self._send(
                ManualResultCommand(
                    kind=ManualResultKind.CAPTURE, ok=False, message=self._manual_notice
                )
            )
            self._manual_notice = ""
        self._sync_visibility()
        while True:
            try:
                line = await stdout.readline()
            except ValueError:
                logger.error("The person-step window sent an over-long message")
                return
            if not line:
                return
            try:
                event = decode_event(line)
            except ProtocolError as e:
                logger.warning("Invalid message from the person-step window: %s", e)
                # Any kind of request may be waiting: release them all (the
                # window ignores a result for a kind it does not wait for).
                self._send(ResultCommand(ok=False, message=_REFUSED_INVALID))
                for kind in CaseRequestKind:
                    self._send(
                        CaseResultCommand(kind=kind, ok=False, message=_REFUSED_INVALID)
                    )
                for manual_kind in ManualResultKind:
                    self._send(
                        ManualResultCommand(
                            kind=manual_kind, ok=False, message=_REFUSED_INVALID
                        )
                    )
                continue
            match event:
                case HiddenEvent(seq=seq):
                    waiter = self._hide_waiters.pop(seq, None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(True)
                case ActionEvent(action=action):
                    self._on_action(action)
                case PauseEvent(pause=pause):
                    self._on_pause(pause)
                case ChatSendEvent(send=send):
                    self._on_chat_send(send)
                case HelpActionEvent(action=help_action):
                    self._on_help_action(help_action)
                case (
                    ManualCaseActionEvent()
                    | ManualCaptureEvent()
                    | ManualDraftEvent()
                    | ManualSubmitEvent()
                    | ManualThumbnailRequestEvent()
                    | ManualChatSendEvent()
                ):
                    # Never hides or raises the window; validated by the client.
                    self._manual_requests.put_nowait(event)
                case ClosedByUserEvent():
                    self._visible = False
                    self._result_needs_attention = False
                    self._suppressed = self._attention_keys()
                case ReadyEvent():
                    pass

    def _on_action(self, action: PersonStepActionPayload) -> None:
        # The window hid itself before sending the answer.
        self._visible = False
        self._result_needs_attention = False
        keys = frozenset(_key(step) for step in self._steps)
        if (action.person_step_id, action.round) not in keys:
            self.show_result(
                PersonStepActionResultPayload(
                    ok=False, message=_REFUSED_STALE, queued=False
                )
            )
            return
        # Stay hidden until a new step or round, or a refusal (an open help
        # case still brings the window back).
        self._suppressed |= {_step_attention(step) for step in self._steps}
        self._actions.put_nowait(action)

    def _case(self, pivot_id: int) -> PersonCaseStatePayload | None:
        for case in self._cases:
            if case.pivot_id == pivot_id:
                return case
        return None

    def _open_help(
        self, pivot_id: int, help_request_id: int
    ) -> PersonHelpPayload | None:
        case = self._case(pivot_id)
        if case is None:
            return None
        help_state = _help_of(case)
        if help_state is None or help_state.help_request_id != help_request_id:
            return None
        return help_state

    def _on_pause(self, pause: CasePauseActionPayload) -> None:
        # The window stays as it is: no hide, no suppression.
        case = self._case(pause.pivot_id)
        if case is None:
            message = _CASE_STALE
        elif case.pause is None or not case.pause.can_pause:
            message = (
                case.pause.pause_blocker if case.pause is not None else ""
            ) or _PAUSE_REFUSED
        else:
            self._case_requests.put_nowait(pause)
            return
        self.show_case_result(CaseRequestKind.PAUSE, False, message)

    def _on_chat_send(self, send: PersonChatSendPayload) -> None:
        # The window stays as it is: no hide, no suppression.
        help_state = self._open_help(send.pivot_id, send.help_request_id)
        if help_state is None:
            message = _HELP_STALE
        elif not help_state.can_send:
            message = help_state.send_blocker or _CHAT_REFUSED
        else:
            self._case_requests.put_nowait(send)
            return
        self.show_case_result(CaseRequestKind.CHAT, False, message)

    def _on_help_action(self, action: PersonHelpActionPayload) -> None:
        # The window hid itself before sending the resolution.
        self._visible = False
        self._result_needs_attention = False
        help_state = self._open_help(action.pivot_id, action.help_request_id)
        message = ""
        if help_state is None:
            message = _HELP_STALE
        elif action.action is PersonHelpActionKind.CONTINUE:
            draft = help_state.plan_draft
            if (
                action.continue_mode is not None
                and action.continue_mode not in help_state.continue_mode_choices
            ):
                message = _HELP_MODE_REFUSED
            elif (draft.revision_id if draft is not None else None) != (
                action.plan_revision_id
            ):
                message = _HELP_PLAN_STALE
        if message:
            self.show_case_result(CaseRequestKind.HELP, False, message)
            return
        # Stay hidden until something new arrives, or a refusal.
        self._suppressed |= {
            key
            for key in self._attention_keys()
            if key[0] == "help" and key[1] == action.help_request_id
        }
        self._case_requests.put_nowait(action)

    def _state_command(self) -> StateCommand:
        return StateCommand(steps=self._steps, cases=self._cases)

    def _send(self, command: WindowCommand) -> None:
        process = self._process
        if not self._ready or process is None or process.stdin is None:
            return
        stdin = process.stdin
        if stdin.is_closing():
            return
        try:
            data = encode_command(command)
        except ProtocolError as e:
            if isinstance(command, ManualStateCommand):
                logger.error(
                    "The Manual run view is too large for the window (%s); open "
                    "the run on Punk Hazard",
                    e,
                )
                return
            if not isinstance(command, StateCommand) or not command.cases:
                logger.error("Could not send to the person-step window: %s", e)
                return
            # Only possible with pathological escaping: keep the steps.
            logger.warning(
                "The person-step window snapshot is too large (%s); sending it "
                "without the cases",
                e,
            )
            self._send(StateCommand(steps=command.steps))
            return
        if stdin.transport.get_write_buffer_size() > _MAX_WRITE_BUFFER_BYTES:
            logger.error("The person-step window stopped reading; closing it")
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            return
        stdin.write(data)

    def _release_hide_waiters(self) -> None:
        # The window went away before acknowledging: not a confirmed hide.
        for waiter in self._hide_waiters.values():
            if not waiter.done():
                waiter.set_result(False)
        self._hide_waiters.clear()
