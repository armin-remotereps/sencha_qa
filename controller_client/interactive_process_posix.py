from __future__ import annotations

import os
import signal
import sys
from collections.abc import Mapping
from typing import Final

from controller_client.process_tracker import ProcessTracker

if sys.platform != "win32":
    import pexpect

    _DRAIN_CHUNK_CHARS: Final[int] = 65_536

    class ShellProcess:
        """A ``/bin/bash -c`` command on a PTY, driven by pexpect."""

        def __init__(
            self,
            command: str,
            process_tracker: ProcessTracker,
            read_timeout: float,
            env: Mapping[str, str] | None = None,
        ) -> None:
            self._child: pexpect.spawn[str] = pexpect.spawn(
                "/bin/bash",
                ["-c", command],
                encoding="utf-8",
                codec_errors="replace",
                timeout=read_timeout,
                env=None if env is None else dict(env),
            )
            if self._child.pid is not None:
                process_tracker.register(self._child.pid)

        def read(self, timeout: float) -> str:
            try:
                self._child.expect([pexpect.TIMEOUT, pexpect.EOF], timeout=timeout)
            except pexpect.TIMEOUT:
                pass
            raw: str | bytes | None = self._child.before
            if raw is None:
                return ""
            if isinstance(raw, bytes):
                return raw.decode("utf-8", errors="replace")
            return str(raw)

        def drain(self, timeout: float) -> str:
            """Output that arrived since the last drain, waiting up to
            ``timeout`` for some (kept sessions; never mixed with ``read``,
            whose text repeats everything still in pexpect's buffer)."""
            try:
                text: str = self._child.read_nonblocking(_DRAIN_CHUNK_CHARS, timeout)
            except (pexpect.TIMEOUT, pexpect.EOF):
                return ""
            return text

        def send_line(self, text: str) -> None:
            self._child.sendline(text)

        def is_alive(self) -> bool:
            result: bool = self._child.isalive()
            return result

        def exit_code(self) -> int | None:
            if self._child.isalive():
                return None
            status: int | None = self._child.exitstatus
            return status

        def terminate(self) -> None:
            if self._child.isalive():
                self._child.terminate(force=True)

        def kill_tree(self) -> None:
            """Kill the shell and everything left in its process group (the
            PTY child leads its own session, so the group id is its pid),
            then reap the shell."""
            pid = self._child.pid
            if pid is not None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            self.terminate()

        def at_eof(self) -> bool:
            """Whether every writer of the PTY has closed it."""
            result: bool = self._child.eof()
            return result
