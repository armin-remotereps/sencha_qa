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
    ActionEvent,
    ClosedByUserEvent,
    ConnectionCommand,
    HiddenEvent,
    HideCommand,
    QuitCommand,
    ReadyEvent,
    ResultCommand,
    ShowCommand,
    StateCommand,
    WindowCommand,
    decode_event,
    encode_command,
    next_seq,
)
from controller_client.protocol import (
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

_StepKey = tuple[int, int]


def _key(step: PersonStepStatePayload) -> _StepKey:
    return (step.person_step_id, step.round)


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

    def __init__(self) -> None:
        self._steps: tuple[PersonStepStatePayload, ...] = ()
        self._connected = True
        self._process: asyncio.subprocess.Process | None = None
        self._ready = False
        self._supervisor: asyncio.Task[None] | None = None
        # What the manager last told the window (the window may hide itself
        # earlier on an answer or close; that is reported as an event).
        self._visible = False
        # Steps the person answered or closed the window on: no re-show for them.
        self._suppressed: frozenset[_StepKey] = frozenset()
        # A server reply the person must see (refused, or saved but not queued).
        self._result_needs_attention = False
        # Step rounds the window was told to show (once shown, not "new").
        self._shown: frozenset[_StepKey] = frozenset()
        self._holds = 0
        # When the last agent action ended (loop time); None before any.
        self._last_action_end: float | None = None
        self._reshow: asyncio.TimerHandle | None = None
        self._hide_seq = 0
        self._hide_waiters: dict[int, asyncio.Future[None]] = {}
        self._actions: asyncio.Queue[PersonStepActionPayload] = asyncio.Queue()
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

    def apply_state(self, steps: Sequence[PersonStepStatePayload]) -> None:
        """The latest ``person_step_state`` snapshot (newest first)."""
        if self._disabled or self._stopping:
            return
        self._steps = tuple(steps)
        keys = {_key(step) for step in self._steps}
        self._suppressed &= keys
        self._shown &= keys
        self._send(StateCommand(steps=self._steps))
        if not self._steps:
            # The window hides itself on an empty snapshot.
            self._visible = False
        self._sync_visibility()
        if self._steps and self._supervisor is None:
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

    async def ensure_hidden(self) -> None:
        """Hide the window and wait for its ack; no IPC when already hidden."""
        if not self._visible or not self._ready or self._process is None:
            self._visible = False
            return
        self._visible = False
        self._hide_seq = next_seq(self._hide_seq)
        seq = self._hide_seq
        ack: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._hide_waiters[seq] = ack
        self._send(HideCommand(seq=seq))
        try:
            await asyncio.wait_for(ack, _HIDE_ACK_TIMEOUT_SECONDS)
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
        finally:
            self._hide_waiters.pop(seq, None)
        await asyncio.sleep(_HIDE_SETTLE_SECONDS)

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

    def _wants_visible(self) -> bool:
        if self._result_needs_attention:
            return True
        return any(_key(step) not in self._suppressed for step in self._steps)

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
        keys = frozenset(_key(step) for step in self._steps)
        if self._last_action_end is not None:
            # Something new for the person: a new step round, or a server
            # reply about their answer (shown once when no step is open).
            news = bool(keys - self._shown) or self._result_needs_attention
            quiet = _RESHOW_QUIET_SECONDS if news else _RESHOW_IDLE_SECONDS
            delay = self._last_action_end + quiet - loop.time()
            if delay > 0:
                self._cancel_reshow()
                self._reshow = loop.call_later(delay, self._sync_visibility)
                return
        self._visible = True
        self._shown |= keys
        if not self._steps:
            # Nothing open: the reply is shown this once, never re-shown.
            self._result_needs_attention = False
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
            while self._steps and not self._stopping:
                if not self._may_start():
                    logger.error(
                        "The person-step window keeps closing; it stays off for "
                        "this Test Runner session. Answer person steps on Punk "
                        "Hazard."
                    )
                    self._disabled = True
                    self._steps = ()
                    return
                try:
                    process = await _spawn_app(
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
        self._send(StateCommand(steps=self._steps))
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
                self._send(ResultCommand(ok=False, message=_REFUSED_INVALID))
                continue
            match event:
                case HiddenEvent(seq=seq):
                    waiter = self._hide_waiters.pop(seq, None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(None)
                case ActionEvent(action=action):
                    self._on_action(action)
                case ClosedByUserEvent():
                    self._visible = False
                    self._result_needs_attention = False
                    self._suppressed = frozenset(_key(s) for s in self._steps)
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
        # Stay hidden until a new step or round, or a refusal.
        self._suppressed = keys
        self._actions.put_nowait(action)

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
            logger.error("Could not send to the person-step window: %s", e)
            return
        if stdin.transport.get_write_buffer_size() > _MAX_WRITE_BUFFER_BYTES:
            logger.error("The person-step window stopped reading; closing it")
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            return
        stdin.write(data)

    def _release_hide_waiters(self) -> None:
        for waiter in self._hide_waiters.values():
            if not waiter.done():
                waiter.set_result(None)
        self._hide_waiters.clear()
