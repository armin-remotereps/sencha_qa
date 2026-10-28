from __future__ import annotations

import sys

from controller_client.process_tracker import ProcessTracker

if sys.platform != "win32":
    import pexpect

    class ShellProcess:
        """A ``/bin/bash -c`` command on a PTY, driven by pexpect."""

        def __init__(
            self, command: str, process_tracker: ProcessTracker, read_timeout: float
        ) -> None:
            self._child: pexpect.spawn[str] = pexpect.spawn(
                "/bin/bash",
                ["-c", command],
                encoding="utf-8",
                codec_errors="replace",
                timeout=read_timeout,
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
