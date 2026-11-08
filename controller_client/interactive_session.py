from __future__ import annotations

import logging
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from controller_client.exceptions import ExecutionError
from controller_client.kept_output_buffer import KeptOutputBuffer
from controller_client.process_tracker import ProcessTracker
from controller_client.protocol import (
    KEPT_SESSION_BUFFER_BYTES,
    KEPT_SESSIONS_MAX,
    InteractiveSessionInfo,
)

if sys.platform == "win32":
    from controller_client.interactive_process_windows import (
        ShellProcess as _ShellProcess,
    )
else:
    from controller_client.interactive_process_posix import (
        ShellProcess as _ShellProcess,
    )

logger = logging.getLogger(__name__)


class _ShellProcessContract(Protocol):
    """What both platform ``ShellProcess`` classes provide; each mypy pass
    checks only its own platform's class, so this pins the shared shape."""

    def read(self, timeout: float) -> str: ...

    def drain(self, timeout: float) -> str: ...

    def send_line(self, text: str) -> None: ...

    def is_alive(self) -> bool: ...

    def exit_code(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill_tree(self) -> None: ...

    def at_eof(self) -> bool: ...


class TooManySessionsError(ExecutionError):
    """A kept start while the most kept sessions allowed are still running."""


@dataclass(frozen=True)
class SessionOutput:
    text: str
    # A kept session's buffer discarded older output since the last read.
    dropped: bool = False


_DEFAULT_READ_TIMEOUT: float = 5.0
# How long one drain of a kept session's process blocks; also the longest a
# send, a status check or a stop waits for the reader to let go of it.
_KEPT_POLL_SECONDS: Final[float] = 0.2
# After a stop or the time limit, how long the reader keeps draining output
# still held open by a process outside the killed tree.
_KEPT_STOP_GRACE_SECONDS: Final[float] = 5.0
# Kept sessions that ended stay readable (their final output) until stopped;
# beyond this many, the oldest ended ones are forgotten on the next kept start.
_KEPT_ENDED_RETAINED: Final[int] = KEPT_SESSIONS_MAX


class InteractiveSession:
    """One command on a shell process.

    The foreground session is read by its caller and ends after
    ``overall_timeout``. A kept session (``max_seconds`` set) is drained by
    its own reader thread into a bounded buffer, so it never stalls on unread
    output; it is stopped after ``max_seconds`` and every read returns what
    the buffer holds.
    """

    def __init__(
        self,
        command: str,
        overall_timeout: float,
        *,
        max_seconds: float | None = None,
    ) -> None:
        self._command = command
        self._overall_timeout = overall_timeout
        self._process: _ShellProcessContract | None = None
        self._session_id = str(uuid.uuid4())
        self._start_time = 0.0
        self._max_seconds = max_seconds
        # Kept sessions only. ``_process_lock`` serializes every call into the
        # process (the reader's drain, send, stop): neither platform's process
        # object is safe to use from two threads at once.
        self._process_lock = threading.Lock()
        self._buffer = KeptOutputBuffer(KEPT_SESSION_BUFFER_BYTES)
        self._ended = threading.Event()
        self._stopping = threading.Event()
        self._exit_code: int | None = None
        self._ended_at: float | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def kept(self) -> bool:
        return self._max_seconds is not None

    def start(
        self,
        process_tracker: ProcessTracker,
        read_timeout: float = _DEFAULT_READ_TIMEOUT,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Start the command; ``env`` None inherits the runner's environment."""
        return self.start_with_status(process_tracker, read_timeout, env).text

    def start_with_status(
        self,
        process_tracker: ProcessTracker,
        read_timeout: float = _DEFAULT_READ_TIMEOUT,
        env: Mapping[str, str] | None = None,
    ) -> SessionOutput:
        self._start_time = time.monotonic()
        if not self.kept:
            self._process = _ShellProcess(
                self._command, process_tracker, read_timeout, env
            )
            return SessionOutput(self._process.read(read_timeout))
        try:
            process = _ShellProcess(
                self._command, process_tracker, _KEPT_POLL_SECONDS, env
            )
        except BaseException:
            self._mark_ended(None)
            raise
        self._process = process
        if self._stopping.is_set():
            # Stopped while it was starting: the reader records the end.
            with self._process_lock:
                process.kill_tree()
        threading.Thread(
            target=self._drain,
            name=f"kept-session-{self._session_id[:8]}",
            daemon=True,
        ).start()
        return self._read_kept(read_timeout)

    def send_input(self, text: str, read_timeout: float = _DEFAULT_READ_TIMEOUT) -> str:
        return self.send_input_with_status(text, read_timeout).text

    def send_input_with_status(
        self, text: str, read_timeout: float = _DEFAULT_READ_TIMEOUT
    ) -> SessionOutput:
        process = self._require_process()
        if not self.kept:
            if text:
                process.send_line(text)
            return SessionOutput(process.read(read_timeout))
        if text:
            if self._ended.is_set():
                raise ExecutionError("process has exited")
            with self._process_lock:
                process.send_line(text)
        return self._read_kept(read_timeout)

    def read_output(self, read_timeout: float = _DEFAULT_READ_TIMEOUT) -> str:
        return self.read_output_with_status(read_timeout).text

    def read_output_with_status(
        self, read_timeout: float = _DEFAULT_READ_TIMEOUT
    ) -> SessionOutput:
        process = self._require_process()
        if not self.kept:
            return SessionOutput(process.read(read_timeout))
        return self._read_kept(read_timeout)

    def is_alive(self) -> bool:
        if self.kept:
            return self._process is not None and not self._ended.is_set()
        return self._process is not None and self._process.is_alive()

    def exit_code(self) -> int | None:
        if self.kept:
            return self._exit_code if self._ended.is_set() else None
        if self._process is None:
            return None
        return self._process.exit_code()

    def terminate(self) -> None:
        if not self.kept:
            if self._process is not None:
                self._process.terminate()
            return
        # Set first: a start still creating the process stops it itself.
        self._stopping.set()
        process = self._process
        if process is None:
            return
        with self._process_lock:
            process.kill_tree()
        # The reader notices on its next drain (within one poll) and records
        # the exit; wait briefly so the caller sees the final state.
        self._ended.wait(timeout=_KEPT_POLL_SECONDS * 5)

    def elapsed_ms(self) -> float:
        end = self._ended_at if self._ended_at is not None else time.monotonic()
        return (end - self._start_time) * 1000

    def info(self) -> InteractiveSessionInfo:
        """This session's state for a session list; never raises."""
        try:
            alive = self.is_alive()
            exit_code = self.exit_code()
        except Exception:
            # The foreground process is read by its own caller without a
            # lock; a status check racing its exit is reported as alive.
            alive, exit_code = True, None
        return InteractiveSessionInfo(
            session_id=self._session_id,
            alive=alive,
            exit_code=exit_code,
            elapsed_ms=self.elapsed_ms() if self._start_time else 0.0,
            kept=self.kept,
        )

    def ended(self) -> bool:
        """A kept session whose process is gone (or never started)."""
        return self._ended.is_set()

    def _require_process(self) -> _ShellProcessContract:
        if self._process is None:
            raise ExecutionError("Session not started")
        if self.kept:
            # Its lifetime is enforced by its reader (max_seconds).
            return self._process
        elapsed = time.monotonic() - self._start_time
        if elapsed > self._overall_timeout:
            self.terminate()
            raise ExecutionError(
                f"Session exceeded overall timeout of {self._overall_timeout}s"
            )
        return self._process

    # --- Kept sessions --------------------------------------------------

    def _read_kept(self, read_timeout: float) -> SessionOutput:
        """Wait up to ``read_timeout`` (less if the session ends), then take
        everything buffered."""
        self._ended.wait(timeout=max(read_timeout, 0.0))
        text, dropped = self._buffer.take()
        return SessionOutput(text, dropped)

    def _drain(self) -> None:
        process = self._process
        assert process is not None
        max_seconds = self._max_seconds
        assert max_seconds is not None
        timed_out = False
        gone_since: float | None = None
        try:
            while True:
                if (
                    not timed_out
                    and not self._stopping.is_set()
                    and time.monotonic() - self._start_time > max_seconds
                ):
                    timed_out = True
                    with self._process_lock:
                        process.kill_tree()
                with self._process_lock:
                    text = process.drain(_KEPT_POLL_SECONDS)
                    # Ends only once the shell exited and nothing holds its
                    # output open any more (a background child keeps it
                    # running, drained, until it exits, is stopped or times
                    # out).
                    shell_gone = not process.is_alive()
                    finished = not text and shell_gone and process.at_eof()
                if text:
                    self._buffer.append(text)
                if finished:
                    break
                if shell_gone and (timed_out or self._stopping.is_set()):
                    # Stopped, but something outside the killed tree still
                    # holds the output open: give up on it after a grace.
                    now = time.monotonic()
                    gone_since = gone_since if gone_since is not None else now
                    if now - gone_since > _KEPT_STOP_GRACE_SECONDS:
                        break
            with self._process_lock:
                exit_code = process.exit_code()
        except Exception:
            logger.exception("Kept session %s reader failed", self._session_id)
            with self._process_lock:
                try:
                    process.kill_tree()
                except Exception:
                    logger.exception("Kept session %s stop failed", self._session_id)
            exit_code = None
        if timed_out:
            self._buffer.append(
                f"\n[kept session stopped after its limit of {max_seconds:g} s]\n"
            )
        self._mark_ended(exit_code)

    def _mark_ended(self, exit_code: int | None) -> None:
        self._exit_code = exit_code
        self._ended_at = time.monotonic()
        self._ended.set()


class InteractiveSessionManager:
    """The single foreground session plus any kept sessions.

    A plain start replaces only the foreground session; kept sessions are
    never replaced and are stopped by id, by ``terminate_all`` or after their
    ``max_seconds``. Safe to call from several threads.
    """

    def __init__(self) -> None:
        self._session: InteractiveSession | None = None
        # Insertion order = start order (oldest first).
        self._kept: dict[str, InteractiveSession] = {}
        self._lock = threading.Lock()

    def start_session(
        self,
        command: str,
        timeout: float,
        *,
        keep: bool = False,
        max_seconds: float | None = None,
        max_kept: int | None = None,
    ) -> InteractiveSession:
        if keep:
            return self._start_kept(command, timeout, max_seconds, max_kept)
        with self._lock:
            previous = self._session
            self._session = None
        if previous is not None:
            logger.info("Terminating existing session %s", previous.session_id)
            previous.terminate()
        session = InteractiveSession(command, timeout)
        with self._lock:
            replaced = self._session
            self._session = session
        if replaced is not None:
            # Another plain start won the race; only one foreground session.
            logger.info("Terminating existing session %s", replaced.session_id)
            replaced.terminate()
        return session

    def get_session(self, session_id: str) -> InteractiveSession:
        with self._lock:
            session = self._find(session_id)
        if session is None:
            raise ExecutionError(f"No active session with id {session_id}")
        return session

    def terminate_session(self, session_id: str) -> InteractiveSession:
        with self._lock:
            session = self._find(session_id)
            if session is None:
                raise ExecutionError(f"No active session with id {session_id}")
            if session is self._session:
                self._session = None
            else:
                self._kept.pop(session_id, None)
        session.terminate()
        return session

    def discard(self, session_id: str) -> None:
        """Forget a kept session whose start failed."""
        with self._lock:
            session = self._kept.pop(session_id, None)
        if session is not None:
            session.terminate()

    def terminate_all(self) -> None:
        with self._lock:
            sessions = list(self._kept.values())
            self._kept.clear()
            foreground = self._session
            self._session = None
        for session in sessions:
            logger.info("Terminating kept session %s", session.session_id)
            try:
                session.terminate()
            except Exception:
                logger.exception("Kept session %s stop failed", session.session_id)
        if foreground is not None:
            logger.info("Terminating existing session %s", foreground.session_id)
            foreground.terminate()

    def list_sessions(self) -> tuple[InteractiveSessionInfo, ...]:
        with self._lock:
            sessions = ([self._session] if self._session is not None else []) + list(
                self._kept.values()
            )
        return tuple(session.info() for session in sessions)

    def _start_kept(
        self,
        command: str,
        timeout: float,
        max_seconds: float | None,
        max_kept: int | None,
    ) -> InteractiveSession:
        if max_seconds is None or max_kept is None:
            raise ExecutionError("A kept session needs max_seconds and max_kept")
        limit = min(max_kept, KEPT_SESSIONS_MAX)
        with self._lock:
            running = sum(1 for s in self._kept.values() if not s.ended())
            if running >= limit:
                raise TooManySessionsError(
                    f"{running} kept sessions are already running (limit {limit})"
                )
            ended = [s for s in self._kept.values() if s.ended()]
            for old in ended[: max(len(ended) - _KEPT_ENDED_RETAINED, 0)]:
                del self._kept[old.session_id]
            session = InteractiveSession(command, timeout, max_seconds=max_seconds)
            self._kept[session.session_id] = session
        return session

    def _find(self, session_id: str) -> InteractiveSession | None:
        if self._session is not None and self._session.session_id == session_id:
            return self._session
        return self._kept.get(session_id)
