from __future__ import annotations

import logging
import sys
import time
import uuid
from typing import Protocol

from controller_client.exceptions import ExecutionError
from controller_client.process_tracker import ProcessTracker

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

    def send_line(self, text: str) -> None: ...

    def is_alive(self) -> bool: ...

    def exit_code(self) -> int | None: ...

    def terminate(self) -> None: ...


_DEFAULT_READ_TIMEOUT: float = 5.0


class InteractiveSession:
    def __init__(self, command: str, overall_timeout: float) -> None:
        self._command = command
        self._overall_timeout = overall_timeout
        self._process: _ShellProcessContract | None = None
        self._session_id = str(uuid.uuid4())
        self._start_time = 0.0

    @property
    def session_id(self) -> str:
        return self._session_id

    def start(
        self,
        process_tracker: ProcessTracker,
        read_timeout: float = _DEFAULT_READ_TIMEOUT,
    ) -> str:
        self._start_time = time.monotonic()
        self._process = _ShellProcess(self._command, process_tracker, read_timeout)
        return self._process.read(read_timeout)

    def send_input(self, text: str, read_timeout: float = _DEFAULT_READ_TIMEOUT) -> str:
        process = self._require_process()
        if text:
            process.send_line(text)
        return process.read(read_timeout)

    def read_output(self, read_timeout: float = _DEFAULT_READ_TIMEOUT) -> str:
        return self._require_process().read(read_timeout)

    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def exit_code(self) -> int | None:
        if self._process is None:
            return None
        return self._process.exit_code()

    def terminate(self) -> None:
        if self._process is not None:
            self._process.terminate()

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start_time) * 1000

    def _require_process(self) -> _ShellProcessContract:
        if self._process is None:
            raise ExecutionError("Session not started")
        elapsed = time.monotonic() - self._start_time
        if elapsed > self._overall_timeout:
            self.terminate()
            raise ExecutionError(
                f"Session exceeded overall timeout of {self._overall_timeout}s"
            )
        return self._process


class InteractiveSessionManager:
    def __init__(self) -> None:
        self._session: InteractiveSession | None = None

    def start_session(self, command: str, timeout: float) -> InteractiveSession:
        if self._session is not None:
            self._terminate_existing()
        session = InteractiveSession(command, timeout)
        self._session = session
        return session

    def get_session(self, session_id: str) -> InteractiveSession:
        if self._session is None or self._session.session_id != session_id:
            raise ExecutionError(f"No active session with id {session_id}")
        return self._session

    def terminate_session(self, session_id: str) -> InteractiveSession:
        session = self.get_session(session_id)
        session.terminate()
        self._session = None
        return session

    def terminate_all(self) -> None:
        self._terminate_existing()

    def _terminate_existing(self) -> None:
        if self._session is not None:
            logger.info("Terminating existing session %s", self._session.session_id)
            self._session.terminate()
            self._session = None
