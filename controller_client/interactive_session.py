from __future__ import annotations

import logging
import time
import uuid

import pexpect

from controller_client.exceptions import ExecutionError

logger = logging.getLogger(__name__)

_DEFAULT_READ_TIMEOUT: float = 5.0


class InteractiveSession:
    def __init__(self, command: str, overall_timeout: float) -> None:
        self._command = command
        self._overall_timeout = overall_timeout
        self._child: pexpect.spawn[str] | None = None
        self._session_id = str(uuid.uuid4())
        self._start_time = 0.0

    @property
    def session_id(self) -> str:
        return self._session_id

    def start(self, read_timeout: float = _DEFAULT_READ_TIMEOUT) -> str:
        self._start_time = time.monotonic()
        self._child = pexpect.spawn(
            "/bin/bash",
            ["-c", self._command],
            encoding="utf-8",
            timeout=read_timeout,
        )
        return self._read_output(self._child, read_timeout)

    def send_input(self, text: str, read_timeout: float = _DEFAULT_READ_TIMEOUT) -> str:
        child = self._require_child()
        if text:
            child.sendline(text)
        return self._read_output(child, read_timeout)

    def read_output(self, read_timeout: float = _DEFAULT_READ_TIMEOUT) -> str:
        child = self._require_child()
        return self._read_output(child, read_timeout)

    def is_alive(self) -> bool:
        if self._child is None:
            return False
        result: bool = self._child.isalive()
        return result

    def exit_code(self) -> int | None:
        if self._child is None:
            return None
        if self._child.isalive():
            return None
        status: int | None = self._child.exitstatus
        return status

    def terminate(self) -> None:
        if self._child is not None and self._child.isalive():
            self._child.terminate(force=True)

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start_time) * 1000

    def _require_child(self) -> pexpect.spawn[str]:
        if self._child is None:
            raise ExecutionError("Session not started")
        elapsed = time.monotonic() - self._start_time
        if elapsed > self._overall_timeout:
            self.terminate()
            raise ExecutionError(
                f"Session exceeded overall timeout of {self._overall_timeout}s"
            )
        return self._child

    def _read_output(self, child: pexpect.spawn[str], read_timeout: float) -> str:
        try:
            child.expect([pexpect.TIMEOUT, pexpect.EOF], timeout=read_timeout)
        except pexpect.TIMEOUT:
            pass
        raw: str | bytes | None = child.before
        if raw is None:
            return ""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)


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
