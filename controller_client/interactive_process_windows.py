from __future__ import annotations

import codecs
import os
import queue
import subprocess
import sys
import threading
import time
from typing import IO, Final

from controller_client.exceptions import ExecutionError
from controller_client.process_tracker import ProcessTracker
from controller_client.shell_encoding import SHELL_OUTPUT_ENCODING

if sys.platform == "win32":
    from controller_client.process_tracker import kill_process_tree

    _READ_CHUNK_BYTES: Final[int] = 4096
    _TERMINATE_WAIT_SECONDS: Final[float] = 5.0
    # Output kept after cmd exits: cmd's own last bytes, plus about one read
    # window of a `start /b` child's startup log (e.g. "listening on ...").
    _EXIT_DRAIN_SECONDS: Final[float] = 5.0
    # By full path: a bare "cmd.exe" is searched in the runner's own folders
    # before System32.
    _CMD_EXE: Final[str] = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"
    )

    class ShellProcess:
        """A ``cmd.exe /c`` command on pipes, in its own hidden console.

        No PTY: input is not echoed, and programs that read only the console
        (getpass and the like) cannot be answered.
        """

        def __init__(
            self, command: str, process_tracker: ProcessTracker, read_timeout: float
        ) -> None:
            # A string, not a list: list2cmdline would backslash-escape inner
            # quotes, which cmd does not understand. /s strips just the outer pair.
            self._proc = subprocess.Popen(
                f'cmd.exe /d /s /c "{command}"',
                executable=_CMD_EXE,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            process_tracker.register(self._proc.pid)
            self._chunks: queue.Queue[bytes | None] = queue.Queue()
            self._decoder = codecs.getincrementaldecoder(SHELL_OUTPUT_ENCODING)(
                errors="replace"
            )
            self._eof = False
            # A process started with `start "" /b` inherits the pipe and outlives
            # cmd. Shortly after cmd exits (or on terminate) keep draining, so
            # it never blocks on a full pipe, but drop the output instead of
            # queueing it forever.
            self._detached = False
            stdout = self._proc.stdout
            assert stdout is not None
            threading.Thread(target=self._pump, args=(stdout,), daemon=True).start()

        def _pump(self, stdout: IO[bytes]) -> None:
            fd = stdout.fileno()
            exited_at: float | None = None
            try:
                while True:
                    chunk = os.read(fd, _READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    if exited_at is None and self._proc.poll() is not None:
                        exited_at = time.monotonic()
                    if (
                        exited_at is not None
                        and time.monotonic() - exited_at > _EXIT_DRAIN_SECONDS
                    ):
                        self._detached = True
                    if not self._detached:
                        self._chunks.put(chunk)
            except OSError:
                pass
            finally:
                self._chunks.put(None)

        def read(self, timeout: float) -> str:
            deadline = time.monotonic() + timeout
            parts: list[str] = []
            while not self._eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    chunk = self._chunks.get(timeout=remaining)
                except queue.Empty:
                    break
                if chunk is None:
                    self._eof = True
                    parts.append(self._decoder.decode(b"", final=True))
                else:
                    parts.append(self._decoder.decode(chunk))
            if self._eof:
                # The pipe closes a moment before cmd's exit is visible; wait
                # so callers polling is_alive() do not spin.
                try:
                    self._proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
                except subprocess.TimeoutExpired:
                    pass
            return "".join(parts)

        def send_line(self, text: str) -> None:
            stdin = self._proc.stdin
            if stdin is None:
                raise ExecutionError("process has exited")
            try:
                stdin.write((text + "\r\n").encode(SHELL_OUTPUT_ENCODING, "replace"))
                stdin.flush()
            except OSError as exc:
                raise ExecutionError("process has exited") from exc

        def is_alive(self) -> bool:
            return self._proc.poll() is None

        def exit_code(self) -> int | None:
            return self._proc.poll()

        def terminate(self) -> None:
            self._detached = True
            while not self._chunks.empty():
                self._chunks.get_nowait()
            if self._proc.poll() is None:
                kill_process_tree(self._proc.pid)
                try:
                    self._proc.wait(timeout=_TERMINATE_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            if self._proc.stdin is not None:
                try:
                    self._proc.stdin.close()
                except OSError:
                    pass
