from __future__ import annotations

import logging
import threading
from types import TracebackType

import paramiko
from django.conf import settings

from environments.types import ContainerPorts, SSHResult

logger = logging.getLogger(__name__)

_DEFAULT_COMMAND_PREFIX = (
    "export DISPLAY=:0; " "[ -f /tmp/.dbus_env ] && . /tmp/.dbus_env; "
)


class SSHSessionManager:
    """Persistent SSH connection manager for an agent run.

    Lazily connects on first ``execute()`` call.  Auto-reconnects once if
    the underlying transport is dead.  Enables keepalive packets to avoid
    silent connection drops.

    Usage::

        with SSHSessionManager(ports) as ssh:
            result = ssh.execute("echo hello")
    """

    def __init__(
        self,
        ports: ContainerPorts,
        *,
        command_timeout: int | None = None,
        keepalive_interval: int | None = None,
        command_prefix: str = _DEFAULT_COMMAND_PREFIX,
    ) -> None:
        self._ports = ports
        self._command_prefix = command_prefix
        self._command_timeout: int = (
            command_timeout
            if command_timeout is not None
            else int(settings.SSH_COMMAND_TIMEOUT)
        )
        self._keepalive_interval: int = (
            keepalive_interval
            if keepalive_interval is not None
            else int(settings.SSH_KEEPALIVE_INTERVAL)
        )
        self._client: paramiko.SSHClient | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> SSHSessionManager:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._is_transport_active()

    def connect(self) -> None:
        """Open the SSH connection (idempotent if already connected)."""
        with self._lock:
            if self._is_transport_active():
                return
            self._connect_locked()

    def close(self) -> None:
        """Close the SSH connection if open."""
        with self._lock:
            self._close_locked()

    def execute(self, command: str) -> SSHResult:
        """Execute *command* over the persistent SSH connection.

        Reconnects once automatically if the transport is dead.
        """
        with self._lock:
            return self._execute_locked(command)

    # ------------------------------------------------------------------
    # Private helpers (must be called under self._lock)
    # ------------------------------------------------------------------

    def _is_transport_active(self) -> bool:
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def _connect_locked(self) -> None:
        self._close_locked()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname="localhost",
            port=self._ports.ssh,
            username=settings.ENV_SSH_USER,
            password=settings.ENV_SSH_PASSWORD,
            timeout=(
                float(settings.ENV_SSH_TIMEOUT)
                if hasattr(settings, "ENV_SSH_TIMEOUT")
                else 10.0
            ),
        )
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(self._keepalive_interval)
        self._client = client
        logger.debug("SSH session connected to port %d", self._ports.ssh)

    def _close_locked(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                logger.debug("Error closing SSH client", exc_info=True)
            self._client = None

    def _execute_locked(self, command: str) -> SSHResult:
        """Execute with one automatic reconnection attempt on transport failure."""
        if not self._is_transport_active():
            self._connect_locked()

        try:
            return self._run_command(command)
        except (paramiko.SSHException, OSError) as first_err:
            logger.warning("SSH command failed, reconnecting: %s", first_err)
            self._connect_locked()
            return self._run_command(command)

    def _run_command(self, command: str) -> SSHResult:
        if self._client is None:
            msg = "SSH client is not connected"
            raise paramiko.SSHException(msg)

        channel = self._client.get_transport()
        if channel is None:
            msg = "SSH transport is not available"
            raise paramiko.SSHException(msg)

        ssh_channel = channel.open_session()
        ssh_channel.settimeout(float(self._command_timeout))
        ssh_channel.exec_command(f"{self._command_prefix}{command}")

        exit_code = ssh_channel.recv_exit_status()
        stdout = self._read_channel_output(ssh_channel, stderr=False)
        stderr = self._read_channel_output(ssh_channel, stderr=True)
        ssh_channel.close()

        return SSHResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )

    @staticmethod
    def _read_channel_output(channel: paramiko.Channel, *, stderr: bool) -> str:
        chunks: list[bytes] = []
        read_fn = channel.recv_stderr if stderr else channel.recv
        while True:
            data = read_fn(4096)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks).decode("utf-8")
